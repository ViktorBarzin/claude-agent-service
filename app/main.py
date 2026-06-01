import asyncio
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from subprocess import PIPE
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Claude Agent Service")

API_TOKEN = os.environ.get("API_BEARER_TOKEN", "")
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/workspace/infra")

# OpenAI compat: model selection is per-request so callers can pick
# Haiku/Sonnet/Opus to control cost. The agent is fixed — `recruiter-triage`
# has the broadest tool surface (WebSearch, WebFetch, Read, Grep, Glob, Bash);
# the alternative (`beads-task-runner`) is locked to read-only `bd` verbs which
# would fail arbitrary OpenAI-API callers. The model on the agent's frontmatter
# is overridden by the `--model` CLI flag we pass per-request.
SUPPORTED_MODELS: frozenset[str] = frozenset({
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
})
DEFAULT_MODEL = "claude-sonnet-4-6"
OPENAI_COMPAT_AGENT = "recruiter-triage"
OPENAI_COMPAT_BUDGET_USD = 2.0
OPENAI_COMPAT_TIMEOUT_SECONDS = 900

jobs: dict[str, dict] = {}
execution_lock = asyncio.Lock()


class ExecuteRequest(BaseModel):
    prompt: str
    agent: str
    max_budget_usd: float = 5.0
    timeout_seconds: int = 2700
    metadata: dict | None = None


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


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


async def run_git_sync():
    proc = await asyncio.create_subprocess_exec(
        "git", "pull", "--rebase",
        cwd=WORKSPACE_DIR,
        stdout=PIPE, stderr=PIPE,
    )
    await proc.wait()


async def _invoke_claude_subprocess(
    prompt: str,
    agent: str,
    max_budget_usd: float,
    model: str | None = None,
) -> dict[str, Any]:
    """Run the claude CLI once and return a result dict.

    The caller is responsible for holding `execution_lock` for the duration —
    this helper does not touch the lock or the `jobs` dict, so it can be
    shared by both the background `/execute` path and the synchronous
    `/v1/chat/completions` path.

    `model`, when provided, becomes `--model <id>` on the claude CLI. This
    overrides whatever `model:` is set in the agent's frontmatter so the
    OpenAI-compat path can pick Haiku/Sonnet/Opus per-request.
    """
    await run_git_sync()

    cmd = [
        "claude", "-p",
        "--agent", agent,
        "--dangerously-skip-permissions",
        "--max-budget-usd", str(max_budget_usd),
        "--output-format", "json",
    ]
    if model is not None:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=WORKSPACE_DIR,
        stdout=PIPE,
        stderr=PIPE,
    )

    output_lines: list[str] = []
    async for line in proc.stdout:
        output_lines.append(line.decode())

    stderr = await proc.stderr.read()
    await proc.wait()

    return {
        "exit_code": proc.returncode,
        "output": output_lines,
        "stderr": stderr.decode(),
    }


async def run_agent(job_id: str, request: ExecuteRequest):
    try:
        result = await _invoke_claude_subprocess(
            request.prompt, request.agent, request.max_budget_usd,
        )
        jobs[job_id].update({
            "status": "completed" if result["exit_code"] == 0 else "failed",
            "exit_code": result["exit_code"],
            "output": result["output"],
            "stderr": result["stderr"],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
    except asyncio.TimeoutError:
        jobs[job_id].update({"status": "timeout"})
    except Exception as exc:
        jobs[job_id].update({"status": "error", "error": str(exc)})
    finally:
        execution_lock.release()


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


def _synthesise_prompt(messages: list[ChatMessage]) -> str:
    """Flatten OpenAI chat messages into a single prompt body.

    System messages are surfaced as preamble; user messages become the
    actual request. Multiple user turns are concatenated in order so a
    short multi-turn back-and-forth still works (this is a stateless
    completion — we don't replay prior assistant turns).
    """
    system_parts = [m.content for m in messages if m.role == "system"]
    user_parts = [m.content for m in messages if m.role == "user"]
    # Assistant messages from prior turns are intentionally NOT injected —
    # claude `-p` is stateless and replaying them as user text would
    # confuse the agent.
    sections: list[str] = []
    if system_parts:
        sections.append("System instructions:\n" + "\n\n".join(system_parts))
    if user_parts:
        sections.append("Request:\n" + "\n\n".join(user_parts))
    if not sections:
        # Defensive — pydantic min_length=1 should already prevent this.
        return ""
    return "\n\n---\n\n".join(sections)


@app.get("/health")
async def health():
    return {"status": "ok", "busy": execution_lock.locked()}


@app.post("/execute", status_code=202)
async def execute(
    request: ExecuteRequest,
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)

    if execution_lock.locked():
        raise HTTPException(status_code=409, detail="Agent is busy")

    await execution_lock.acquire()

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "status": "running",
        "prompt": request.prompt,
        "agent": request.agent,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "metadata": request.metadata,
    }

    asyncio.create_task(
        asyncio.wait_for(
            run_agent(job_id, request),
            timeout=request.timeout_seconds,
        )
    )

    return {"job_id": job_id, "status": "running"}


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

    if request.stream:
        raise HTTPException(status_code=400, detail="streaming not supported")

    model = request.model if request.model is not None else DEFAULT_MODEL
    if model not in SUPPORTED_MODELS:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported model",
                "supported": sorted(SUPPORTED_MODELS),
            },
        )

    prompt = _synthesise_prompt(request.messages)

    if execution_lock.locked():
        return JSONResponse(
            status_code=503,
            content={"error": "execution failed", "detail": "agent is busy"},
        )

    await execution_lock.acquire()
    try:
        try:
            result = await asyncio.wait_for(
                _invoke_claude_subprocess(
                    prompt, OPENAI_COMPAT_AGENT, OPENAI_COMPAT_BUDGET_USD, model=model,
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
        execution_lock.release()

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
