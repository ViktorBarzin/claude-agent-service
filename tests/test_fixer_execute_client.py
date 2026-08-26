"""Tests for ``app.fixer.execute_client`` — one-shot jobs behind the loop's ports.

The mapping under test is the one that decides whether a run waits, closes, or
escalates, so every job status the runner can produce is covered, including the
two that only happen when something has gone wrong: an unrecognised status, and
a job the runner no longer knows about (a pod restart mid-run).
"""
import pytest

from app.afk.types import Action, CIStatus, Config, Issue
from app.afk.watcher import InFlightRun, Watcher
from app.fixer.execute_client import ExecuteClient


class FakeRunner:
    """Stands in for ``/execute`` + ``/jobs/{id}``."""

    def __init__(self, statuses: dict[str, str] | None = None):
        self.statuses = statuses or {}
        self.submitted: list[str] = []
        self.next_id = "job-1"

    def submit(self, prompt: str) -> str:
        self.submitted.append(prompt)
        return self.next_id

    def fetch(self, job_id: str) -> dict | None:
        if job_id not in self.statuses:
            return None
        return {"status": self.statuses[job_id]}


def make_client(statuses: dict[str, str] | None = None):
    runner = FakeRunner(statuses)
    return ExecuteClient(runner.submit, runner.fetch), runner


# --------------------------------------------------------------------------- #
# The adapter drives the real Watcher — the point of the whole class.
# --------------------------------------------------------------------------- #
def _watcher_tick(client, fake_tracker, fake_ci, fake_notifier, run):
    watcher = Watcher(
        t3_client=client, tracker=fake_tracker, ci_watcher=fake_ci,
        notifier=fake_notifier, ready_for_human_label="needs-human",
    )
    return watcher.tick(run, Config(allowlist=["infra"], kill_switch=False))


def test_a_completed_job_with_green_ci_closes_the_issue(
    fake_tracker, fake_ci, fake_notifier
):
    client, _ = make_client({"job-1": "completed"})
    client.dispatch("infra", 5, "go")
    fake_ci.set_status("infra", "abc1234", CIStatus.GREEN)
    run = InFlightRun(
        issue=Issue(number=5, repo="infra", labels=["broken"], blocked_by=[],
                    labeled_by_trusted=True, priority=1),
        thread_id="job-1", commit="abc1234",
    )
    result = _watcher_tick(client, fake_tracker, fake_ci, fake_notifier, run)
    assert result.action is Action.CLOSE_SUCCESS
    assert fake_tracker.closed == [("infra", 5)]


def test_a_job_the_runner_forgot_escalates_instead_of_waiting(
    fake_tracker, fake_ci, fake_notifier
):
    """The gap the design doc flagged: a restart must not park a run forever."""
    client, _ = make_client(statuses={})
    client.track("job-lost")
    run = InFlightRun(
        issue=Issue(number=5, repo="infra", labels=["broken"], blocked_by=[],
                    labeled_by_trusted=True, priority=1),
        thread_id="job-lost", commit=None,
    )
    result = _watcher_tick(client, fake_tracker, fake_ci, fake_notifier, run)
    assert result.action is Action.ESCALATE_PREPUSH
    assert ("add", "infra", 5, "needs-human") in fake_tracker.label_ops


def test_a_running_job_keeps_waiting(fake_tracker, fake_ci, fake_notifier):
    client, _ = make_client({"job-1": "running"})
    client.dispatch("infra", 5, "go")
    run = InFlightRun(
        issue=Issue(number=5, repo="infra", labels=["broken"], blocked_by=[],
                    labeled_by_trusted=True, priority=1),
        thread_id="job-1", commit=None,
    )
    result = _watcher_tick(client, fake_tracker, fake_ci, fake_notifier, run)
    assert result.action is Action.WAIT
    assert fake_tracker.closed == []


# --------------------------------------------------------------------------- #
# dispatch.
# --------------------------------------------------------------------------- #
def test_dispatch_submits_the_prompt_and_returns_the_job_id():
    client, runner = make_client()
    assert client.dispatch("infra", 5, "fix issue 5") == "job-1"
    assert runner.submitted == ["fix issue 5"]


def test_dispatch_refuses_an_empty_job_id():
    """A runner that answers without an id has failed, and must not look dispatched."""
    client, runner = make_client()
    runner.next_id = ""
    with pytest.raises(RuntimeError, match="infra#5"):
        client.dispatch("infra", 5, "fix it")
    assert client.snapshot() == {"threads": []}


# --------------------------------------------------------------------------- #
# snapshot / turn_state mapping.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("job_status,expected", [
    ("queued", "running"),
    ("running", "running"),
    ("completed", "completed"),
    ("failed", "errored"),
    ("error", "errored"),
    ("timeout", "errored"),
])
def test_every_job_status_maps_to_a_turn_state(job_status, expected):
    client, _ = make_client({"job-1": job_status})
    client.dispatch("infra", 5, "go")
    assert client.snapshot() == {"threads": [{"id": "job-1", "latestTurn": {"state": expected}}]}


def test_an_unrecognised_status_escalates_rather_than_hanging():
    client, _ = make_client({"job-1": "something-new"})
    client.dispatch("infra", 5, "go")
    assert client.turn_state("job-1") == "errored"


def test_a_forgotten_job_reads_as_errored():
    """A pod restart takes the in-process job dict with it; the run is not still running."""
    client, _ = make_client(statuses={})
    client.dispatch("infra", 5, "go")
    assert client.turn_state("job-1") == "errored"


def test_a_never_dispatched_job_reads_as_errored():
    client, _ = make_client()
    assert client.turn_state("job-nobody-started") == "errored"


# --------------------------------------------------------------------------- #
# track — adopting a job an earlier tick started.
# --------------------------------------------------------------------------- #
def test_track_lets_a_later_tick_see_an_earlier_runs_job():
    client, _ = make_client({"job-from-footer": "running"})
    client.track("job-from-footer")
    assert client.snapshot() == {
        "threads": [{"id": "job-from-footer", "latestTurn": {"state": "running"}}]
    }


def test_track_is_idempotent():
    client, _ = make_client({"j": "running"})
    client.track("j")
    client.track("j")
    assert len(client.snapshot()["threads"]) == 1


def test_track_ignores_an_empty_id():
    client, _ = make_client()
    client.track("")
    assert client.snapshot() == {"threads": []}


def test_snapshot_reports_several_runs_independently():
    client, _ = make_client({"a": "running", "b": "completed"})
    client.track("a")
    client.track("b")
    states = {t["id"]: t["latestTurn"]["state"] for t in client.snapshot()["threads"]}
    assert states == {"a": "running", "b": "completed"}


# --------------------------------------------------------------------------- #
# Unreachable runner vs dead run — conflating them escalated a live run.
# --------------------------------------------------------------------------- #
def test_an_unreachable_runner_reads_as_unknown_not_errored():
    """Regression (infra#56, 2026-08-26): a run two minutes into working was
    escalated because a failed fetch was reported as a dead run."""
    from app.fixer.execute_client import STATE_UNKNOWN, UNREACHABLE
    client = ExecuteClient(lambda p: "job-1", lambda j: dict(UNREACHABLE))
    client.track("job-1")
    assert client.turn_state("job-1") == STATE_UNKNOWN


def test_an_unknown_turn_state_makes_the_watcher_wait(
    fake_tracker, fake_ci, fake_notifier
):
    """The watcher does not recognise 'unknown', which is exactly right: an
    unrecognised state means 'no status yet', and that WAITs."""
    from app.fixer.execute_client import UNREACHABLE
    client = ExecuteClient(lambda p: "job-1", lambda j: dict(UNREACHABLE))
    client.track("job-1")
    run = InFlightRun(
        issue=Issue(number=56, repo="infra", labels=["broken"], blocked_by=[],
                    labeled_by_trusted=True, priority=1),
        thread_id="job-1", commit=None,
    )
    result = _watcher_tick(client, fake_tracker, fake_ci, fake_notifier, run)
    assert result.action is Action.WAIT
    assert ("add", "infra", 56, "needs-human") not in fake_tracker.label_ops


def test_a_genuinely_unknown_job_still_escalates(fake_tracker, fake_ci, fake_notifier):
    """The runner answering 'no such job' IS an assertion the run is gone."""
    client = ExecuteClient(lambda p: "job-1", lambda j: None)
    client.track("job-1")
    run = InFlightRun(
        issue=Issue(number=56, repo="infra", labels=["broken"], blocked_by=[],
                    labeled_by_trusted=True, priority=1),
        thread_id="job-1", commit=None,
    )
    result = _watcher_tick(client, fake_tracker, fake_ci, fake_notifier, run)
    assert result.action is Action.ESCALATE_PREPUSH
