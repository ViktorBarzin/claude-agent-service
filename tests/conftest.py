import asyncio
import os

os.environ.setdefault("API_BEARER_TOKEN", "test-token")
os.environ.setdefault("WORKSPACE_DIR", "/tmp/test-workspace")

import pytest

from app import main as app_main


@pytest.fixture(autouse=True)
def _reset_execution_state():
    """Reset concurrency state between tests.

    A fresh semaphore per test avoids the "bound to a different event loop"
    error (pytest-asyncio uses a new loop per function), and clearing the
    counters/jobs keeps tests independent.
    """
    app_main.jobs.clear()
    app_main.inflight_active = 0
    app_main.inflight_queued = 0
    app_main.execution_semaphore = asyncio.Semaphore(app_main.MAX_CONCURRENCY)
    app_main._last_fetch_epoch = 0.0
    app_main.MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "100"))
    yield


@pytest.fixture
def drain():
    """Wait for all background /execute jobs to finish.

    Tests that fire `/execute` must drain before leaving the `patch(...)`
    context — otherwise a background task resumes after the mocks are torn
    down, spawns a real subprocess during loop teardown, and deadlocks the
    asyncio child-watcher.
    """
    async def _drain(timeout: float = 3.0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while app_main.inflight_active or app_main.inflight_queued:
            if loop.time() > deadline:
                break
            await asyncio.sleep(0.01)
    return _drain
