from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app import main as app_main
from app.main import ExecuteRequest, app


def test_execute_request_default_timeout_is_45_minutes():
    # The service-upgrade agent can take 20-45m for CAUTION-class bumps
    # (multi-release changelog summarisation, Woodpecker CI polling, DB
    # backup waits). 15m cuts off too many real runs — see beads code-cfy.
    assert ExecuteRequest(prompt="p", agent="a").timeout_seconds == 2700


@pytest.fixture
def auth_header():
    return {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_health_returns_ok():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_execute_rejects_missing_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/execute", json={
            "prompt": "test",
            "agent": "test-agent",
        })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_execute_rejects_wrong_token():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/execute",
            json={"prompt": "test", "agent": "test-agent"},
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_execute_rejects_missing_prompt(auth_header):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/execute",
            json={"agent": "test-agent"},
            headers=auth_header,
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_execute_starts_job(auth_header, drain):
    mock_process = AsyncMock()
    mock_process.stdout = AsyncMock()
    mock_process.stdout.__aiter__ = MagicMock(return_value=iter([]))
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=0)
    mock_process.returncode = 0

    with patch("app.main.asyncio.create_subprocess_exec", return_value=mock_process):
        with patch("app.main.prepare_workspace", new=AsyncMock(return_value="/tmp/ws")), \
                patch("app.main.cleanup_workspace", new=AsyncMock()):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/execute",
                    json={"prompt": "test prompt", "agent": "test-agent"},
                    headers=auth_header,
                )
                await drain()
    assert response.status_code == 202
    body = response.json()
    assert "job_id" in body
    assert body["status"] == "queued"


@pytest.mark.asyncio
async def test_get_job_not_found(auth_header):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/jobs/nonexistent", headers=auth_header)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_execute_stores_metadata_on_job(auth_header, drain):
    mock_process = AsyncMock()
    mock_process.stdout = AsyncMock()
    mock_process.stdout.__aiter__ = MagicMock(return_value=iter([]))
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=0)
    mock_process.returncode = 0

    metadata = {"task_id": "code-xyz", "source": "beadboard"}

    with patch("app.main.asyncio.create_subprocess_exec", return_value=mock_process):
        with patch("app.main.prepare_workspace", new=AsyncMock(return_value="/tmp/ws")), \
                patch("app.main.cleanup_workspace", new=AsyncMock()):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/execute",
                    json={
                        "prompt": "test prompt",
                        "agent": "beads-task-runner",
                        "metadata": metadata,
                    },
                    headers=auth_header,
                )
                assert response.status_code == 202
                job_id = response.json()["job_id"]

                job_response = await client.get(f"/jobs/{job_id}", headers=auth_header)
                await drain()
    assert job_response.status_code == 200
    assert job_response.json()["metadata"] == metadata


@pytest.mark.asyncio
async def test_execute_rejects_empty_api_token_header():
    # When the service is booted without an API_BEARER_TOKEN (misconfiguration),
    # every request must be rejected — including requests with an empty Bearer
    # header. Without the guard, hmac.compare_digest("", "") would return True.
    with patch.object(app_main, "API_TOKEN", ""):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/execute",
                json={"prompt": "test", "agent": "test-agent"},
                headers={"Authorization": "Bearer "},
            )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_execute_accepts_correct_bearer_token(drain):
    mock_process = AsyncMock()
    mock_process.stdout = AsyncMock()
    mock_process.stdout.__aiter__ = MagicMock(return_value=iter([]))
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=0)
    mock_process.returncode = 0

    with patch.object(app_main, "API_TOKEN", "secret"):
        with patch("app.main.asyncio.create_subprocess_exec", return_value=mock_process):
            with patch("app.main.prepare_workspace", new=AsyncMock(return_value="/tmp/ws")), \
                patch("app.main.cleanup_workspace", new=AsyncMock()):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/execute",
                        json={"prompt": "test", "agent": "test-agent"},
                        headers={"Authorization": "Bearer secret"},
                    )
                    await drain()
    assert response.status_code == 202
