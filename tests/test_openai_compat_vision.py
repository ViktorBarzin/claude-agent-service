"""Image input on /v1/chat/completions.

TripIt's ingest reads forwarded ticket screenshots with a local vision model
(llama-swap) and falls back here when that model is unreachable — which it
measurably is: 50 llama-server exits in 24h on 2026-09-10, and 9 of 10
extraction calls failed during that window. The fallback only works if this
service can accept an image, so `content` takes the OpenAI multimodal shape
(a list of `text` / `image_url` parts) alongside the plain string it has
always taken.

The claude CLI cannot be handed bytes on argv, so an inlined `data:` image is
written into the run's own workspace and the prompt names the file for the
agent to Read. These tests pin both halves: the file really lands in the
workspace, and the prompt really points at it.
"""
import base64
import json
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import ChatMessage, _synthesise_prompt, app, write_inline_images


def _messages(*raw: dict) -> list[ChatMessage]:
    """Validate dicts into the real model, so these tests exercise the same
    parsing the route does rather than a hand-built stand-in."""
    return [ChatMessage.model_validate(m) for m in raw]

# A one-pixel PNG — enough to prove the bytes survive the round trip.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


@pytest.fixture
def auth_header():
    return {"Authorization": "Bearer test-token"}


class _AsyncLineIter:
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
    mock_process = AsyncMock()
    lines = [chunk + b"\n" for chunk in output.split(b"\n") if chunk]
    mock_process.stdout = _AsyncLineIter(lines)
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=returncode)
    mock_process.returncode = returncode
    return mock_process


def _result(text: str) -> bytes:
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": text, "total_cost_usd": 0.001, "num_turns": 1,
        "session_id": "vis123",
    }).encode()


# --- the pure helpers -------------------------------------------------------

def test_write_inline_images_lands_the_bytes_in_the_workspace() -> None:
    """The agent Reads the image off disk, so the bytes must survive decode."""
    with tempfile.TemporaryDirectory() as ws:
        names = write_inline_images(ws, [PNG_DATA_URL])
        assert names == ["input-image-1.png"]
        assert open(os.path.join(ws, names[0]), "rb").read() == PNG_BYTES


def test_write_inline_images_names_each_by_its_declared_type() -> None:
    jpeg = "data:image/jpeg;base64," + base64.b64encode(b"notarealjpeg").decode()
    with tempfile.TemporaryDirectory() as ws:
        names = write_inline_images(ws, [PNG_DATA_URL, jpeg])
        assert names == ["input-image-1.png", "input-image-2.jpg"]


def test_write_inline_images_skips_what_it_cannot_decode() -> None:
    """A remote URL or a malformed data: URI is dropped, not written as junk —
    the agent has WebFetch for the former and nothing to do with the latter."""
    with tempfile.TemporaryDirectory() as ws:
        names = write_inline_images(ws, [
            "https://example.com/pass.png",
            "data:image/png;base64,!!!not base64!!!",
            PNG_DATA_URL,
        ])
        assert names == ["input-image-1.png"]
        assert sorted(os.listdir(ws)) == ["input-image-1.png"]


def test_synthesise_prompt_still_takes_a_plain_string() -> None:
    """The text callers (recruiter-responder, TripIt's text fallback) must not
    change shape."""
    prompt = _synthesise_prompt(_messages(
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "Capital of France?"},
    ))
    assert "Be terse." in prompt
    assert "Capital of France?" in prompt
    assert "input-image" not in prompt


def test_synthesise_prompt_reads_text_parts_out_of_a_content_list() -> None:
    prompt = _synthesise_prompt(_messages(
        {"role": "user", "content": [
            {"type": "text", "text": "Extract the travel segments."},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        ]},
    ))
    assert "Extract the travel segments." in prompt
    # The data: URI itself must never reach argv — it is megabytes of base64.
    assert "base64" not in prompt


def test_synthesise_prompt_points_the_agent_at_the_written_files() -> None:
    prompt = _synthesise_prompt(
        _messages({"role": "user", "content": [
            {"type": "text", "text": "Read it."},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        ]}),
        image_files=["input-image-1.png"],
    )
    assert "input-image-1.png" in prompt


# --- the endpoint -----------------------------------------------------------

@pytest.mark.asyncio
async def test_an_inlined_image_reaches_the_workspace_and_the_prompt(auth_header):
    """End to end through the route: the file is written into the run's own
    workspace and the prompt handed to the CLI names it."""
    mock_proc = _mock_subprocess_returning(_result('{"segments": []}'))
    with tempfile.TemporaryDirectory() as ws:
        with patch("app.main.asyncio.create_subprocess_exec",
                   return_value=mock_proc) as spawn, \
                patch("app.main.prepare_workspace", new=AsyncMock(return_value=ws)), \
                patch("app.main.cleanup_workspace", new=AsyncMock()):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "sonnet",
                        "messages": [
                            {"role": "system", "content": "You extract segments."},
                            {"role": "user", "content": [
                                {"type": "text", "text": "Extract the travel segments."},
                                {"type": "image_url",
                                 "image_url": {"url": PNG_DATA_URL}},
                            ]},
                        ],
                    },
                    headers=auth_header,
                )
            assert response.status_code == 200
            # The bytes landed where the agent can Read them...
            written = os.path.join(ws, "input-image-1.png")
            assert open(written, "rb").read() == PNG_BYTES
            # ...and the prompt the CLI actually received says so.
            prompt = spawn.call_args.args[-1]
            assert "input-image-1.png" in prompt
            assert "base64" not in prompt


@pytest.mark.asyncio
async def test_a_plain_string_request_is_unchanged(auth_header):
    """The existing text contract keeps working, images or no images."""
    mock_proc = _mock_subprocess_returning(_result("Paris."))
    with patch("app.main.asyncio.create_subprocess_exec", return_value=mock_proc), \
            patch("app.main.prepare_workspace", new=AsyncMock(return_value="/tmp/ws")), \
            patch("app.main.cleanup_workspace", new=AsyncMock()):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "haiku", "messages": [
                    {"role": "user", "content": "Capital of France?"}]},
                headers=auth_header,
            )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Paris."
