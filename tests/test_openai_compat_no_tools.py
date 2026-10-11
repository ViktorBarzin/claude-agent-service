"""The opt-in no-tools mode on /v1/chat/completions.

The default compat path runs the CLI with every built-in tool and
--dangerously-skip-permissions inside a git-crypt-unlocked infra clone, in a pod
that also holds forge tokens, a Vault token and an exec-capable service account.
That is fine for callers that send their own prompts, and wrong for callers that
relay text from strangers: a booking email, a reel caption, a to-do. With
``"no_tools": true`` the run gets no tools, no MCP servers, no slash commands and
an empty scratch directory instead of the infra clone, so injected instructions
have nothing to act with. Images travel as content blocks on stdin, because the
file-and-Read route the default path uses needs a tool.
"""
import base64
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import no_tools
from app.main import ChatMessage, app

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode()
PNG_DATA_URL = "data:image/png;base64," + PNG_B64


def _messages(*raw: dict) -> list[ChatMessage]:
    return [ChatMessage.model_validate(m) for m in raw]


@pytest.fixture
def auth_header():
    return {"Authorization": "Bearer test-token"}


class _AsyncLineIter:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


def _stream(*events: dict) -> list[bytes]:
    return [json.dumps(e).encode() + b"\n" for e in events]


def _init_event() -> dict:
    return {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}


def _result_event(text: str, is_error: bool = False) -> dict:
    return {"type": "result", "subtype": "success", "is_error": is_error,
            "result": text, "total_cost_usd": 0.001}


def _mock_proc(lines: list[bytes], returncode: int = 0):
    proc = AsyncMock()
    proc.stdout = _AsyncLineIter(lines)
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.stdin = AsyncMock()
    proc.stdin.write = lambda data: proc.written.append(data)
    proc.stdin.close = lambda: None
    proc.stdin.drain = AsyncMock()
    proc.written = []
    proc.wait = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    return proc


# --- argv -------------------------------------------------------------------

def test_argv_disables_every_tool_and_mcp_server() -> None:
    argv = no_tools.argv(model="haiku", max_budget_usd=2.0)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.0"


def test_argv_never_bypasses_permissions_or_names_an_agent() -> None:
    """Nothing may be auto-approved, and no agent persona or its tools load."""
    argv = no_tools.argv(model="sonnet", max_budget_usd=None)
    assert "--dangerously-skip-permissions" not in argv
    assert "--agent" not in argv
    assert "--max-budget-usd" not in argv


def test_argv_reads_the_prompt_from_stdin() -> None:
    """The prompt is not on argv, so a large email or an image cannot hit ARG_MAX."""
    argv = no_tools.argv(model="haiku", max_budget_usd=2.0)
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"


# --- stdin ------------------------------------------------------------------

def test_stdin_message_carries_system_and_user_text() -> None:
    raw = no_tools.stdin_message(_messages(
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "Extract the flight."},
    ))
    event = json.loads(raw)
    assert event["type"] == "user"
    blocks = event["message"]["content"]
    assert blocks[0]["type"] == "text"
    assert "Return JSON." in blocks[0]["text"]
    assert "Extract the flight." in blocks[0]["text"]
    assert raw.endswith(b"\n")


def test_stdin_message_sends_images_as_content_blocks() -> None:
    raw = no_tools.stdin_message(_messages(
        {"role": "user", "content": [
            {"type": "text", "text": "Read the ticket."},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        ]},
    ))
    blocks = json.loads(raw)["message"]["content"]
    images = [b for b in blocks if b["type"] == "image"]
    assert images == [{"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": PNG_B64}}]
    # No file is named, because nothing could open it.
    assert "Read tool" not in blocks[0]["text"]


def test_stdin_message_drops_images_it_cannot_inline() -> None:
    """A remote link would need WebFetch and a bad payload is junk; both go."""
    raw = no_tools.stdin_message(_messages(
        {"role": "user", "content": [
            {"type": "text", "text": "Read these."},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!"}},
            {"type": "image_url", "image_url": {"url": "data:text/html;base64,PGI+"}},
        ]},
    ))
    blocks = json.loads(raw)["message"]["content"]
    assert [b["type"] for b in blocks] == ["text"]


def test_stdin_message_normalises_jpg_media_type() -> None:
    jpg = "data:image/jpg;base64," + base64.b64encode(b"jpegbytes").decode()
    raw = no_tools.stdin_message(_messages(
        {"role": "user", "content": [
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": jpg}},
        ]},
    ))
    image = json.loads(raw)["message"]["content"][1]
    assert image["source"]["media_type"] == "image/jpeg"


# --- result -----------------------------------------------------------------

def test_result_text_is_read_from_the_result_event() -> None:
    lines = [line.decode() for line in _stream(_init_event(), _result_event("BA 2490"))]
    assert no_tools.result_text(lines) == ("BA 2490", None)


def test_an_error_result_is_reported_as_an_error() -> None:
    lines = [line.decode() for line in _stream(_result_event("budget exceeded", is_error=True))]
    text, error = no_tools.result_text(lines)
    assert error == "budget exceeded"


def test_a_run_with_no_result_event_is_an_error() -> None:
    lines = [line.decode() for line in _stream(_init_event())]
    _, error = no_tools.result_text(lines)
    assert error


# --- the endpoint -----------------------------------------------------------

@pytest.mark.asyncio
async def test_no_tools_request_skips_the_infra_workspace(auth_header):
    proc = _mock_proc(_stream(_init_event(), _result_event('{"flight": "BA 2490"}')))
    prepare = AsyncMock(return_value="/workspace/jobs/x")
    with patch("app.no_tools.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=proc)) as spawn, \
            patch("app.main.prepare_workspace", new=prepare):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "haiku", "no_tools": True, "messages": [
                    {"role": "system", "content": "Return JSON."},
                    {"role": "user", "content": [
                        {"type": "text", "text": "Read the ticket."},
                        {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
                    ]},
                ]},
                headers=auth_header,
            )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == '{"flight": "BA 2490"}'
    prepare.assert_not_called()
    argv = list(spawn.call_args.args)
    assert argv[argv.index("--tools") + 1] == ""
    assert "--dangerously-skip-permissions" not in argv
    # The run's cwd is a scratch dir, not the infra clone, and it is gone after.
    cwd = spawn.call_args.kwargs["cwd"]
    assert not cwd.startswith("/workspace")
    assert not os.path.exists(cwd)
    # The image went in on stdin as a content block.
    sent = json.loads(b"".join(proc.written))
    assert sent["message"]["content"][1]["source"]["data"] == PNG_B64


@pytest.mark.asyncio
async def test_no_tools_error_result_maps_to_503(auth_header):
    proc = _mock_proc(_stream(_result_event("rate limited", is_error=True)), returncode=1)
    with patch("app.no_tools.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=proc)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "haiku", "no_tools": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=auth_header,
            )
    assert response.status_code == 503
    assert "rate limited" in response.json()["detail"]


@pytest.mark.asyncio
async def test_default_requests_keep_the_tool_enabled_path(auth_header):
    """Callers that rely on WebSearch (TripIt's narration) are unaffected."""
    result = json.dumps({"type": "result", "result": "Paris.", "is_error": False}).encode()
    proc = AsyncMock()
    proc.stdout = _AsyncLineIter([result + b"\n"])
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    prepare = AsyncMock(return_value="/tmp/ws")
    with patch("app.main.asyncio.create_subprocess_exec", return_value=proc) as spawn, \
            patch("app.main.prepare_workspace", new=prepare), \
            patch("app.main.cleanup_workspace", new=AsyncMock()), \
            patch("app.no_tools.run",
                  new=AsyncMock(side_effect=AssertionError("no-tools path used"))):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "haiku", "messages": [
                    {"role": "user", "content": "Capital of France?"}]},
                headers=auth_header,
            )
    assert response.status_code == 200
    prepare.assert_awaited_once()
    assert "--dangerously-skip-permissions" in spawn.call_args.args
