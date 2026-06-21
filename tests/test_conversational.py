"""Tests for the conversational (no-tools, multi-turn) brain endpoint.

This is the portal-assistant "Brain": a lean path that drives the Claude CLI with
a no-tools conversational agent and per-conversation `--resume`, used by the voice
gateway. Unlike /v1/chat/completions it does NOT clone a workspace or run a
tool-enabled agent (see portal-assistant ADR-0002).
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import conversational
from app.main import app


# --------------------------------------------------------------------------- #
# argv builder
# --------------------------------------------------------------------------- #
def test_conversational_argv_new_session():
    argv = conversational_argv_call(resume=False)
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--agent") + 1] == "conversational"
    # a new conversation opens with --session-id, never --resume
    assert argv[argv.index("--session-id") + 1] == "sess-1"
    assert "--resume" not in argv
    # SECURITY: a public-facing endpoint must NOT skip tool permissions
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--output-format") + 1] == "json"
    # latency: trims project CLAUDE.md/MCP + dynamic system-prompt sections off
    # the no-tools voice turn (~45k -> ~23k input tokens, ~1.3s faster TTFT)
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert argv[-1] == "Hi there"


def test_conversational_argv_resume_continues_session():
    argv = conversational_argv_call(resume=True)
    # a follow-up turn resumes the existing claude session
    assert argv[argv.index("--resume") + 1] == "sess-1"
    assert "--session-id" not in argv


def conversational_argv_call(resume: bool):
    from app.conversational import conversational_argv
    return conversational_argv(
        session_id="sess-1", message="Hi there", model="sonnet", resume=resume
    )


# --------------------------------------------------------------------------- #
# endpoint
# --------------------------------------------------------------------------- #
class _AsyncLineIter:
    """Async iterator over a list of byte lines — mimics `proc.stdout`."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._i]
        self._i += 1
        return line


def _mock_subprocess_returning(output: bytes, returncode: int = 0):
    proc = AsyncMock()
    lines = [chunk + b"\n" for chunk in output.split(b"\n") if chunk]
    proc.stdout = _AsyncLineIter(lines)
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    return proc


@pytest.fixture(autouse=True)
def _reset_sessions():
    conversational.reset_started()
    yield
    conversational.reset_started()


@pytest.fixture
def auth_header():
    return {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_conversational_happy_path(auth_header):
    """A message in → the assistant's reply out, keyed to the session."""
    cli_output = json.dumps({
        "type": "result",
        "is_error": False,
        "result": "Здравейте! Как мога да помогна?",
        "session_id": "sess-1",
    }).encode()
    mock_proc = _mock_subprocess_returning(cli_output, returncode=0)

    with patch("app.conversational.asyncio.create_subprocess_exec", return_value=mock_proc):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/conversational",
                json={"session_id": "sess-1", "message": "Здравей"},
                headers=auth_header,
            )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == "sess-1"
    assert body["reply"] == "Здравейте! Как мога да помогна?"


@pytest.mark.asyncio
async def test_conversational_resumes_on_second_turn(auth_header):
    """First turn opens the session (--session-id); a second turn on the same
    session id resumes it (--resume) — this is what makes it a conversation."""
    calls: list[tuple] = []

    def fake_spawn(*args, **kwargs):
        calls.append(args)
        out = json.dumps({"type": "result", "is_error": False, "result": "ok"}).encode()
        return _mock_subprocess_returning(out, returncode=0)

    with patch("app.conversational.asyncio.create_subprocess_exec", side_effect=fake_spawn):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(2):
                r = await client.post(
                    "/v1/conversational",
                    json={"session_id": "sess-X", "message": "hi"},
                    headers=auth_header,
                )
                assert r.status_code == 200, r.text

    assert "--session-id" in calls[0] and "--resume" not in calls[0]
    assert "--resume" in calls[1] and "--session-id" not in calls[1]


@pytest.mark.asyncio
async def test_conversational_requires_auth():
    """No bearer token → 401, same as the other endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/conversational",
            json={"session_id": "s", "message": "hi"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_conversational_returns_503_on_failure(auth_header):
    """A non-zero claude exit surfaces as 503 execution-failed."""
    mock_proc = _mock_subprocess_returning(b"", returncode=7)
    mock_proc.stderr.read = AsyncMock(return_value=b"boom")

    with patch("app.conversational.asyncio.create_subprocess_exec", return_value=mock_proc):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/v1/conversational",
                json={"session_id": "s", "message": "x"},
                headers=auth_header,
            )
    assert r.status_code == 503
    assert r.json()["error"] == "execution failed"


# --------------------------------------------------------------------------- #
# streaming helpers (OpenAI-compatible token relay for the realtime voice agent)
# --------------------------------------------------------------------------- #
from collections import namedtuple  # noqa: E402

_Msg = namedtuple("_Msg", "role content")


def test_stream_argv_uses_stream_json_and_is_stateless():
    argv = conversational.stream_argv("hello", "sonnet")
    assert argv[:2] == ["claude", "-p"]
    assert "--agent" in argv and "conversational" in argv
    assert "stream-json" in argv
    assert "--include-partial-messages" in argv
    assert "--verbose" in argv
    assert "--model" in argv and "sonnet" in argv
    # latency: same lean-context trim as the gateway path
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert argv[-1] == "hello"
    # stateless + no tools
    assert "--resume" not in argv and "--session-id" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_delta_text_extracts_content_block_delta():
    line = json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "text_delta", "text": "Слон"}},
    })
    assert conversational.delta_text(line) == "Слон"


def test_delta_text_ignores_non_text_events():
    for ev in [
        {"type": "system"},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": "{"}}},
        {"type": "result"},
    ]:
        assert conversational.delta_text(json.dumps(ev)) is None
    assert conversational.delta_text("") is None
    assert conversational.delta_text("not json") is None


def test_openai_chunk_valid_sse_and_keeps_cyrillic():
    s = conversational.openai_chunk("chatcmpl-x", "sonnet", 123, content="две")
    assert s.startswith("data: ") and s.endswith("\n\n")
    payload = json.loads(s[len("data: "):].strip())
    assert payload["object"] == "chat.completion.chunk"
    assert payload["choices"][0]["delta"]["content"] == "две"
    assert payload["choices"][0]["finish_reason"] is None
    assert "две" in s  # not unicode-escaped


def test_openai_chunk_role_and_finish():
    role = conversational.openai_chunk("id", "m", 1, role="assistant")
    assert json.loads(role[6:].strip())["choices"][0]["delta"] == {"role": "assistant"}
    stop = conversational.openai_chunk("id", "m", 1, finish_reason="stop")
    c = json.loads(stop[6:].strip())["choices"][0]
    assert c["finish_reason"] == "stop" and c["delta"] == {}


def test_synthesise_chat_prompt_keeps_assistant_turns():
    msgs = [
        _Msg("system", "Be brief."),
        _Msg("user", "Здравей"),
        _Msg("assistant", "Здравей! Как си?"),
        _Msg("user", "Добре, ти?"),
    ]
    p = conversational.synthesise_chat_prompt(msgs)
    assert "Be brief." in p
    assert "User: Здравей" in p
    assert "Assistant: Здравей! Как си?" in p
    assert p.strip().endswith("User: Добре, ти?")
