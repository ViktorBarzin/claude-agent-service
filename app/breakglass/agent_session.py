"""Claude CLI argv + stream-json → UI-event translation for the breakglass agent.

The session lifecycle (running turns, attaching clients) lives in ``session.py``;
this module is just the two helpers it builds on:
  * ``_turn_argv`` — the no-shell list argv for one ``claude -p`` turn.
  * ``translate_event`` — map a raw stream-json event to the small UI vocabulary
    (session / text / tool / result), dropping the hook/thinking-token noise.
"""
from . import config


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
        # The session log flattens a "batch" into individual events.
        return events[0] if len(events) == 1 else {"kind": "batch", "events": events}

    if etype == "result":
        return {
            "kind": "result",
            "is_error": bool(obj.get("is_error")),
            "result": obj.get("result", ""),
            "duration_ms": obj.get("duration_ms"),
        }

    return None
