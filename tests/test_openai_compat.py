"""Tests for the OpenAI-compatible /v1/chat/completions endpoint."""
import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import main as app_main
from app.main import app


@pytest.fixture
def auth_header():
    return {"Authorization": "Bearer test-token"}


class _AsyncLineIter:
    """Real async iterator over a list of bytes lines — mimics
    `proc.stdout` from `asyncio.subprocess`."""

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
    """Build an AsyncMock that mimics asyncio.create_subprocess_exec."""
    mock_process = AsyncMock()
    lines = [chunk + b"\n" for chunk in output.split(b"\n") if chunk]
    mock_process.stdout = _AsyncLineIter(lines)
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=returncode)
    mock_process.returncode = returncode
    return mock_process


@pytest.mark.asyncio
async def test_chat_completions_happy_path(auth_header):
    """Happy path: messages in, OpenAI-shape response out."""
    cli_output = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Paris is the capital of France.",
        "total_cost_usd": 0.001,
        "num_turns": 1,
        "session_id": "abc123",
    }).encode()

    mock_proc = _mock_subprocess_returning(cli_output, returncode=0)

    with patch("app.main.asyncio.create_subprocess_exec", return_value=mock_proc), \
            patch("app.main.run_git_sync", new_callable=AsyncMock):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-haiku-4-5",
                    "messages": [
                        {"role": "system", "content": "You are concise."},
                        {"role": "user", "content": "Capital of France?"},
                    ],
                },
                headers=auth_header,
            )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "claude-haiku-4-5"
    assert "created" in body
    assert isinstance(body["created"], int)

    assert len(body["choices"]) == 1
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "Paris is the capital of France."

    assert "usage" in body
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert key in body["usage"]


@pytest.mark.asyncio
async def test_chat_completions_rejects_streaming(auth_header):
    """stream=true is not supported and must 400 with a clear message."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=auth_header,
        )
    assert response.status_code == 400
    body = response.json()
    assert "streaming not supported" in json.dumps(body).lower()


@pytest.mark.asyncio
async def test_chat_completions_requires_auth():
    """Missing bearer token must 401, identical to /execute."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chat_completions_wrong_bearer_token():
    """A wrong bearer token must also 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": "Bearer wrong"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chat_completions_returns_503_on_job_failure(auth_header):
    """If the underlying claude subprocess exits non-zero, return 503."""
    mock_proc = _mock_subprocess_returning(b"", returncode=42)
    mock_proc.stderr.read = AsyncMock(return_value=b"boom")

    with patch("app.main.asyncio.create_subprocess_exec", return_value=mock_proc), \
            patch("app.main.run_git_sync", new_callable=AsyncMock):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-haiku-4-5",
                    "messages": [{"role": "user", "content": "trigger fail"}],
                },
                headers=auth_header,
            )
    assert response.status_code == 503
    body = response.json()
    assert body.get("error") == "execution failed"
    assert "detail" in body


@pytest.mark.asyncio
async def test_chat_completions_rejects_empty_messages(auth_header):
    """`messages` must be a non-empty list."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-haiku-4-5",
                "messages": [],
            },
            headers=auth_header,
        )
    assert response.status_code in (400, 422)


@pytest.mark.asyncio
async def test_chat_completions_falls_back_when_no_json_result(auth_header):
    """If stdout is not parseable JSON, fall back to raw concatenation."""
    mock_proc = _mock_subprocess_returning(b"plain non-json output", returncode=0)

    with patch("app.main.asyncio.create_subprocess_exec", return_value=mock_proc), \
            patch("app.main.run_git_sync", new_callable=AsyncMock):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-haiku-4-5",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers=auth_header,
            )
    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "plain non-json output" in content


@pytest.mark.asyncio
async def test_chat_completions_concats_system_and_user_messages(auth_header):
    """The synthesised prompt passed to claude must include both system and user content."""
    captured: dict = {}

    async def fake_subprocess(*args, **kwargs):
        captured["args"] = args
        return _mock_subprocess_returning(
            json.dumps({"type": "result", "result": "ok", "is_error": False}).encode(),
            returncode=0,
        )

    with patch("app.main.asyncio.create_subprocess_exec", side_effect=fake_subprocess), \
            patch("app.main.run_git_sync", new_callable=AsyncMock):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-haiku-4-5",
                    "messages": [
                        {"role": "system", "content": "SYSTEM-MARKER"},
                        {"role": "user", "content": "USER-MARKER"},
                    ],
                },
                headers=auth_header,
            )
    assert response.status_code == 200
    prompt_arg = captured["args"][-1]
    assert "SYSTEM-MARKER" in prompt_arg
    assert "USER-MARKER" in prompt_arg


@pytest.mark.asyncio
async def test_chat_completions_returns_503_when_agent_busy(auth_header):
    """If the agent is already busy, return 503."""
    await app_main.execution_lock.acquire()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-haiku-4-5",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers=auth_header,
            )
    finally:
        app_main.execution_lock.release()
    assert response.status_code == 503
    body = response.json()
    assert body.get("error") == "execution failed"
