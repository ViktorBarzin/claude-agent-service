"""Tests for parallel, independent execution.

These exercise the post-lock behavior: multiple agent calls run concurrently,
each in its own workspace, with a bounded semaphore + FIFO queue instead of a
single-flight lock.
"""
import asyncio

import pytest
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient

from app import main as app_main
from app.main import app


@pytest.fixture
def auth_header():
    return {"Authorization": "Bearer test-token"}


class _BlockingStdout:
    """async-iterable stdout that blocks on first read until `release` is set,
    then ends with no output — mimics a long-running `claude -p`."""

    def __init__(self, release: asyncio.Event):
        self._release = release
        self._done = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        await self._release.wait()
        self._done = True
        raise StopAsyncIteration


class ConcurrencyProbe:
    """Tracks how many mock subprocesses have started, and gates their exit."""

    def __init__(self):
        self.started = 0
        self.release = asyncio.Event()

    def factory(self):
        async def make(*args, **kwargs):
            self.started += 1
            mock = AsyncMock()
            mock.stdout = _BlockingStdout(self.release)
            mock.stderr = AsyncMock()
            mock.stderr.read = AsyncMock(return_value=b"")
            mock.wait = AsyncMock(return_value=0)
            mock.returncode = 0
            return mock
        return make

    async def wait_started(self, n: int, timeout: float = 2.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while self.started < n:
            if asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(0.01)


def _patch_workspace():
    """Patch the per-job workspace seams so no real git runs."""
    return (
        patch("app.main.prepare_workspace", new=AsyncMock(return_value="/tmp/ws")),
        patch("app.main.cleanup_workspace", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_execute_does_not_return_409_when_a_job_is_running(auth_header, drain):
    """A second /execute must NOT be rejected with 409 while one is in flight."""
    probe = ConcurrencyProbe()
    pw, cw = _patch_workspace()
    with pw, cw, patch("app.main.asyncio.create_subprocess_exec", side_effect=probe.factory()):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post("/execute", json={"prompt": "a", "agent": "x"}, headers=auth_header)
            await probe.wait_started(1)
            r2 = await client.post("/execute", json={"prompt": "b", "agent": "y"}, headers=auth_header)
            probe.release.set()
            await drain()
    assert r1.status_code == 202
    assert r2.status_code == 202


@pytest.mark.asyncio
async def test_two_execute_jobs_run_concurrently(auth_header, drain):
    """Two /execute jobs run their subprocesses at the same time (not serialized)."""
    probe = ConcurrencyProbe()
    pw, cw = _patch_workspace()
    with pw, cw, patch("app.main.asyncio.create_subprocess_exec", side_effect=probe.factory()):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/execute", json={"prompt": "a", "agent": "x"}, headers=auth_header)
            await client.post("/execute", json={"prompt": "b", "agent": "y"}, headers=auth_header)
            await probe.wait_started(2)
            both_running = probe.started >= 2
            probe.release.set()
            await drain()
    assert both_running, "both jobs should have started before either finished"


@pytest.mark.asyncio
async def test_safety_queue_blocks_beyond_capacity(auth_header, drain):
    """With capacity=1, the 2nd job is accepted but stays queued until a slot frees."""
    app_main.execution_semaphore = asyncio.Semaphore(1)
    probe = ConcurrencyProbe()
    pw, cw = _patch_workspace()
    with pw, cw, patch("app.main.asyncio.create_subprocess_exec", side_effect=probe.factory()):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post("/execute", json={"prompt": "a", "agent": "x"}, headers=auth_header)
            await probe.wait_started(1)
            r2 = await client.post("/execute", json={"prompt": "b", "agent": "y"}, headers=auth_header)
            # Give the 2nd task a chance to (not) start — capacity is 1.
            await asyncio.sleep(0.05)
            only_one_started = probe.started == 1
            job2 = (await client.get(f"/jobs/{r2.json()['job_id']}", headers=auth_header)).json()
            probe.release.set()
            await drain()
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert only_one_started, "2nd job must wait while capacity is full"
    assert job2["status"] == "queued"


@pytest.mark.asyncio
async def test_two_chat_completions_run_concurrently(auth_header):
    """Concurrent /v1/chat/completions both run — no 503 busy."""
    probe = ConcurrencyProbe()
    pw, cw = _patch_workspace()
    with pw, cw, patch("app.main.asyncio.create_subprocess_exec", side_effect=probe.factory()):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}
            t1 = asyncio.create_task(client.post("/v1/chat/completions", json=payload, headers=auth_header))
            t2 = asyncio.create_task(client.post("/v1/chat/completions", json=payload, headers=auth_header))
            await probe.wait_started(2)
            both_running = probe.started >= 2
            probe.release.set()
            r1, r2 = await asyncio.gather(t1, t2)
    assert both_running, "both chat calls should run concurrently"
    assert r1.status_code == 200
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_health_reports_capacity_fields():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["busy"] is False
    assert body["active"] == 0
    assert body["queued"] == 0
    assert body["capacity"] == app_main.MAX_CONCURRENCY


@pytest.mark.asyncio
async def test_each_job_gets_distinct_workspace(auth_header, drain):
    """prepare_workspace is called per job with the job id, yielding distinct cwds."""
    seen_job_ids = []

    async def fake_prepare(job_id):
        seen_job_ids.append(job_id)
        return f"/tmp/ws/{job_id}"

    probe = ConcurrencyProbe()
    with patch("app.main.prepare_workspace", side_effect=fake_prepare), \
            patch("app.main.cleanup_workspace", new=AsyncMock()), \
            patch("app.main.asyncio.create_subprocess_exec", side_effect=probe.factory()):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/execute", json={"prompt": "a", "agent": "x"}, headers=auth_header)
            await client.post("/execute", json={"prompt": "b", "agent": "y"}, headers=auth_header)
            await probe.wait_started(2)
            probe.release.set()
            await drain()
    assert len(set(seen_job_ids)) == 2, "each job should prepare its own workspace"


@pytest.mark.asyncio
async def test_queue_depth_rejection(auth_header, drain):
    """Beyond MAX_QUEUE_DEPTH, /execute is rejected with 429."""
    app_main.execution_semaphore = asyncio.Semaphore(1)
    app_main.MAX_QUEUE_DEPTH = 2
    probe = ConcurrencyProbe()
    pw, cw = _patch_workspace()
    with pw, cw, patch("app.main.asyncio.create_subprocess_exec", side_effect=probe.factory()):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post("/execute", json={"prompt": "a", "agent": "x"}, headers=auth_header)
            await probe.wait_started(1)
            r2 = await client.post("/execute", json={"prompt": "b", "agent": "y"}, headers=auth_header)
            r3 = await client.post("/execute", json={"prompt": "c", "agent": "z"}, headers=auth_header)
            probe.release.set()
            await drain()
    assert r1.status_code == 202  # active
    assert r2.status_code == 202  # queued
    assert r3.status_code == 429  # over depth


def test_evict_old_jobs_drops_finished_past_ttl():
    """Completed jobs older than JOB_TTL are evicted; running/queued are kept."""
    import time
    app_main.jobs.clear()
    now = time.time()
    app_main.jobs["old"] = {"status": "completed", "finished_epoch": now - 99999}
    app_main.jobs["fresh"] = {"status": "completed", "finished_epoch": now}
    app_main.jobs["running"] = {"status": "running"}
    app_main.jobs["queued"] = {"status": "queued"}
    app_main._evict_old_jobs()
    assert "old" not in app_main.jobs
    assert "fresh" in app_main.jobs
    assert "running" in app_main.jobs
    assert "queued" in app_main.jobs
