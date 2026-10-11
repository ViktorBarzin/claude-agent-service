"""No-tools completions — the opt-in sandbox for /v1/chat/completions.

The default compat path runs the CLI with every built-in tool and
--dangerously-skip-permissions inside a git-crypt-unlocked infra clone. This pod
also holds forge tokens, a Vault token and a service account that can exec into
pods, and nothing restricts its egress. That suits callers whose prompts are
their own. It does not suit callers that relay text from strangers (TripIt's
booking emails and reel captions, nextcloud-todos' to-dos): an injected
instruction would run with all of that in reach.

A request with ``"no_tools": true`` comes here instead:

- the CLI gets ``--tools ""`` and ``--strict-mcp-config``, so it has no
  built-in tools and no MCP servers, and ``--disable-slash-commands``;
- it runs in an empty scratch directory, never the infra clone;
- nothing is auto-approved (no --dangerously-skip-permissions), and no agent
  persona is loaded;
- the prompt goes in on stdin as one stream-json user message, so images ride
  along as base64 content blocks. The default path writes images to files for
  the Read tool, which this mode does not have.

Verified on CLI 2.1.197 (the pod) and 2.1.296: the init event reports
``tools: []`` and ``mcp_servers: []``, and a ticket image is still read.
"""
import asyncio
import base64
import binascii
import json
import shutil
import tempfile
from subprocess import PIPE

from app.conversational import STREAM_LINE_LIMIT

# The media types the Messages API accepts for a base64 image block.
_MEDIA_TYPES = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}


def argv(model: str, max_budget_usd: float | None) -> list[str]:
    """The CLI argv for one no-tools completion. The prompt arrives on stdin."""
    cmd = [
        "claude", "-p",
        "--tools", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--setting-sources", "user",
        "--exclude-dynamic-system-prompt-sections",
        "--no-session-persistence",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",  # the CLI requires it for stream-json under --print
        "--model", model,
    ]
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    return cmd


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            part.text for part in content
            if getattr(part, "type", None) == "text" and getattr(part, "text", None))
    return ""


def _image_block(url: str) -> dict | None:
    """A base64 image block for a `data:` URI, or None when it cannot be one.

    A remote link would need WebFetch, which this mode does not have, so it is
    dropped along with anything that is not a decodable image.
    """
    if not url.startswith("data:"):
        return None
    header, _, encoded = url[len("data:"):].partition(",")
    media_type = _MEDIA_TYPES.get(header.split(";", 1)[0].strip().lower())
    if media_type is None or not encoded:
        return None
    try:
        base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": encoded}}


def stdin_message(messages) -> bytes:
    """One stream-json user event carrying the whole request.

    System text is a preamble and user turns follow in order, as on the default
    path. Prior assistant turns are dropped: this is a stateless completion.
    """
    system = [t for m in messages if m.role == "system" and (t := _text(m.content))]
    user = [t for m in messages if m.role == "user" and (t := _text(m.content))]
    sections: list[str] = []
    if system:
        sections.append("System instructions:\n" + "\n\n".join(system))
    if user:
        sections.append("Request:\n" + "\n\n".join(user))
    blocks: list[dict] = [{"type": "text", "text": "\n\n---\n\n".join(sections)}]
    for message in messages:
        if message.role != "user" or not isinstance(message.content, list):
            continue
        for part in message.content:
            if part.type != "image_url" or not isinstance(part.image_url, dict):
                continue
            block = _image_block(str(part.image_url.get("url") or ""))
            if block is not None:
                blocks.append(block)
    event = {"type": "user", "message": {"role": "user", "content": blocks}}
    return json.dumps(event, ensure_ascii=False).encode() + b"\n"


def result_text(lines: list[str]) -> tuple[str, str | None]:
    """The reply text and, when the run failed, the reason.

    The final `result` event carries both; a run that never produced one died
    before answering.
    """
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            text = str(event.get("result") or "")
            if event.get("is_error"):
                return text, text or str(event.get("subtype") or "error")
            return text, None
    return "", "no result from the CLI"


async def run(messages, model: str, max_budget_usd: float | None) -> tuple[str, str | None]:
    """Run one no-tools completion; return (reply, error)."""
    scratch = tempfile.mkdtemp(prefix="no-tools-")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv(model, max_budget_usd),
            cwd=scratch, stdin=PIPE, stdout=PIPE, stderr=PIPE,
            limit=STREAM_LINE_LIMIT,
        )
        assert proc.stdin is not None and proc.stdout is not None
        assert proc.stderr is not None
        proc.stdin.write(stdin_message(messages))
        await proc.stdin.drain()
        proc.stdin.close()
        lines = [raw.decode(errors="replace") async for raw in proc.stdout]
        stderr = (await proc.stderr.read()).decode(errors="replace")
        await proc.wait()
        text, error = result_text(lines)
        if error is None and proc.returncode != 0:
            error = stderr.strip() or f"exit {proc.returncode}"
        return text, error
    finally:
        # A caller's timeout cancels us; the child would otherwise keep running.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        shutil.rmtree(scratch, ignore_errors=True)
