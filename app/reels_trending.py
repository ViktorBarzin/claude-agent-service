"""The Reels trending-songs route: ``POST /v1/reels/trending-songs``.

The Reels worker (viktor/trip-reel) asks once a week for the songs trending
right now as audio on Instagram Reels and TikTok. No official chart exists, so
an agent reads this week's trending-audio roundups with web search. The worker
downloads and parses untrusted media, so it never holds the ``/execute`` token
and this route starts its one agent in a locked-down way, like the tv intake:

- its own token (``REELS_TRENDING_TOKEN``); unset, the route answers 503;
- ``--tools WebSearch,WebFetch`` and the same two allowed, no MCP servers, no
  slash commands, no settings files, ``--permission-mode dontAsk``, no
  ``--dangerously-skip-permissions``;
- an environment of ``PATH``, ``HOME``, ``LANG`` and the Claude credential
  only;
- a fixed prompt in which only a validated ISO date varies;
- one run at a time, and the answer cached per date, so a retrying worker does
  not start a second search.

Synchronous: the call returns when the agent has answered, which can take
several minutes.
"""
import asyncio
import datetime
import hmac
import json
import logging
import os
import re
import tempfile
from subprocess import PIPE

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.intake import parse_agent

log = logging.getLogger(__name__)

router = APIRouter()

ENV_TOKEN = "REELS_TRENDING_TOKEN"
ENV_AGENT_FILE = "REELS_TRENDING_AGENT_FILE"
ENV_MODEL = "REELS_TRENDING_MODEL"

AGENT = "reels-trending"
DEFAULT_AGENT_FILE = "/usr/share/agent-seed/reels-trending.md"
TOOLS = ("WebSearch", "WebFetch")
BUDGET_USD = "2"
RUN_TIMEOUT_SECONDS = 900
MIN_SONGS = 3

ENV_KEEP = ("PATH", "HOME", "LANG")
CLAUDE_CREDENTIAL_ENV = ("CLAUDE_CODE_OAUTH_TOKEN",)

SCHEMA = {
    "type": "object",
    "properties": {"songs": {"type": "array", "items": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "artist": {"type": "string"},
                       "why": {"type": "string"}, "source": {"type": "string"}},
        "required": ["title", "artist", "why", "source"],
    }}},
    "required": ["songs"],
}

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

cache: dict[str, list[dict]] = {}
semaphore = asyncio.Semaphore(1)


class TrendingRequest(BaseModel):
    today: str


def verify_token(authorization: str | None) -> None:
    token = os.environ.get(ENV_TOKEN, "")
    if not token:
        raise HTTPException(status_code=503, detail="Reels trending not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if not hmac.compare_digest(authorization.removeprefix("Bearer "), token):
        raise HTTPException(status_code=401, detail="Invalid token")


def valid_date(value: str) -> str:
    if not _DATE.match(value):
        raise ValueError("today must be YYYY-MM-DD")
    datetime.date.fromisoformat(value)  # raises on 2026-13-01
    return value


def load_agent() -> dict:
    with open(os.environ.get(ENV_AGENT_FILE) or DEFAULT_AGENT_FILE) as fh:
        return parse_agent(fh.read())


def argv(agent: dict, model: str | None) -> list[str]:
    """The CLI argv for one run. The prompt arrives on stdin."""
    if tuple(agent.get("tools") or ()) != TOOLS:
        raise ValueError(f"the reels-trending agent must have exactly {', '.join(TOOLS)}")
    tools = ",".join(TOOLS)
    cmd = [
        "claude", "-p",
        "--agent", AGENT,
        "--agents", json.dumps({AGENT: agent}),
        "--setting-sources", "",
        "--tools", tools,
        # In print mode a tool that is available but not allowed is a silent
        # permission denial: the agent searches nothing and answers empty.
        "--allowedTools", tools,
        "--permission-mode", "dontAsk",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
        "--max-budget-usd", BUDGET_USD,
    ]
    if model:
        cmd.extend(["--model", model])
    return cmd


def scrubbed_env(environ) -> dict[str, str]:
    return {k: environ[k] for k in (*ENV_KEEP, *CLAUDE_CREDENTIAL_ENV) if k in environ}


_PROMPT = """\
Today is {today}. Find the songs trending right now as audio on Instagram Reels
and TikTok, following your instructions. Your final message must be ONLY one JSON
object, {{"songs": [{{"title", "artist", "why", "source"}}, ...]}}: no prose, no
code fence.
"""


def prompt(today: str) -> str:
    return _PROMPT.format(today=today)


def parse_songs(stdout: str) -> list[dict]:
    """The songs from the CLI's JSON output. Raises RuntimeError with the reason."""
    try:
        event = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError("unreadable output from the CLI") from None
    if not isinstance(event, dict) or event.get("is_error"):
        raise RuntimeError(str(event.get("result") if isinstance(event, dict) else "") or "agent error")
    structured = event.get("structured_output") or _json_in_text(str(event.get("result") or ""))
    raw = (structured or {}).get("songs") or []
    songs = [
        {k: str(s.get(k) or "") for k in ("title", "artist", "why", "source")}
        for s in raw if isinstance(s, dict) and s.get("title") and s.get("artist")
    ]
    if len(songs) < MIN_SONGS:
        raise RuntimeError(f"only {len(songs)} songs found")
    return songs


def _json_in_text(text: str) -> dict | None:
    """The outermost {...} in the agent's text answer, if it parses. With --agent,
    the CLI does not always apply --json-schema, and the answer arrives as text."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


async def run_agent(today: str) -> list[dict]:
    cmd = argv(load_agent(), os.environ.get(ENV_MODEL) or None)
    with tempfile.TemporaryDirectory(prefix="reels-trending-") as workdir:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workdir, env=scrubbed_env(os.environ),
            stdin=PIPE, stdout=PIPE, stderr=PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(prompt(today).encode()),
                                            timeout=RUN_TIMEOUT_SECONDS)
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
    return parse_songs(out.decode(errors="replace"))


@router.post("/v1/reels/trending-songs")
async def trending_songs(request: TrendingRequest, authorization: str | None = Header(default=None)):
    verify_token(authorization)
    try:
        today = valid_date(request.today)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    async with semaphore:
        if today not in cache:
            try:
                cache[today] = await run_agent(today)
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError) as exc:
                log.warning("reels trending for %s failed: %s", today, exc)
                raise HTTPException(status_code=502, detail=str(exc)[:300]) from None
            log.info("reels trending for %s: %d songs", today, len(cache[today]))
    return {"songs": cache[today]}
