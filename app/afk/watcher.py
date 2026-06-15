"""CronJob entrypoint: drive ONE in-flight AFK run by a single tick.

The watcher is the *second half* of the loop — the part that drives a run the
poller already started through to a terminal state. Given one in-flight run
(``InFlightRun``: the issue, the T3 thread to poll, the pushed commit if any,
and the fix-forward bookkeeping), one ``tick``:

  1. **assemble a ``RunState``** from the live edges + the run's bookkeeping:
       * ``thread_status`` — from ``t3_client.snapshot()``, by finding this run's
         thread and mapping its ``latestTurn.state`` (``completed`` → idle,
         ``running``/``in_progress``/``pending`` → running, ``errored`` → error)
         to a ``ThreadStatus`` (missing thread, no turn yet, or any unrecognised
         state folds to ``None`` → "no status yet" → the state machine WAITs; we
         never escalate or close on a status we don't understand);
       * ``ci_status`` — ``ci_watcher.status(repo, commit)`` *only* when a commit
         is pushed (no commit ⇒ nothing to check ⇒ ``None``);
       * ``pushed`` / ``fix_forward_attempts`` / ``elapsed_seconds`` — straight
         from the run.
  2. **decide** via the pure ``run_state_machine.next_action`` (it owns the
     lifecycle policy; the watcher owns only the I/O the decision implies).
  3. **act** on the returned ``Action``:
       * ``CLOSE_SUCCESS`` → ``tracker.close`` + drop the in-progress label +
         DONE checklist + ``done`` doorbell. The run landed.
       * ``ESCALATE_PREPUSH`` / ``FREEZE_ESCALATE`` → drop the in-progress label,
         add the ``ready-for-human`` label, post the checklist, ring the
         ``needs-human`` / ``frozen`` doorbell. The run is handed to a human; the
         issue is left OPEN (not closed) with the work in place.
       * ``FIX_FORWARD`` → dispatch a corrective turn (``t3_client.dispatch``),
         bump the fix-forward attempt count, refresh the checklist, and keep the
         run in flight (NOT terminal: no label churn, no doorbell — the notifier
         only speaks terminal kinds). The new thread id rides back on the result
         so the next tick polls the corrective turn.
       * ``WAIT`` → just refresh the progress checklist and keep waiting.

Every adapter (T3, tracker, CI, notifier) is injected behind a structural
Protocol, so production wires the real clients and the tests wire the in-memory
fakes; this module opens no socket and reads no message bodies. (The pilot keeps
T3 ``state.sqlite`` message-body reads out of the core loop — snapshot status +
CI status are all the state machine needs — so this watcher never execs into the
pod; that observability nicety is a separate, optional concern.)

DISABLED BY DEFAULT applies transitively: the poller never starts a run while
the loop is off (``config.kill_switch`` / empty allowlist — see ``config.py``),
so with the shipped defaults there is never an ``InFlightRun`` to tick.
"""
from dataclasses import dataclass
from typing import Protocol

from . import phase_checklist, run_state_machine
from .notifier import KIND_DONE, KIND_FROZEN, KIND_NEEDS_HUMAN
from .poller import T3Port as _DispatchPort  # dispatch(repo, issue, prompt) -> id
from .types import Action, CIStatus, Config, Issue, Phase, RunState, ThreadStatus

# T3 ``latestTurn.state`` -> ThreadStatus. The real snapshot reports a thread's
# liveness as the state of its latest turn (verified against t3-afk v0.0.27):
# ``completed`` == the turn finished cleanly (agent is idle, awaiting input);
# any not-yet-finished state (``running``/``in_progress``/``pending``/``queued``/
# ``pendingInit``) == still working; ``errored`` == the turn failed. Anything not
# in here (a state T3 adds later, or a malformed/absent entry) maps to None —
# "no usable status yet" — so the state machine waits rather than acting on
# something it can't interpret.
_THREAD_STATUS_BY_STRING: dict[str, ThreadStatus] = {
    "completed": ThreadStatus.IDLE,
    "running": ThreadStatus.RUNNING,
    "in_progress": ThreadStatus.RUNNING,
    "pending": ThreadStatus.RUNNING,
    "queued": ThreadStatus.RUNNING,
    "pendingInit": ThreadStatus.RUNNING,
    "errored": ThreadStatus.ERROR,
}

# Action -> the terminal doorbell kind to ring. Only the terminal actions appear;
# WAIT / FIX_FORWARD are non-terminal and ring nothing (the notifier rejects a
# non-terminal kind on purpose — see ``notifier.TERMINAL_KINDS``).
_TERMINAL_KIND_BY_ACTION: dict[Action, str] = {
    Action.CLOSE_SUCCESS: KIND_DONE,
    Action.ESCALATE_PREPUSH: KIND_NEEDS_HUMAN,
    Action.FREEZE_ESCALATE: KIND_FROZEN,
}

# Default label applied when a run is handed back to a human. Mirrors the
# tracker's ``ready-for-agent`` convention; overridable per-Watcher.
DEFAULT_READY_FOR_HUMAN_LABEL = "ready-for-human"


# --------------------------------------------------------------------------- #
# Injected adapter Protocols — structural, so the real clients and the test
# fakes both satisfy them with no subclassing. Only the methods the watcher
# actually calls appear. ``DispatchPort`` is reused from ``poller``.
# --------------------------------------------------------------------------- #
class SnapshotPort(_DispatchPort, Protocol):
    """T3 surface the watcher needs: ``dispatch`` (for the corrective turn) plus
    ``snapshot`` (for thread liveness)."""

    def snapshot(self) -> dict: ...


class TrackerPort(Protocol):
    """The slice of ``tracker.Tracker`` the watch tick needs."""

    def add_label(self, repo: str, issue: int, label: str) -> None: ...
    def remove_label(self, repo: str, issue: int, label: str) -> None: ...
    def comment(self, repo: str, issue: int, body: str) -> None: ...
    def close(self, repo: str, issue: int) -> None: ...


class CIPort(Protocol):
    """The slice of ``ci_watcher.CIWatcher`` the watch tick needs."""

    def status(self, repo: str, commit: str) -> CIStatus: ...


class NotifierPort(Protocol):
    """The slice of ``notifier.Notifier`` the watch tick needs."""

    def notify(self, kind: str, issue: Issue, thread_id: str | None, detail: str) -> None: ...


@dataclass
class InFlightRun:
    """One run the watcher is driving, as the loop tracks it between ticks.

    ``thread_id`` is the T3 thread to poll this tick; ``commit`` is the pushed
    commit CI watches (``None`` until the agent has pushed). ``fix_forward_attempts``
    and ``elapsed_seconds`` are the loop's own bookkeeping, fed straight into the
    assembled ``RunState`` — ``pushed`` is derived as ``commit is not None``.
    """

    issue: Issue
    thread_id: str
    commit: str | None
    fix_forward_attempts: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class TickResult:
    """The outcome of one watch tick.

    ``action`` is the state machine's verdict; ``terminal`` is True iff the run
    reached an end state (closed or handed to a human) and should no longer be
    ticked. ``thread_id`` / ``fix_forward_attempts`` carry the (possibly updated)
    bookkeeping the caller threads into the next ``InFlightRun`` — they change
    only on a FIX_FORWARD (new corrective thread, incremented attempts) and are
    otherwise echoed back unchanged.
    """

    action: Action
    terminal: bool
    thread_id: str
    fix_forward_attempts: int


class Watcher:
    """Drives one in-flight run per ``tick`` over injected adapters.

    The three escalation-vs-success decisions live in the pure
    ``run_state_machine``; this class only performs the I/O each decision
    implies. ``ready_for_human_label`` is the label stamped on a run handed back
    to a human (default :data:`DEFAULT_READY_FOR_HUMAN_LABEL`).
    """

    def __init__(
        self,
        t3_client: SnapshotPort,
        tracker: TrackerPort,
        ci_watcher: CIPort,
        notifier: NotifierPort,
        ready_for_human_label: str = DEFAULT_READY_FOR_HUMAN_LABEL,
    ) -> None:
        self._t3 = t3_client
        self._tracker = tracker
        self._ci = ci_watcher
        self._notifier = notifier
        self._ready_for_human_label = ready_for_human_label

    def tick(self, run: InFlightRun, config: Config) -> TickResult:
        """Drive ``run`` one step (see module docstring)."""
        state = self._assemble_state(run)
        action = run_state_machine.next_action(state, config)

        if action is Action.CLOSE_SUCCESS:
            return self._close_success(run, config)
        if action in (Action.ESCALATE_PREPUSH, Action.FREEZE_ESCALATE):
            return self._escalate(run, state, action, config)
        if action is Action.FIX_FORWARD:
            return self._fix_forward(run, state)
        # WAIT: still in flight — just show progress and poll again next tick.
        return self._wait(run, state, action)

    # ----------------------------------------------------------------- #
    # RunState assembly.
    # ----------------------------------------------------------------- #
    def _assemble_state(self, run: InFlightRun) -> RunState:
        thread_status = self._thread_status(run.thread_id)
        # Only fold CI when there's a commit to check — an unpushed run has no
        # pipeline, and we must not query CI (the assertion in the tests, and
        # avoiding a needless API call, both rely on this).
        ci_status = (
            self._ci.status(run.issue.repo, run.commit)
            if run.commit is not None
            else None
        )
        return RunState(
            thread_status=thread_status,
            ci_status=ci_status,
            pushed=run.commit is not None,
            fix_forward_attempts=run.fix_forward_attempts,
            elapsed_seconds=run.elapsed_seconds,
        )

    def _thread_status(self, thread_id: str) -> ThreadStatus | None:
        """This thread's liveness from the fleet snapshot, or ``None`` when the
        thread is absent, has no turn yet, or its ``latestTurn.state`` is one we
        don't recognise. Liveness is the state of the thread's latest turn (the
        real snapshot shape), not a top-level ``status`` field."""
        for thread in self._t3.snapshot().get("threads", []):
            if thread.get("id") == thread_id:
                latest_turn = thread.get("latestTurn") or {}
                return _THREAD_STATUS_BY_STRING.get(latest_turn.get("state"))
        return None

    # ----------------------------------------------------------------- #
    # Per-action handlers.
    # ----------------------------------------------------------------- #
    def _close_success(self, run: InFlightRun, config: Config) -> TickResult:
        """Landed: close the issue, drop the lock, post DONE, ring the doorbell."""
        self._post_checklist(run, Phase.DONE)
        self._tracker.remove_label(
            run.issue.repo, run.issue.number, config.in_progress_label
        )
        self._tracker.close(run.issue.repo, run.issue.number)
        self._notify(run, Action.CLOSE_SUCCESS, "Run landed: pushed and CI green.")
        return _terminal(Action.CLOSE_SUCCESS, run)

    def _escalate(
        self, run: InFlightRun, state: RunState, action: Action, config: Config
    ) -> TickResult:
        """Hand back to a human: drop the lock, add ready-for-human, post the
        checklist, ring the matching doorbell. The issue stays OPEN."""
        self._post_checklist(run, _phase_for(state))
        self._tracker.remove_label(
            run.issue.repo, run.issue.number, config.in_progress_label
        )
        self._tracker.add_label(
            run.issue.repo, run.issue.number, self._ready_for_human_label
        )
        self._notify(run, action, _escalation_detail(action, state))
        return _terminal(action, run)

    def _fix_forward(self, run: InFlightRun, state: RunState) -> TickResult:
        """CI red with budget left: dispatch a corrective turn and stay in flight.

        Not terminal — no doorbell (the notifier only speaks terminal kinds) and
        no label churn (the in-progress lock stays put). The corrective dispatch
        spawns a fresh thread; its id and the incremented attempt count ride back
        so the next tick tracks the right thread.
        """
        attempts = run.fix_forward_attempts + 1
        new_thread_id = self._t3.dispatch(
            run.issue.repo, run.issue.number, _fix_forward_prompt(run)
        )
        self._post_checklist(run, Phase.CI, fix_forward_attempts=attempts)
        return TickResult(
            action=Action.FIX_FORWARD,
            terminal=False,
            thread_id=new_thread_id,
            fix_forward_attempts=attempts,
        )

    def _wait(self, run: InFlightRun, state: RunState, action: Action) -> TickResult:
        """Still working: refresh the progress checklist, change nothing else."""
        self._post_checklist(run, _phase_for(state))
        return TickResult(
            action=action,
            terminal=False,
            thread_id=run.thread_id,
            fix_forward_attempts=run.fix_forward_attempts,
        )

    # ----------------------------------------------------------------- #
    # I/O helpers.
    # ----------------------------------------------------------------- #
    def _post_checklist(
        self, run: InFlightRun, phase: Phase, *, fix_forward_attempts: int | None = None
    ) -> None:
        attempts = run.fix_forward_attempts if fix_forward_attempts is None else fix_forward_attempts
        body = phase_checklist.render(
            phase,
            {
                "repo": run.issue.repo,
                "issue": run.issue.number,
                "thread_id": run.thread_id,
                "fix_forward_attempts": attempts,
            },
        )
        self._tracker.comment(run.issue.repo, run.issue.number, body)

    def _notify(self, run: InFlightRun, action: Action, detail: str) -> None:
        self._notifier.notify(
            _TERMINAL_KIND_BY_ACTION[action], run.issue, run.thread_id, detail
        )


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #
def _terminal(action: Action, run: InFlightRun) -> TickResult:
    """A terminal :class:`TickResult` echoing the run's bookkeeping unchanged."""
    return TickResult(
        action=action,
        terminal=True,
        thread_id=run.thread_id,
        fix_forward_attempts=run.fix_forward_attempts,
    )


def _phase_for(state: RunState) -> Phase:
    """Best-effort current lifecycle phase from the evidence in ``state``.

    The checklist is decoration only (the loop reads no agent message bodies), so
    this maps the observable signals — pushed? CI verdict? — onto the closest
    phase: nothing pushed ⇒ still working toward the implementation (GREEN);
    pushed ⇒ the CI phase is where attention sits until it goes green. A green CI
    is rendered as DONE by the close path, not here.
    """
    if not state.pushed:
        return Phase.GREEN
    if state.ci_status is CIStatus.GREEN:
        return Phase.DEPLOYED
    return Phase.CI


def _escalation_detail(action: Action, state: RunState) -> str:
    """Human-readable escalation reason for the doorbell + logs (never parsed)."""
    if action is Action.ESCALATE_PREPUSH:
        return (
            "Agent stalled or errored before pushing any commit "
            f"(thread {state.thread_status.value if state.thread_status else 'unknown'}). "
            "Handed back for a human."
        )
    return (
        "Fix-forward budget exhausted with CI still red "
        f"({state.fix_forward_attempts} attempts, {state.elapsed_seconds:.0f}s). "
        "Frozen for a human."
    )


def _fix_forward_prompt(run: InFlightRun) -> str:
    """The corrective-turn prompt: point the agent at the red CI on its commit."""
    return (
        f"CI is RED on your pushed commit {run.commit} for issue #{run.issue.number} "
        f"in `{run.issue.repo}`. Investigate the failing run, fix the cause, and "
        f"push the fix to master. Then watch CI again until it is green."
    )
