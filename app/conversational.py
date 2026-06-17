"""Conversational Brain — drives the Claude CLI for the portal-assistant gateway.

A lean, no-tools, multi-turn path (portal-assistant ADR-0002): no workspace clone,
no tool-enabled agent, and NO --dangerously-skip-permissions. Per-conversation
continuity comes from the Claude CLI's own --session-id / --resume, so the gateway
only has to hand us a stable session id per conversation.
"""
import asyncio
import json
import os
from subprocess import PIPE

CONVERSATIONAL_AGENT = "conversational"
# A spoken chat turn is short; a turn that runs longer than this is wedged.
CONVERSATIONAL_TIMEOUT_SECONDS = int(
    os.environ.get("CONVERSATIONAL_TIMEOUT_SECONDS", "120")
)

# Session ids the Claude CLI has already opened in THIS process, so a follow-up
# turn resumes instead of re-opening. In-memory + single-replica: a pod restart
# clears this AND the CLI's emptyDir session state together, so they stay in sync.
_started: set[str] = set()


def reset_started() -> None:
    """Forget all opened sessions (used by tests)."""
    _started.clear()


def conversational_argv(
    session_id: str, message: str, model: str, resume: bool
) -> list[str]:
    """Build the argv for one conversational turn.

    A new conversation opens the session with --session-id; subsequent turns
    continue it with --resume so Claude keeps its own context. We never pass
    --dangerously-skip-permissions: the conversational agent has no tools and the
    endpoint is public-facing, so nothing may be auto-permitted.
    """
    argv = [
        "claude", "-p",
        "--agent", CONVERSATIONAL_AGENT,
        "--output-format", "json",
        "--model", model,
    ]
    argv += ["--resume", session_id] if resume else ["--session-id", session_id]
    argv.append(message)
    return argv


def extract_reply(output_lines: list[str]) -> str:
    """Pull the final assistant text out of `claude -p --output-format json`.

    The CLI emits one JSON object with the final message under `result`; fall
    back to the raw text if it isn't parseable so callers always get something.
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


async def run_turn(session_id: str, message: str, model: str) -> dict:
    """Run one conversational turn and return {exit_code, reply, stderr}.

    Resumes the Claude session if we've opened it before; otherwise opens it.
    The session is only marked opened on success so a failed first turn can be
    retried cleanly as a new one.
    """
    resume = session_id in _started
    argv = conversational_argv(session_id, message, model, resume)

    proc = await asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=PIPE)
    assert proc.stdout is not None and proc.stderr is not None

    output_lines: list[str] = []
    async for line in proc.stdout:
        output_lines.append(line.decode(errors="replace"))
    stderr = await proc.stderr.read()
    await proc.wait()

    if proc.returncode == 0:
        _started.add(session_id)

    return {
        "exit_code": proc.returncode,
        "reply": extract_reply(output_lines),
        "stderr": stderr.decode(errors="replace"),
    }
