"""Run state machine: assembled ``RunState`` -> next ``Action`` (ADR-0002).

This is the heart of the AFK loop's per-issue control: each tick the loop
assembles a :class:`~app.afk.types.RunState` (thread liveness from the
orchestration snapshot, CI verdict from the watcher, plus its own ``pushed`` /
``fix_forward_attempts`` / ``elapsed_seconds`` bookkeeping) and calls
:func:`next_action` to decide what to do next.

The function is **pure** — it reads only its two arguments, never the clock, the
network, or any global. That keeps the lifecycle policy a plain decision table
the test suite can exhaust combinatorially; the loop owns all the I/O (closing
issues, dispatching corrective turns, escalating) based on the Action returned.

The decision table (first match wins):

  * pushed AND thread RUNNING,
    within the defer ceiling                    -> WAIT
      The turn is still working. A verdict-driven action now would either close
      the issue under a live turn (which keeps a clone and push credentials) or
      start a second turn on the same issue.
  * pushed AND thread RUNNING,
    past the defer ceiling                      -> CLOSE_SUCCESS on green,
                                                   FREEZE_ESCALATE on red
      The fixer runs with no job timeout by design, so deferring needs its own
      ceiling or a turn that wedges after pushing holds the lock forever. Past
      it, a green verdict is released (the commit landed) and a red one goes to a
      human rather than starting a second turn beside a live one.
  * pushed AND CI green                         -> CLOSE_SUCCESS
      The run is healthy and verified; close the issue. The thread's own status
      is irrelevant once a pushed commit is green.
  * pushed AND CI red, budget remaining         -> FIX_FORWARD
      A pushed commit broke CI. Dispatch another corrective turn — but only
      while BOTH budgets hold: ``fix_forward_attempts < fix_forward_max_attempts``
      AND ``elapsed_seconds < fix_forward_max_seconds`` (strict; at/over either
      bound is exhausted).
  * pushed AND CI red, budget exhausted         -> FREEZE_ESCALATE
      Out of fix-forward attempts or wall-clock; stop churning and hand to a
      human with the broken commit left in place.
  * not pushed AND thread VANISHED,
    redispatch budget remaining                 -> REDISPATCH
      The runner has no record of the job, which is what a pod roll looks like
      from here: jobs live in an in-process dict, so replacing the process loses
      them. Nothing was pushed, so nothing is half-done and the run can simply
      start again. Bounded by ``redispatch_attempts < max_redispatch_attempts``
      (default 1) — a job that disappears twice is not a restart.
  * not pushed AND thread VANISHED,
    redispatch budget exhausted                 -> ESCALATE_PREPUSH
  * not pushed AND thread ERROR/IDLE            -> ESCALATE_PREPUSH
      The agent will never reach green: it errored, or its turn finished /
      stalled with nothing pushed. There is no pushed commit to fix forward, so
      escalate before-push (a different remediation path than FREEZE_ESCALATE).
      ERROR is deliberately NOT re-dispatched: the runner is asserting the turn
      failed, and an identical fresh turn has no particular reason to survive.
  * everything else                             -> WAIT
      Still in flight: working toward a first push (thread running / unknown), or
      pushed with CI not yet decided. Poll again next tick.
"""
from .types import Action, CIStatus, Config, RunState, ThreadStatus

# Thread states that mean the agent is finished with this turn — it will not push
# any further on its own. Reaching one of these with nothing pushed is terminal
# (escalate), whereas RUNNING / None (no snapshot entry yet) means keep waiting.
_TERMINAL_THREAD_STATES: frozenset[ThreadStatus] = frozenset(
    {ThreadStatus.ERROR, ThreadStatus.IDLE}
)


def next_action(state: RunState, config: Config) -> Action:
    """Decide the next :class:`Action` for one issue's run.

    Pure and total: every reachable ``(thread_status, ci_status, pushed,
    attempts, elapsed)`` combination maps to exactly one Action via the table in
    the module docstring. See that table for the rationale of each branch.
    """
    if state.pushed:
        # The agent is still working. Nothing a CI verdict would tell us to do is
        # safe yet: closing would end the run under a live turn that still holds
        # a clone and push rights, and fix-forward would put a second agent on
        # the same issue and branch. So wait for it to stop — but not forever.
        #
        # The fixer dispatches with no timeout on purpose, so there is no clock
        # on the turn to inherit; past ``close_defer_max_seconds`` the turn stops
        # being treated as authoritative. Releasing a green verdict then closes
        # the run, and releasing a red one escalates rather than fixing forward,
        # because a second agent must not join a turn that is still live.
        if state.thread_status is ThreadStatus.RUNNING:
            if state.elapsed_seconds < config.close_defer_max_seconds:
                return Action.WAIT
            if state.ci_status is CIStatus.RED:
                return Action.FREEZE_ESCALATE
            if state.ci_status is not CIStatus.GREEN:
                return Action.WAIT

        # A commit is out and the turn has stopped; the CI verdict decides.
        if state.ci_status is CIStatus.GREEN:
            return Action.CLOSE_SUCCESS
        if state.ci_status is CIStatus.RED:
            return (
                Action.FIX_FORWARD
                if _fix_forward_budget_remaining(state, config)
                else Action.FREEZE_ESCALATE
            )
        # CI pending / not yet reported -> wait for the verdict.
        return Action.WAIT

    # Nothing pushed, and the runner has no record of the job: the process
    # holding it was almost certainly replaced mid-run. Nothing is half-done, so
    # start over rather than waking a human for a pod roll — bounded, because a
    # second disappearance is evidence of something a restart will not fix.
    if state.thread_status is ThreadStatus.VANISHED:
        return (
            Action.REDISPATCH
            if state.redispatch_attempts < config.max_redispatch_attempts
            else Action.ESCALATE_PREPUSH
        )

    # Nothing pushed yet. If the turn is over (errored or gone idle) the run can
    # never reach green on its own -> escalate before-push; otherwise it is still
    # working toward a first push -> wait.
    if state.thread_status in _TERMINAL_THREAD_STATES:
        return Action.ESCALATE_PREPUSH
    return Action.WAIT


def _fix_forward_budget_remaining(state: RunState, config: Config) -> bool:
    """True while another fix-forward turn is allowed.

    Both bounds must hold (strict ``<``): the run has spent fewer than
    ``fix_forward_max_attempts`` corrective turns AND fewer than
    ``fix_forward_max_seconds`` of wall-clock. Hitting either cap exhausts the
    budget.
    """
    return (
        state.fix_forward_attempts < config.fix_forward_max_attempts
        and state.elapsed_seconds < config.fix_forward_max_seconds
    )
