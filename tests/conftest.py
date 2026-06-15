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


# --------------------------------------------------------------------------- #
# AFK loop fixtures.
#
# Shared factories + in-memory fakes for the app.afk modules. EVERYTHING the AFK
# tests touch is faked here — no test ever reaches a real T3 server, GitHub /
# Forgejo, or the cluster. The fakes implement the module interfaces from the
# contract and record their calls so tests can assert on them.
# --------------------------------------------------------------------------- #
from app.afk.types import (  # noqa: E402  (after the env setup above, like app_main)
    CIStatus,
    Config,
    Issue,
    RunState,
    ThreadStatus,
)


@pytest.fixture
def make_issue():
    """Factory for ``Issue``. Defaults to a clean, dispatchable issue (trusted
    label, nothing blocking); override any field per test."""
    def _make(
        number: int = 1,
        repo: str = "infra",
        labels: list[str] | None = None,
        blocked_by: list[int] | None = None,
        labeled_by_trusted: bool = True,
        priority: int = 0,
    ) -> Issue:
        return Issue(
            number=number,
            repo=repo,
            labels=["ready-for-agent"] if labels is None else labels,
            blocked_by=[] if blocked_by is None else blocked_by,
            labeled_by_trusted=labeled_by_trusted,
            priority=priority,
        )
    return _make


@pytest.fixture
def make_config():
    """Factory for ``Config``. Defaults to an ENABLED config (kill switch off,
    a one-repo allowlist) so policy/state-machine tests exercise real behaviour;
    the disabled production default is covered separately in the config tests."""
    def _make(
        allowlist: list[str] | None = None,
        kill_switch: bool = False,
        **overrides,
    ) -> Config:
        return Config(
            allowlist=["infra"] if allowlist is None else allowlist,
            kill_switch=kill_switch,
            **overrides,
        )
    return _make


@pytest.fixture
def make_run_state():
    """Factory for ``RunState``. Defaults to a freshly-dispatched run (thread
    running, nothing pushed, no CI, no fix-forward attempts yet)."""
    def _make(
        thread_status: ThreadStatus | None = ThreadStatus.RUNNING,
        ci_status: CIStatus | None = None,
        pushed: bool = False,
        fix_forward_attempts: int = 0,
        elapsed_seconds: float = 0.0,
    ) -> RunState:
        return RunState(
            thread_status=thread_status,
            ci_status=ci_status,
            pushed=pushed,
            fix_forward_attempts=fix_forward_attempts,
            elapsed_seconds=elapsed_seconds,
        )
    return _make


class FakeT3Client:
    """In-memory stand-in for ``t3_client.T3Client``. Records each dispatch and
    hands back a deterministic thread id; ``snapshot`` returns whatever was
    staged via ``set_snapshot``."""

    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self._snapshot: dict = {"threads": []}
        self._next_id = 0

    def dispatch(self, repo: str, issue: int, prompt: str) -> str:
        thread_id = f"thread-{self._next_id}"
        self._next_id += 1
        self.dispatched.append(
            {"repo": repo, "issue": issue, "prompt": prompt, "thread_id": thread_id}
        )
        return thread_id

    def snapshot(self) -> dict:
        return self._snapshot

    def set_snapshot(self, snapshot: dict) -> None:
        self._snapshot = snapshot


class FakeTracker:
    """In-memory stand-in for ``tracker.Tracker``. ``list_ready`` returns issues
    staged via ``seed``; label/comment/close just record their calls."""

    def __init__(self) -> None:
        self._ready: dict[str, list[Issue]] = {}
        self.label_ops: list[tuple[str, str, int, str]] = []  # (op, repo, issue, label)
        self.comments: list[tuple[str, int, str]] = []
        self.closed: list[tuple[str, int]] = []

    def seed(self, repo: str, issues: list[Issue]) -> None:
        self._ready[repo] = issues

    def list_ready(self, repos: list[str]) -> list[Issue]:
        out: list[Issue] = []
        for repo in repos:
            out.extend(self._ready.get(repo, []))
        return out

    def add_label(self, repo: str, issue: int, label: str) -> None:
        self.label_ops.append(("add", repo, issue, label))

    def remove_label(self, repo: str, issue: int, label: str) -> None:
        self.label_ops.append(("remove", repo, issue, label))

    def comment(self, repo: str, issue: int, body: str) -> None:
        self.comments.append((repo, issue, body))

    def close(self, repo: str, issue: int) -> None:
        self.closed.append((repo, issue))


class FakeCIWatcher:
    """In-memory stand-in for ``ci_watcher.CIWatcher``. Returns the status staged
    per ``(repo, commit)`` via ``set_status``; unknown commits read PENDING."""

    def __init__(self) -> None:
        self._statuses: dict[tuple[str, str], CIStatus] = {}

    def set_status(self, repo: str, commit: str, status: CIStatus) -> None:
        self._statuses[(repo, commit)] = status

    def status(self, repo: str, commit: str) -> CIStatus:
        return self._statuses.get((repo, commit), CIStatus.PENDING)


class FakeNotifier:
    """In-memory stand-in for ``notifier.Notifier``. Records every notification
    so tests can assert escalations fired with the right kind/detail."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def notify(self, kind: str, issue: Issue, thread_id: str | None, detail: str) -> None:
        self.sent.append(
            {"kind": kind, "issue": issue, "thread_id": thread_id, "detail": detail}
        )


@pytest.fixture
def fake_t3() -> FakeT3Client:
    return FakeT3Client()


@pytest.fixture
def fake_tracker() -> FakeTracker:
    return FakeTracker()


@pytest.fixture
def fake_ci() -> FakeCIWatcher:
    return FakeCIWatcher()


@pytest.fixture
def fake_notifier() -> FakeNotifier:
    return FakeNotifier()
