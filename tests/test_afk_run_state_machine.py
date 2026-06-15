"""Tests for ``app.afk.run_state_machine.next_action`` — the pure decision
function that turns one assembled ``RunState`` into the next ``Action``.

The function encodes ADR-0002's run lifecycle:

  * healthy (pushed AND CI green)                 -> CLOSE_SUCCESS
  * cannot reach green before push (errored /
    stalled with nothing pushed)                  -> ESCALATE_PREPUSH
  * pushed but CI red, budget remaining           -> FIX_FORWARD
  * pushed but CI red, budget exhausted           -> FREEZE_ESCALATE
  * anything still in flight                       -> WAIT

It is PURE: no I/O, no clock, no globals — it reads only its two arguments, so
every case is a plain table assertion. ``make_config`` / ``make_run_state`` come
from ``conftest.py`` (config defaults to ENABLED, run state to a fresh dispatch).
"""
import pytest

from app.afk.run_state_machine import next_action
from app.afk.types import Action, CIStatus, ThreadStatus


# --------------------------------------------------------------------------- #
# Healthy terminal: pushed + CI green -> close, regardless of thread status.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "thread_status",
    [ThreadStatus.RUNNING, ThreadStatus.IDLE, ThreadStatus.ERROR, None],
)
def test_pushed_and_green_closes_success(make_config, make_run_state, thread_status):
    state = make_run_state(
        thread_status=thread_status, ci_status=CIStatus.GREEN, pushed=True
    )
    assert next_action(state, make_config()) is Action.CLOSE_SUCCESS


# --------------------------------------------------------------------------- #
# Pre-push escalation: nothing pushed and the turn is no longer going to push
# (errored, or finished/stalled clean) -> hand back to a human.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("thread_status", [ThreadStatus.ERROR, ThreadStatus.IDLE])
@pytest.mark.parametrize("ci_status", [None, CIStatus.PENDING])
def test_not_pushed_terminal_thread_escalates_prepush(
    make_config, make_run_state, thread_status, ci_status
):
    state = make_run_state(
        thread_status=thread_status, ci_status=ci_status, pushed=False
    )
    assert next_action(state, make_config()) is Action.ESCALATE_PREPUSH


# --------------------------------------------------------------------------- #
# Still working toward a first push -> WAIT (not yet an escalation).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("thread_status", [ThreadStatus.RUNNING, None])
@pytest.mark.parametrize("ci_status", [None, CIStatus.PENDING])
def test_not_pushed_in_flight_waits(
    make_config, make_run_state, thread_status, ci_status
):
    state = make_run_state(
        thread_status=thread_status, ci_status=ci_status, pushed=False
    )
    assert next_action(state, make_config()) is Action.WAIT


# --------------------------------------------------------------------------- #
# Pushed, CI not yet decided -> WAIT for the verdict, whatever the thread does.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "thread_status",
    [ThreadStatus.RUNNING, ThreadStatus.IDLE, ThreadStatus.ERROR, None],
)
@pytest.mark.parametrize("ci_status", [None, CIStatus.PENDING])
def test_pushed_ci_pending_waits(
    make_config, make_run_state, thread_status, ci_status
):
    state = make_run_state(
        thread_status=thread_status, ci_status=ci_status, pushed=True
    )
    assert next_action(state, make_config()) is Action.WAIT


# --------------------------------------------------------------------------- #
# Pushed + CI red: fix-forward while BOTH budgets remain, else freeze.
# Boundaries are strict-less-than on attempts AND elapsed; at/over either freezes.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("attempts", "elapsed", "expected"),
    [
        # fresh red, plenty of budget -> fix forward
        (0, 0.0, Action.FIX_FORWARD),
        (1, 10.0, Action.FIX_FORWARD),
        # one attempt below the cap, well inside the clock -> still fix forward
        (4, 3599.0, Action.FIX_FORWARD),
        # attempts hit the cap (5) -> freeze
        (5, 0.0, Action.FREEZE_ESCALATE),
        (6, 0.0, Action.FREEZE_ESCALATE),
        # clock hits the cap (3600s) -> freeze even with attempts to spare
        (0, 3600.0, Action.FREEZE_ESCALATE),
        (0, 7200.0, Action.FREEZE_ESCALATE),
        # both exhausted -> freeze
        (5, 3600.0, Action.FREEZE_ESCALATE),
    ],
)
def test_pushed_red_fix_forward_until_budget_exhausted(
    make_config, make_run_state, attempts, elapsed, expected
):
    state = make_run_state(
        thread_status=ThreadStatus.IDLE,
        ci_status=CIStatus.RED,
        pushed=True,
        fix_forward_attempts=attempts,
        elapsed_seconds=elapsed,
    )
    assert next_action(state, make_config()) is expected


# --------------------------------------------------------------------------- #
# Fix-forward budget is honoured from config, not hardcoded.
# --------------------------------------------------------------------------- #
def test_fix_forward_attempts_cap_comes_from_config(make_config, make_run_state):
    config = make_config(fix_forward_max_attempts=2)
    red = dict(thread_status=ThreadStatus.IDLE, ci_status=CIStatus.RED, pushed=True)
    assert next_action(make_run_state(fix_forward_attempts=1, **red), config) is Action.FIX_FORWARD
    assert next_action(make_run_state(fix_forward_attempts=2, **red), config) is Action.FREEZE_ESCALATE


def test_fix_forward_seconds_cap_comes_from_config(make_config, make_run_state):
    config = make_config(fix_forward_max_seconds=120)
    red = dict(thread_status=ThreadStatus.IDLE, ci_status=CIStatus.RED, pushed=True)
    assert next_action(make_run_state(elapsed_seconds=119.0, **red), config) is Action.FIX_FORWARD
    assert next_action(make_run_state(elapsed_seconds=120.0, **red), config) is Action.FREEZE_ESCALATE


# --------------------------------------------------------------------------- #
# A red CI on a pushed commit while the thread is still RUNNING a fix is, per
# spec, keyed only on (pushed AND red) + budget — thread status doesn't gate it.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "thread_status",
    [ThreadStatus.RUNNING, ThreadStatus.IDLE, ThreadStatus.ERROR, None],
)
def test_pushed_red_with_budget_fixes_forward_for_any_thread_status(
    make_config, make_run_state, thread_status
):
    state = make_run_state(
        thread_status=thread_status,
        ci_status=CIStatus.RED,
        pushed=True,
        fix_forward_attempts=0,
        elapsed_seconds=0.0,
    )
    assert next_action(state, make_config()) is Action.FIX_FORWARD


# --------------------------------------------------------------------------- #
# Full cross-product sanity sweep: next_action is TOTAL — it returns a real
# Action for every reachable combination, and matches the reference table.
# --------------------------------------------------------------------------- #
def _expected(thread_status, ci_status, pushed):
    """Reference implementation of the decision table, written independently of
    the module under test, to cross-check every combination."""
    if pushed and ci_status is CIStatus.GREEN:
        return Action.CLOSE_SUCCESS
    if pushed and ci_status is CIStatus.RED:
        return Action.FIX_FORWARD  # budget always available in this sweep
    if not pushed and thread_status in (ThreadStatus.ERROR, ThreadStatus.IDLE):
        return Action.ESCALATE_PREPUSH
    return Action.WAIT


@pytest.mark.parametrize(
    "thread_status",
    [ThreadStatus.RUNNING, ThreadStatus.IDLE, ThreadStatus.ERROR, None],
)
@pytest.mark.parametrize("ci_status", [None, CIStatus.PENDING, CIStatus.GREEN, CIStatus.RED])
@pytest.mark.parametrize("pushed", [True, False])
def test_decision_table_is_total(
    make_config, make_run_state, thread_status, ci_status, pushed
):
    state = make_run_state(
        thread_status=thread_status,
        ci_status=ci_status,
        pushed=pushed,
        fix_forward_attempts=0,
        elapsed_seconds=0.0,
    )
    result = next_action(state, make_config())
    assert isinstance(result, Action)
    assert result is _expected(thread_status, ci_status, pushed)
