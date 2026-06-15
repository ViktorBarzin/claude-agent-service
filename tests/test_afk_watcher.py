"""Integration tests for ``app.afk.watcher`` — the in-flight run driver.

These wire the REAL pure cores (the actual ``run_state_machine.next_action`` and
``phase_checklist.render``) to the in-memory adapter FAKES from ``conftest``
(``FakeT3Client`` / ``FakeTracker`` / ``FakeCIWatcher`` / ``FakeNotifier``). No
test touches a real T3 server, GitHub/Forgejo, the cluster, or Slack — the
watcher is exercised end to end with fakes only at the I/O edges.

What one watch tick must do (the watcher contract), given an in-flight run
``(issue, thread_id, commit, bookkeeping)``:

  * assemble a ``RunState`` from ``t3_client.snapshot()`` (the thread's liveness)
    + ``ci_watcher.status(repo, commit)`` (the CI verdict, only when something is
    pushed) + the run's own ``pushed`` / ``fix_forward_attempts`` /
    ``elapsed_seconds`` bookkeeping, and feed it to the pure state machine;
  * **CLOSE_SUCCESS** → ``tracker.close``, drop the in-progress label, post the
    DONE checklist, and ring the ``done`` doorbell;
  * **ESCALATE_PREPUSH / FREEZE_ESCALATE** → drop the in-progress label, relabel
    ``ready-for-human``, ring the ``needs-human`` / ``frozen`` doorbell, post the
    checklist — the run is handed back to a human;
  * **FIX_FORWARD** → dispatch a corrective turn (``t3_client.dispatch``), bump
    the fix-forward attempt count, keep the run in flight, refresh the checklist;
    NOT terminal, so no doorbell and no label churn;
  * **WAIT** → just refresh the progress checklist and keep waiting; no labels,
    no close, no doorbell, no dispatch.
"""
import pytest

from app.afk import watcher
from app.afk.notifier import KIND_DONE, KIND_FROZEN, KIND_NEEDS_HUMAN
from app.afk.types import CIStatus, Issue


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
READY_FOR_HUMAN = "ready-for-human"


def _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier) -> watcher.Watcher:
    return watcher.Watcher(
        t3_client=fake_t3,
        tracker=fake_tracker,
        ci_watcher=fake_ci,
        notifier=fake_notifier,
    )


def _run(
    issue: Issue,
    thread_id: str = "thread-0",
    commit: str | None = None,
    fix_forward_attempts: int = 0,
    elapsed_seconds: float = 0.0,
) -> watcher.InFlightRun:
    return watcher.InFlightRun(
        issue=issue,
        thread_id=thread_id,
        commit=commit,
        fix_forward_attempts=fix_forward_attempts,
        elapsed_seconds=elapsed_seconds,
    )


def _snapshot(thread_id: str, status: str) -> dict:
    return {"threads": [{"id": thread_id, "status": status}]}


def _labels(fake_tracker):
    return [(op, repo, num, lbl) for (op, repo, num, lbl) in fake_tracker.label_ops]


def _kinds(fake_notifier):
    return [n["kind"] for n in fake_notifier.sent]


# --------------------------------------------------------------------------- #
# WAIT — agent still working, nothing pushed: refresh the checklist, no action.
# --------------------------------------------------------------------------- #
def test_wait_refreshes_checklist_and_does_nothing_else(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "running"))

    result = _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue), make_config()
    )

    assert result.action.value == "wait"
    assert result.terminal is False
    assert fake_tracker.closed == []
    assert _labels(fake_tracker) == []          # no label churn while waiting
    assert fake_notifier.sent == []             # no doorbell
    assert fake_t3.dispatched == []             # no corrective turn
    # The progress checklist was posted as a comment.
    assert len(fake_tracker.comments) == 1
    repo, num, body = fake_tracker.comments[0]
    assert (repo, num) == ("infra", 7)
    assert "AFK run progress" in body


def test_wait_when_thread_missing_from_snapshot(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    # No snapshot entry for this thread yet -> thread_status None -> WAIT.
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot({"threads": []})
    result = _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue), make_config()
    )
    assert result.action.value == "wait"
    assert result.terminal is False


def test_pushed_ci_pending_waits(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "running"))
    # commit present (pushed) but CI not yet decided -> PENDING -> WAIT.
    fake_ci.set_status("infra", "deadbeef", CIStatus.PENDING)
    result = _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, commit="deadbeef"), make_config()
    )
    assert result.action.value == "wait"
    assert fake_tracker.closed == []


# --------------------------------------------------------------------------- #
# CLOSE_SUCCESS — pushed + CI green: close, unlabel, DONE checklist, doorbell.
# --------------------------------------------------------------------------- #
def test_close_success_closes_and_unlabels_and_notifies(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "idle"))
    fake_ci.set_status("infra", "cafef00d", CIStatus.GREEN)

    result = _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, commit="cafef00d"), make_config()
    )

    assert result.action.value == "close_success"
    assert result.terminal is True
    assert fake_tracker.closed == [("infra", 7)]
    # in-progress label removed (no ready-for-human on the happy path).
    assert ("remove", "infra", 7, "agent-in-progress") in _labels(fake_tracker)
    assert ("add", "infra", 7, READY_FOR_HUMAN) not in _labels(fake_tracker)
    # done doorbell fired with the thread deep-link target.
    assert _kinds(fake_notifier) == [KIND_DONE]
    assert fake_notifier.sent[0]["thread_id"] == "thread-0"
    assert fake_notifier.sent[0]["issue"] is issue


def test_close_success_posts_done_checklist(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "idle"))
    fake_ci.set_status("infra", "cafef00d", CIStatus.GREEN)

    _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, commit="cafef00d"), make_config()
    )

    # The final checklist shows the run DONE — every phase checked.
    body = fake_tracker.comments[-1][2]
    assert "Done — issue closed" in body
    assert "- [ ]" not in body  # nothing left unchecked at DONE


# --------------------------------------------------------------------------- #
# ESCALATE_PREPUSH — agent stalled/errored before any push: hand to a human.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("thread_state", ["error", "idle"])
def test_escalate_prepush_relabels_and_notifies(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config, thread_state
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", thread_state))

    result = _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, commit=None), make_config()
    )

    assert result.action.value == "escalate_prepush"
    assert result.terminal is True
    assert fake_tracker.closed == []  # NOT closed — needs a human
    labels = _labels(fake_tracker)
    assert ("remove", "infra", 7, "agent-in-progress") in labels
    assert ("add", "infra", 7, READY_FOR_HUMAN) in labels
    assert _kinds(fake_notifier) == [KIND_NEEDS_HUMAN]


# --------------------------------------------------------------------------- #
# FREEZE_ESCALATE — pushed, CI red, fix-forward budget exhausted: freeze + page.
# --------------------------------------------------------------------------- #
def test_freeze_escalate_relabels_and_notifies(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "idle"))
    fake_ci.set_status("infra", "badc0de", CIStatus.RED)
    config = make_config(fix_forward_max_attempts=3)

    # attempts already at the cap -> budget exhausted -> FREEZE_ESCALATE.
    result = _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, commit="badc0de", fix_forward_attempts=3), config
    )

    assert result.action.value == "freeze_escalate"
    assert result.terminal is True
    assert fake_tracker.closed == []
    labels = _labels(fake_tracker)
    assert ("remove", "infra", 7, "agent-in-progress") in labels
    assert ("add", "infra", 7, READY_FOR_HUMAN) in labels
    assert _kinds(fake_notifier) == [KIND_FROZEN]


# --------------------------------------------------------------------------- #
# FIX_FORWARD — pushed, CI red, budget remaining: corrective turn, stay in flight.
# --------------------------------------------------------------------------- #
def test_fix_forward_dispatches_corrective_turn(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "idle"))
    fake_ci.set_status("infra", "badc0de", CIStatus.RED)
    config = make_config(fix_forward_max_attempts=5)

    result = _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, commit="badc0de", fix_forward_attempts=1), config
    )

    assert result.action.value == "fix_forward"
    assert result.terminal is False
    # A corrective turn was dispatched against the same repo/issue.
    assert len(fake_t3.dispatched) == 1
    assert (fake_t3.dispatched[0]["repo"], fake_t3.dispatched[0]["issue"]) == ("infra", 7)
    # Attempt count advanced and is surfaced on the result for the caller's
    # bookkeeping on the next tick.
    assert result.fix_forward_attempts == 2
    # Not terminal: no close, no ready-for-human, no doorbell.
    assert fake_tracker.closed == []
    assert ("add", "infra", 7, READY_FOR_HUMAN) not in _labels(fake_tracker)
    assert fake_notifier.sent == []


def test_fix_forward_updates_thread_id_to_corrective_turn(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    # The corrective dispatch spawns a new thread; the result carries the new id
    # so the next tick polls the right thread.
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "idle"))
    fake_ci.set_status("infra", "badc0de", CIStatus.RED)
    result = _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, thread_id="thread-old", commit="badc0de"), make_config()
    )
    assert result.thread_id == "thread-0"  # FakeT3Client hands back thread-0
    assert result.thread_id != "thread-old"


def test_fix_forward_note_appears_in_checklist(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "idle"))
    fake_ci.set_status("infra", "badc0de", CIStatus.RED)
    _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, commit="badc0de", fix_forward_attempts=1), make_config()
    )
    body = fake_tracker.comments[-1][2]
    assert "Fix-forward" in body


# --------------------------------------------------------------------------- #
# Unknown / unrecognised thread status folds to "keep waiting" (fail-safe).
# --------------------------------------------------------------------------- #
def test_unknown_thread_status_waits(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "provisioning"))  # not a known status
    result = _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, commit=None), make_config()
    )
    # Unknown status must not escalate or close — treat as "no status yet".
    assert result.action.value == "wait"
    assert fake_tracker.closed == []
    assert fake_notifier.sent == []


# --------------------------------------------------------------------------- #
# Terminal cleanup only happens once / cleanly: a terminal tick posts exactly
# one checklist comment (no double-commenting on the way out).
# --------------------------------------------------------------------------- #
def test_terminal_tick_posts_exactly_one_checklist(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "idle"))
    fake_ci.set_status("infra", "cafef00d", CIStatus.GREEN)
    _watcher(fake_t3, fake_tracker, fake_ci, fake_notifier).tick(
        _run(issue, commit="cafef00d"), make_config()
    )
    assert len(fake_tracker.comments) == 1


# --------------------------------------------------------------------------- #
# CI status is only queried when something is pushed (don't hit CI for an
# unpushed run — there's no commit to check).
# --------------------------------------------------------------------------- #
def test_ci_not_queried_when_nothing_pushed(
    fake_t3, fake_tracker, fake_notifier, make_issue, make_config
):
    class ExplodingCI:
        def status(self, repo, commit):
            raise AssertionError("CI must not be queried with no pushed commit")

    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "running"))
    result = watcher.Watcher(
        t3_client=fake_t3,
        tracker=fake_tracker,
        ci_watcher=ExplodingCI(),
        notifier=fake_notifier,
    ).tick(_run(issue, commit=None), make_config())
    assert result.action.value == "wait"


# --------------------------------------------------------------------------- #
# ready-for-human label is configurable.
# --------------------------------------------------------------------------- #
def test_ready_for_human_label_is_configurable(
    fake_t3, fake_tracker, fake_ci, fake_notifier, make_issue, make_config
):
    issue = make_issue(number=7, repo="infra")
    fake_t3.set_snapshot(_snapshot("thread-0", "error"))
    w = watcher.Watcher(
        t3_client=fake_t3,
        tracker=fake_tracker,
        ci_watcher=fake_ci,
        notifier=fake_notifier,
        ready_for_human_label="needs-eyes",
    )
    w.tick(_run(issue, commit=None), make_config())
    assert ("add", "infra", 7, "needs-eyes") in _labels(fake_tracker)
