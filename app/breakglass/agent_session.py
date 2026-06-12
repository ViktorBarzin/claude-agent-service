"""Drive the breakglass Claude agent and stream its work to the browser.

Each chat turn runs ``claude -p --output-format stream-json`` in the session's
persistent workspace; the first turn opens the session with ``--session-id`` and
later turns ``--resume`` it, so the conversation has memory across turns. The
CLI's JSON events are translated to a small, stable SSE vocabulary the UI
renders (``session`` / ``text`` / ``tool`` / ``result`` / ``error``) — we do not
leak the raw event firehose to the client.

Subprocesses use ``asyncio.create_subprocess_exec`` (list argv, no shell): the
prompt and ids are argv elements, never interpreted by a shell.
"""
import asyncio
import json
import os
from subprocess import PIPE
from typing import AsyncIterator

from . import config

# Sessions we've already opened (so the next turn resumes instead of re-creating).
_started: set[str] = set()


def _turn_argv(session_id: str, prompt: str, resume: bool, model: str) -> list[str]:
    argv = [
        "claude", "-p",
        "--agent", config.BREAKGLASS_AGENT,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",                      # required for stream-json output
        "--model", model,
    ]
    # --session-id opens a brand-new session with that id; --resume continues it.
    argv += (["--resume", session_id] if resume else ["--session-id", session_id])
    argv.append(prompt)
    return argv


def translate_event(obj: dict) -> dict | None:
    """Map one raw stream-json event to a UI event, or None to drop it.

    Pure function — the unit tests pin this contract. Keeps the noisy
    hook/thinking-token/system chatter off the wire and exposes only what an
    operator watching a recovery needs: which session, assistant prose, which
    tools ran, and the final result.
    """
    etype = obj.get("type")

    if etype == "system":
        if obj.get("subtype") == "init":
            return {"kind": "session", "session_id": obj.get("session_id", "")}
        return None  # hook_started/hook_response/thinking_tokens/etc. — noise

    if etype == "assistant":
        events: list[dict] = []
        for block in obj.get("message", {}).get("content", []) or []:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                events.append({"kind": "text", "text": block["text"]})
            elif btype == "tool_use":
                events.append({
                    "kind": "tool",
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                })
        if not events:
            return None
        # The server flattens a "batch" into individual SSE frames.
        return events[0] if len(events) == 1 else {"kind": "batch", "events": events}

    if etype == "result":
        return {
            "kind": "result",
            "is_error": bool(obj.get("is_error")),
            "result": obj.get("result", ""),
            "duration_ms": obj.get("duration_ms"),
        }

    return None


async def run_turn(
    session_id: str, prompt: str, model: str | None = None
) -> AsyncIterator[dict]:
    """Run one chat turn, yielding translated UI events as they arrive."""
    resume = session_id in _started
    model = model or config.DEFAULT_MODEL
    workspace = os.path.join(config.SESSIONS_DIR, session_id)
    os.makedirs(workspace, exist_ok=True)

    argv = _turn_argv(session_id, prompt, resume, model)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=workspace, stdout=PIPE, stderr=PIPE,
    )
    _started.add(session_id)
    assert proc.stdout is not None and proc.stderr is not None

    try:
        async def _pump() -> AsyncIterator[dict]:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = translate_event(obj)
                if ev is None:
                    continue
                if ev.get("kind") == "batch":
                    for sub in ev["events"]:
                        yield sub
                else:
                    yield ev

        async for ev in _with_timeout(_pump(), config.TURN_TIMEOUT_SECONDS):
            yield ev
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        yield {"kind": "error", "error": f"turn timed out after {config.TURN_TIMEOUT_SECONDS}s"}
        return

    await proc.wait()
    if proc.returncode not in (0, None):
        err = (await proc.stderr.read()).decode(errors="replace")
        yield {"kind": "error", "error": err.strip()[:500] or f"exit {proc.returncode}"}


async def _with_timeout(agen: AsyncIterator[dict], timeout: float) -> AsyncIterator[dict]:
    """Yield from an async generator but raise TimeoutError if the WHOLE turn
    exceeds ``timeout`` seconds (a wedged agent shouldn't stream forever)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    it = agen.__aiter__()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            yield await asyncio.wait_for(it.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
