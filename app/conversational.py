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

# Latency: the conversational agent is no-tools (ADR-0002), so the CLI's default
# project context — this repo's CLAUDE.md, the MCP server configs, local settings
# — plus the dynamic system-prompt sections are pure overhead on a voice turn.
# Measured 2026-06-21: the default load is ~45k input tokens/turn -> ~3.4s TTFT;
# restricting settings to `user` and excluding the dynamic sections more than
# halves the context (~23k) and cuts TTFT to ~2.1s (~1.3s/turn faster) with no
# change to the reply. Applies to BOTH the gateway (json) and realtime (stream)
# paths, since both run the same no-tools conversational turn.
_LEAN_CONTEXT_FLAGS = [
    "--setting-sources", "user",
    "--exclude-dynamic-system-prompt-sections",
]

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
        *_LEAN_CONTEXT_FLAGS,
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


# ---------------------------------------------------------------------------
# Streaming (OpenAI-compatible) path — token-level deltas for the realtime
# voice agent. Pipecat's OpenAILLMService streams from /v1/chat/completions and
# re-sends the FULL history each turn, so this path is STATELESS: the whole
# dialogue goes in the prompt and we run a fresh CLI with stream-json to relay
# incremental tokens as OpenAI chat-completion SSE chunks. (run_turn above stays
# the session-based path for the non-streaming gateway.)
# ---------------------------------------------------------------------------


def stream_argv(prompt: str, model: str) -> list[str]:
    """Argv for a STREAMING conversational turn (token deltas via stream-json).

    Stateless — the full conversation is in `prompt` (no --session-id/--resume).
    `--include-partial-messages` makes the CLI emit `content_block_delta` token
    events; `--verbose` is required by the CLI for stream-json under --print. No
    --dangerously-skip-permissions: the conversational agent has no tools.
    """
    return [
        "claude", "-p",
        "--agent", CONVERSATIONAL_AGENT,
        "--model", model,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        *_LEAN_CONTEXT_FLAGS,
        prompt,
    ]


def delta_text(line: str) -> str | None:
    """Extract the incremental assistant text from one stream-json line.

    Returns the text of a `content_block_delta` / `text_delta` event, or None
    for any other event (system, message_start, content_block_stop, result) or
    an unparseable line.
    """
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("type") != "stream_event":
        return None
    inner = event.get("event") or {}
    if inner.get("type") != "content_block_delta":
        return None
    delta = inner.get("delta") or {}
    if delta.get("type") == "text_delta":
        return delta.get("text") or None
    return None


def openai_chunk(
    completion_id: str,
    model: str,
    created: int,
    *,
    role: str | None = None,
    content: str | None = None,
    finish_reason: str | None = None,
) -> str:
    """Format one OpenAI `chat.completion.chunk` as an SSE `data:` line.

    ensure_ascii=False keeps Cyrillic (Bulgarian) intact on the wire.
    """
    delta: dict[str, str] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def synthesise_chat_prompt(messages) -> str:
    """Flatten OpenAI chat messages into a dialogue prompt for the conversational
    agent, KEEPING prior assistant turns.

    Pipecat re-sends the full message history every call, so multi-turn context
    is preserved here (statelessly) by replaying the dialogue. Each message is a
    duck-typed object with `.role` and `.content`. System messages become a
    preamble; user/assistant turns are rendered as a `User:`/`Assistant:`
    dialogue ending on the latest user turn.
    """
    system = [m.content for m in messages if m.role == "system" and m.content]
    turns = []
    for m in messages:
        if m.role == "user" and m.content:
            turns.append("User: " + m.content)
        elif m.role == "assistant" and m.content:
            turns.append("Assistant: " + m.content)
    parts = []
    if system:
        parts.append("\n\n".join(system))
    if turns:
        parts.append("\n".join(turns))
    return "\n\n".join(parts).strip()
