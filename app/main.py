import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from subprocess import PIPE
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import conversational
from app.fixer import config as fixer_config
from app.fixer import receiver as fixer_receiver
from app.fixer.runlog import RunLog

log = logging.getLogger(__name__)

app = FastAPI(title="Claude Agent Service")

API_TOKEN = os.environ.get("API_BEARER_TOKEN", "")

# Warm base clone, populated by the init container. Each job clones from this
# into its own dir under JOBS_DIR so concurrent calls never share a working
# tree (no git index.lock contention, no clobbered edits).
BASE_DIR = os.environ.get("WORKSPACE_DIR", "/workspace/infra")
JOBS_DIR = os.environ.get("JOBS_DIR", "/workspace/jobs")
GIT_CRYPT_KEY = os.environ.get("GIT_CRYPT_KEY", "/secrets/git-crypt/key")

# Concurrency. MAX_CONCURRENCY caps simultaneous claude runs ("soft-unbounded"
# — a high default rather than a tight limit); excess calls queue FIFO rather
# than being rejected. MAX_QUEUE_DEPTH is a safety valve so a runaway burst
# can't pin unbounded memory: past it, callers are turned away (429/503).
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "10"))
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "100"))
# Completed jobs are evicted from the in-memory registry past this age so the
# dict doesn't grow without bound.
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "3600"))
# Bursts share one base fetch rather than serialising a network round-trip per
# job behind the git lock.
FETCH_DEBOUNCE_SECONDS = int(os.environ.get("FETCH_DEBOUNCE_SECONDS", "15"))

# OpenAI compat: model selection is per-request so callers can pick
# Haiku/Sonnet/Opus to control cost. NO agent is passed — an OpenAI-compatible
# endpoint takes its instructions from the caller's system message, and a named
# agent's own instructions compete with those. This used to run
# `recruiter-triage`, picked for its tool surface, and the persona came with it:
# asked on 2026-09-11 to extract travel segments from a real Egencia
# confirmation, it answered "I'm the recruiter-triage agent — my job is to
# produce a company deep-dive report" and returned nothing. The CLI's default
# agent has a broader tool surface than recruiter-triage's anyway
# (WebSearch, WebFetch, Read, Grep, Glob, Bash) and no persona of its own.
# Bare aliases auto-roll forward to the latest published version of each
# family. The Claude CLI resolves `haiku` → `claude-haiku-4-5-20251001`
# (and bumps it when Anthropic ships a newer Haiku) — letting us avoid
# version bumps on every release. Add a specific date-suffixed string here
# only if a caller needs to pin against an upcoming roll-forward.
SUPPORTED_MODELS: frozenset[str] = frozenset({
    "haiku",
    "sonnet",
    "opus",
    # Legacy date-suffixed forms — kept for callers that pinned before the
    # 2026-06-01 bare-aliases switch (fire-planner < c1c1e22). Drop these
    # once all consumers have been re-imaged.
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
})
DEFAULT_MODEL = "sonnet"
OPENAI_COMPAT_AGENT = None  # no persona; the caller's system message is the brief
OPENAI_COMPAT_BUDGET_USD = 2.0
OPENAI_COMPAT_TIMEOUT_SECONDS = 900

_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout", "error"})

jobs: dict[str, dict] = {}

# Concurrency primitives. The semaphore bounds simultaneous executions; the git
# lock is held only for the fast per-job workspace setup/teardown (fetch +
# local clone + unlock + rm), NOT for the agent run itself.
execution_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
git_lock = asyncio.Lock()
inflight_active = 0
inflight_queued = 0
_last_fetch_epoch = 0.0


class ExecuteRequest(BaseModel):
    prompt: str
    agent: str
    # ``None`` means NO ceiling — the flag is omitted from the CLI argv and the
    # execution is not raced against a clock. The fixer dispatches this way on
    # purpose (design doc, decision 14): a run is bounded by the per-repo lock,
    # one at a time, rather than by caps that would truncate a hard diagnosis
    # mid-way. Every other caller keeps the historical defaults.
    max_budget_usd: float | None = 5.0
    timeout_seconds: int | None = 2700
    metadata: dict | None = None


class ChatContentPart(BaseModel):
    """One part of an OpenAI multimodal `content` list."""
    type: str
    text: str | None = None
    image_url: dict | None = None
    model_config = {"extra": "allow"}


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    # A plain string is the original contract and still the common case. A list
    # of parts is the OpenAI multimodal shape, accepted so a caller whose own
    # vision model is down can send the image here instead (TripIt's ingest
    # fallback). Images travel inline as `data:` URIs; see write_inline_images
    # for why they land on disk rather than in the prompt.
    content: str | list[ChatContentPart]


class ChatCompletionsRequest(BaseModel):
    # `model` is optional: callers that omit it get DEFAULT_MODEL. We still
    # validate the explicit value against SUPPORTED_MODELS at the route level
    # so we can return a structured 400 listing the allowed IDs.
    model: str | None = None
    messages: list[ChatMessage] = Field(..., min_length=1)
    max_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False
    # Tolerate (and ignore) other OpenAI fields rather than 422-ing on them.
    model_config = {"extra": "allow"}


class ConversationalRequest(BaseModel):
    # The portal-assistant gateway owns the conversation; it hands us a stable
    # session id (for Claude --resume) plus the next user message. Model is
    # selectable per request, same as the OpenAI-compat path.
    session_id: str
    message: str
    model: str | None = None


def verify_token(authorization: str | None):
    # Reject everything when the service is unconfigured. compare_digest("", "")
    # returns True, so without this guard an empty API_TOKEN would happily
    # accept an empty header.
    if not API_TOKEN:
        raise HTTPException(status_code=401, detail="Service unauthenticated")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(token, API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reserve_queue_slot() -> bool:
    """Admit a call into the queue, or refuse it if the queue is saturated.

    Returns False when active + queued already fills MAX_QUEUE_DEPTH — the
    caller should then turn the request away (429/503).
    """
    global inflight_queued
    if inflight_active + inflight_queued >= MAX_QUEUE_DEPTH:
        return False
    inflight_queued += 1
    return True


@asynccontextmanager
async def _execution_slot():
    """Hold one concurrency permit for the duration of an agent run.

    The caller must have reserved a queue slot via `_reserve_queue_slot()`
    first; this moves it from queued -> active on acquire and always releases.
    """
    global inflight_active, inflight_queued
    acquired = False
    try:
        await execution_semaphore.acquire()
        acquired = True
        inflight_queued -= 1
        inflight_active += 1
        yield
    finally:
        if acquired:
            inflight_active -= 1
            execution_semaphore.release()
        else:
            # Cancelled while still waiting in the queue.
            inflight_queued -= 1


def _evict_old_jobs() -> None:
    now = time.time()
    stale = [
        jid for jid, job in jobs.items()
        if job.get("status") in _TERMINAL_STATUSES
        and now - job.get("finished_epoch", now) > JOB_TTL_SECONDS
    ]
    for jid in stale:
        jobs.pop(jid, None)


async def _run(*cmd: str, cwd: str | None = None, timeout: float | None = None,
               check: bool = True, capture: bool = False) -> tuple[int, str]:
    """Run a subprocess (no shell), optionally capturing stdout. Raises on
    non-zero unless `check=False`. Used for the git/git-crypt/rm steps of
    per-job workspace setup."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=PIPE, stderr=PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    rc = proc.returncode or 0
    if check and rc != 0:
        raise RuntimeError(f"{cmd[0]} failed ({rc}): {err.decode(errors='replace')[:200]}")
    return rc, (out.decode(errors="replace") if capture else "")


async def _refresh_base() -> None:
    """Pull the base clone up to origin/master, debounced so a burst of jobs
    shares one fetch. Failures are tolerated — jobs run against the last good
    base rather than wedging on a transient network blip."""
    global _last_fetch_epoch
    now = time.time()
    if now - _last_fetch_epoch < FETCH_DEBOUNCE_SECONDS:
        return
    _last_fetch_epoch = now
    await _run("git", "-C", BASE_DIR, "fetch", "origin", "--prune",
               timeout=120, check=False)
    await _run("git", "-C", BASE_DIR, "reset", "--hard", "origin/master",
               check=False)


async def prepare_workspace(job_id: str) -> str:
    """Create an isolated git checkout for one job and return its path.

    A local clone of the warm base hardlinks the object store (near-free) and
    carries only tracked files (no stale .terraform). The git lock is held just
    for this fast setup, never for the agent run.
    """
    job_dir = os.path.join(JOBS_DIR, job_id)
    async with git_lock:
        await _refresh_base()
        await _run("git", "clone", "--local", BASE_DIR, job_dir)
        rc, base_origin = await _run(
            "git", "-C", BASE_DIR, "remote", "get-url", "origin",
            check=False, capture=True,
        )
        if rc == 0 and base_origin.strip():
            await _run("git", "-C", job_dir, "remote", "set-url", "origin",
                       base_origin.strip(), check=False)
        if GIT_CRYPT_KEY and os.path.exists(GIT_CRYPT_KEY):
            await _run("git-crypt", "unlock", GIT_CRYPT_KEY, cwd=job_dir, check=False)
    return job_dir


async def cleanup_workspace(path: str | None) -> None:
    if not path:
        return
    await _run("rm", "-rf", path, check=False)


async def _invoke_claude_subprocess(
    prompt: str,
    agent: str | None,
    max_budget_usd: float | None,
    workspace: str,
    model: str | None = None,
    sink=None,
) -> dict[str, Any]:
    """Run the claude CLI once in `workspace` and return a result dict.

    Holds no lock and does not touch the `jobs` dict, so it is shared by both
    the background `/execute` path and the synchronous `/v1/chat/completions`
    path. The caller provides an isolated `workspace` (one per job) as cwd.

    `model`, when provided, becomes `--model <id>` on the claude CLI. This
    overrides whatever `model:` is set in the agent's frontmatter so the
    OpenAI-compat path can pick Haiku/Sonnet/Opus per-request.
    """
    # With a sink we ask for the EVENT STREAM rather than one final blob: the
    # point of the log is what happened during the run, and `json` only ever
    # tells us how it ended. The chat paths pass no sink and are unaffected.
    cmd = ["claude", "-p"]
    # `agent=None` is the OpenAI-compat path: no persona, so the caller's own
    # system message is the only brief. Every named-agent caller (/execute, the
    # fixer) still gets its agent.
    if agent is not None:
        cmd.extend(["--agent", agent])
    cmd.extend([
        "--dangerously-skip-permissions",
        "--output-format", "stream-json" if sink is not None else "json",
    ])
    if sink is not None:
        cmd.append("--verbose")  # stream-json requires it
    # A budget of None means "no ceiling": omit the flag rather than passing a
    # sentinel, so the CLI applies none at all.
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    if model is not None:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workspace,
        stdout=PIPE,
        stderr=PIPE,
    )

    # stdout=PIPE / stderr=PIPE guarantee both streams are present.
    assert proc.stdout is not None and proc.stderr is not None
    output_lines: list[str] = []
    try:
        async for line in proc.stdout:
            text = line.decode()
            output_lines.append(text)
            if sink is not None:
                # Written through as it arrives: a run killed mid-flight is
                # exactly the one worth reading afterwards.
                try:
                    sink(text)
                except Exception:  # noqa: BLE001 - logging never fails a run
                    pass

        stderr = await proc.stderr.read()
        await proc.wait()
    finally:
        # Whoever cancelled us — a caller's timeout, a shutdown — the child does
        # not hear about it. Without this the `claude` process outlived its own
        # job record: the record flipped to "timeout" and the agent kept working
        # and kept spending, while cleanup_workspace deleted the tree underneath
        # it. Same shape as the streaming endpoint's cleanup below.
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    return {
        "exit_code": proc.returncode,
        "output": output_lines,
        "stderr": stderr.decode(),
    }


async def _run_execute_job(job_id: str, request: ExecuteRequest):
    """Background worker for /execute: waits for a slot (queued), then runs the
    agent in an isolated workspace. The timeout covers execution only, never
    the time spent waiting in the queue."""
    workspace = None
    label = str((request.metadata or {}).get("issue") or (request.metadata or {}).get("source") or "run")
    runlog = RunLog(job_id, label)
    jobs[job_id]["runlog"] = runlog.path
    runlog.event("start", agent=request.agent, label=label,
                 prompt_head=request.prompt[:1500])
    try:
        async with _execution_slot():
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = _now_iso()
            workspace = await prepare_workspace(job_id)
            result = await asyncio.wait_for(
                _invoke_claude_subprocess(
                    request.prompt, request.agent, request.max_budget_usd, workspace,
                    sink=runlog.raw,
                ),
                timeout=request.timeout_seconds,
            )
            jobs[job_id].update({
                "status": "completed" if result["exit_code"] == 0 else "failed",
                "exit_code": result["exit_code"],
                "output": result["output"],
                "stderr": result["stderr"],
                "finished_at": _now_iso(),
                "finished_epoch": time.time(),
            })
    except asyncio.TimeoutError:
        jobs[job_id].update({
            "status": "timeout",
            "finished_at": _now_iso(),
            "finished_epoch": time.time(),
        })
    except Exception as exc:
        jobs[job_id].update({
            "status": "error",
            "error": str(exc),
            "finished_at": _now_iso(),
            "finished_epoch": time.time(),
        })
    finally:
        record = jobs.get(job_id) or {}
        summary = runlog.finish(record.get("exit_code"), str(record.get("status")),
                                str(record.get("stderr") or ""))
        jobs[job_id]["summary"] = summary
        # One compact line to stdout so the run is queryable in Loki without
        # reaching for the volume: `homelab logs query`.
        log_line = {"fixer_run": job_id, "label": label, **{
            k: summary.get(k) for k in
            ("status", "duration_s", "turns", "tool_call_total", "tool_error_total",
             "pushed", "cost_usd")
        }}
        print("fixer-run " + json.dumps(log_line, default=str), flush=True)
        try:
            await cleanup_workspace(workspace)
        except Exception:
            pass
        _evict_old_jobs()


def _extract_assistant_text(output_lines: list[str]) -> str:
    """Pull the final assistant text out of `claude -p --output-format json`.

    The CLI emits a single JSON object on stdout (possibly across multiple
    lines if it pretty-prints) with a `result` field holding the final
    assistant message. If parsing fails for any reason, fall back to the
    raw concatenation so callers always get *something* useful.
    """
    raw = "".join(output_lines).strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, dict):
        for key in ("result", "content", "text"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value
    return raw


def _one_line(text: str, limit: int = 200) -> str:
    """Collapse multi-line text to a single line, truncated for response bodies."""
    flat = " ".join(text.split())
    return flat[:limit]


# MIME type → the extension the agent's Read tool expects. Anything outside
# this map is not an image we can hand over, so it is dropped.
_IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _message_text(content: object) -> str:
    """The text of one message, whether it came as a string or as parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            part.text for part in content
            if getattr(part, "type", None) == "text" and getattr(part, "text", None))
    return ""


def _inline_image_urls(messages: list[ChatMessage]) -> list[str]:
    """Every `image_url` url carried by the messages, in order."""
    urls: list[str] = []
    for message in messages:
        if not isinstance(message.content, list):
            continue
        for part in message.content:
            if part.type != "image_url" or not isinstance(part.image_url, dict):
                continue
            url = part.image_url.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def write_inline_images(workspace: str, urls: list[str]) -> list[str]:
    """Decode `data:` image URIs into `workspace`; return the filenames written.

    The claude CLI takes its prompt on argv, so an image cannot travel in the
    prompt: a phone screenshot is hundreds of KB, and base64 of it would blow
    past ARG_MAX long before the model ever saw it. Writing the bytes into the
    run's own workspace and naming the file lets the agent's Read tool open it,
    which is the only image path the CLI has.

    A non-`data:` url (a remote link) and anything that fails to decode are
    skipped rather than written as junk — the agent has WebFetch for the former
    and nothing useful to do with the latter.
    """
    written: list[str] = []
    for url in urls:
        if not url.startswith("data:"):
            continue
        header, _, encoded = url[len("data:"):].partition(",")
        mime = header.split(";", 1)[0].strip().lower()
        extension = _IMAGE_EXTENSIONS.get(mime)
        if extension is None or not encoded:
            continue
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            log.warning("chat image part: undecodable %s payload, skipped", mime)
            continue
        name = f"input-image-{len(written) + 1}.{extension}"
        with open(os.path.join(workspace, name), "wb") as handle:
            handle.write(raw)
        written.append(name)
    return written


def _synthesise_prompt(
    messages: list[ChatMessage], image_files: list[str] | None = None,
) -> str:
    """Flatten OpenAI chat messages into a single prompt body.

    System messages are surfaced as preamble; user messages become the
    actual request. Multiple user turns are concatenated in order so a
    short multi-turn back-and-forth still works (this is a stateless
    completion — we don't replay prior assistant turns).

    `image_files` names the images already written into the run's workspace by
    `write_inline_images`; the prompt points the agent at them by filename. The
    bytes themselves never appear here.
    """
    system_parts = [t for m in messages if m.role == "system"
                    and (t := _message_text(m.content))]
    user_parts = [t for m in messages if m.role == "user"
                  and (t := _message_text(m.content))]
    # Assistant messages from prior turns are intentionally NOT injected —
    # claude `-p` is stateless and replaying them as user text would
    # confuse the agent.
    sections: list[str] = []
    if system_parts:
        sections.append("System instructions:\n" + "\n\n".join(system_parts))
    if user_parts:
        sections.append("Request:\n" + "\n\n".join(user_parts))
    if image_files:
        listed = "\n".join(f"- ./{name}" for name in image_files)
        sections.append(
            "Attached images (in your working directory — open each with the "
            f"Read tool before answering):\n{listed}")
    if not sections:
        # Defensive — pydantic min_length=1 should already prevent this.
        return ""
    return "\n\n---\n\n".join(sections)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "busy": inflight_active >= MAX_CONCURRENCY,
        "active": inflight_active,
        "queued": inflight_queued,
        "capacity": MAX_CONCURRENCY,
    }


def start_job(request: ExecuteRequest) -> str:
    """Queue a job and return its id — the shared path into the runner.

    Both ``POST /execute`` and the fixer's webhook receiver come through here, so
    a job started by a webhook is indistinguishable from any other: same queue
    slot accounting, same ``jobs`` record, same background worker. Raises
    ``RuntimeError`` when the queue is full; the HTTP caller maps that to 429 and
    the receiver lets it surface (an un-dispatched run must not be labelled).
    """
    if not _reserve_queue_slot():
        raise RuntimeError("queue full")

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "status": "queued",
        "prompt": request.prompt,
        "agent": request.agent,
        "created_at": _now_iso(),
        "metadata": request.metadata,
    }
    asyncio.create_task(_run_execute_job(job_id, request))
    return job_id


def _submit_fixer_job(prompt: str, issue: str = "") -> str:
    """Start a fixer run. The agent and the (absent) ceilings come from config,
    so the receiver never has to know how the runner is invoked.

    ``issue`` names the run's log file, so a transcript is findable by the issue
    it was for rather than only by an opaque job id.
    """
    cfg = fixer_config.from_env()
    return start_job(ExecuteRequest(
        prompt=prompt,
        agent=cfg.agent,
        max_budget_usd=cfg.max_budget_usd,
        timeout_seconds=cfg.timeout_seconds,
        metadata={"source": "fixer", "issue": issue},
    ))


app.include_router(fixer_receiver.router)
fixer_receiver.set_submitter(_submit_fixer_job)


@app.post("/execute", status_code=202)
async def execute(
    request: ExecuteRequest,
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    try:
        job_id = start_job(request)
    except RuntimeError:
        raise HTTPException(status_code=429, detail="Queue full")
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionsRequest,
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)

    model = request.model if request.model is not None else DEFAULT_MODEL
    if model not in SUPPORTED_MODELS:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported model",
                "supported": sorted(SUPPORTED_MODELS),
            },
        )

    # Streaming path (the realtime voice agent / Pipecat). Token-level deltas via
    # the conversational (no-tools) agent in stream-json mode, relayed as
    # OpenAI chat.completion.chunk SSE. Stateless: the full history is in the
    # prompt (the client re-sends it each turn). No workspace clone — the
    # conversational agent reads no files.
    if request.stream:
        if not _reserve_queue_slot():
            return JSONResponse(
                status_code=503,
                content={"error": "execution failed", "detail": "queue full"},
            )
        prompt = conversational.synthesise_chat_prompt(request.messages)
        completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        spawn = asyncio.create_subprocess_exec  # bound alias (keeps subprocess use tidy)

        async def event_stream():
            workspace = tempfile.mkdtemp(prefix="conv-stream-")
            proc = None
            try:
                async with _execution_slot():
                    proc = await spawn(
                        *conversational.stream_argv(prompt, model),
                        cwd=workspace, stdout=PIPE, stderr=PIPE,
                    )
                    assert proc.stdout is not None
                    yield conversational.openai_chunk(
                        completion_id, model, created, role="assistant"
                    )
                    try:
                        async with asyncio.timeout(
                            conversational.CONVERSATIONAL_TIMEOUT_SECONDS
                        ):
                            async for raw in proc.stdout:
                                text = conversational.delta_text(
                                    raw.decode(errors="replace")
                                )
                                if text:
                                    yield conversational.openai_chunk(
                                        completion_id, model, created, content=text
                                    )
                    except asyncio.TimeoutError:
                        pass  # wedged turn — close the stream cleanly
                    yield conversational.openai_chunk(
                        completion_id, model, created, finish_reason="stop"
                    )
                    yield "data: [DONE]\n\n"
            finally:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
                shutil.rmtree(workspace, ignore_errors=True)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    image_urls = _inline_image_urls(request.messages)

    if not _reserve_queue_slot():
        return JSONResponse(
            status_code=503,
            content={"error": "execution failed", "detail": "queue full"},
        )

    chat_id = uuid.uuid4().hex[:12]
    workspace = None
    try:
        async with _execution_slot():
            workspace = await prepare_workspace(chat_id)
            # Images are written AFTER the workspace exists and the prompt is
            # built from what actually landed, so a part we could not decode
            # never gets advertised to the agent as a file to Read.
            image_files = write_inline_images(workspace, image_urls)
            prompt = _synthesise_prompt(request.messages, image_files)
            result = await asyncio.wait_for(
                _invoke_claude_subprocess(
                    prompt, OPENAI_COMPAT_AGENT, OPENAI_COMPAT_BUDGET_USD,
                    workspace, model=model,
                ),
                timeout=OPENAI_COMPAT_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=503,
            content={"error": "execution failed", "detail": "agent timed out"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"error": "execution failed", "detail": _one_line(str(exc))},
        )
    finally:
        try:
            await cleanup_workspace(workspace)
        except Exception:
            pass

    if result["exit_code"] != 0:
        detail = _one_line(result.get("stderr") or "") or f"exit {result['exit_code']}"
        return JSONResponse(
            status_code=503,
            content={"error": "execution failed", "detail": detail},
        )

    content = _extract_assistant_text(result["output"])
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


@app.post("/v1/conversational")
async def conversational_turn(
    request: ConversationalRequest,
    authorization: str | None = Header(default=None),
):
    """Lean, multi-turn conversational Brain for the portal-assistant gateway.

    Drives a no-tools conversational agent with per-conversation --resume — no
    workspace clone, no tools (see portal-assistant ADR-0002). Returns the
    assistant's reply text keyed to the caller's session id.
    """
    verify_token(authorization)

    model = request.model if request.model is not None else DEFAULT_MODEL
    if model not in SUPPORTED_MODELS:
        return JSONResponse(
            status_code=400,
            content={"error": "unsupported model", "supported": sorted(SUPPORTED_MODELS)},
        )

    if not _reserve_queue_slot():
        return JSONResponse(
            status_code=503,
            content={"error": "execution failed", "detail": "queue full"},
        )

    try:
        async with _execution_slot():
            result = await asyncio.wait_for(
                conversational.run_turn(request.session_id, request.message, model),
                timeout=conversational.CONVERSATIONAL_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=503,
            content={"error": "execution failed", "detail": "agent timed out"},
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={"error": "execution failed", "detail": _one_line(str(exc))},
        )

    if result["exit_code"] != 0:
        detail = _one_line(result.get("stderr") or "") or f"exit {result['exit_code']}"
        return JSONResponse(
            status_code=503,
            content={"error": "execution failed", "detail": detail},
        )

    return {"session_id": request.session_id, "reply": result["reply"]}
