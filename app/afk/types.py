"""Shared types for the AFK loop — the contract every module builds against.

Stdlib only (``dataclasses`` + ``enum``), matching the breakglass code: no
pydantic, modern ``X | None`` unions, precise field types. Every other module in
``app.afk`` imports its inputs/outputs from here so the pieces stay aligned; the
module-level docstrings in ``__init__`` list which functions consume which type.

Nothing here has behaviour — these are pure data carriers and closed enums. Keep
it that way: logic lives in ``dispatch_policy`` / ``run_state_machine`` / the
client modules, never on the dataclasses.
"""
from dataclasses import dataclass
from enum import Enum


# --------------------------------------------------------------------------- #
# Enums — closed vocabularies the state machine and clients speak in.
# --------------------------------------------------------------------------- #
class ThreadStatus(Enum):
    """Liveness of a T3 thread, as projected from the orchestration snapshot.

    ``RUNNING`` — the agent is still working the turn; ``IDLE`` — the turn
    finished cleanly (it has gone quiet); ``ERROR`` — the thread/turn failed.
    """

    RUNNING = "running"
    IDLE = "idle"
    ERROR = "error"


class CIStatus(Enum):
    """CI verdict for a pushed commit. ``PENDING`` covers both "no run yet" and
    "in progress" — the state machine waits on either."""

    PENDING = "pending"
    GREEN = "green"
    RED = "red"


class Phase(Enum):
    """Where a single issue's run is in its lifecycle. Ordered: each phase is a
    gate the run passes through on the way to ``DONE``. ``phase_checklist``
    renders these; the loop advances through them as evidence arrives."""

    WORKTREE = "worktree"      # isolated workspace created
    TESTS_RED = "tests_red"    # failing test written first (TDD red)
    GREEN = "green"            # implementation makes tests pass (TDD green)
    PUSHED = "pushed"          # commit(s) pushed to master
    CI = "ci"                  # CI pipeline running on the pushed commit
    DEPLOYED = "deployed"      # deploy/rollout reached the cluster
    DONE = "done"              # verified complete; issue can be closed


class Action(Enum):
    """The decision ``run_state_machine.next_action`` returns for one tick.

    ``WAIT`` — nothing to do yet, poll again; ``CLOSE_SUCCESS`` — run is green,
    CI passed, close the issue; ``ESCALATE_PREPUSH`` — the agent errored/stalled
    before pushing anything, hand back to a human; ``FIX_FORWARD`` — CI went red
    on a pushed commit, dispatch another corrective turn; ``FREEZE_ESCALATE`` —
    fix-forward budget exhausted (attempts or wall-clock), stop and escalate.
    """

    WAIT = "wait"
    CLOSE_SUCCESS = "close_success"
    ESCALATE_PREPUSH = "escalate_prepush"
    FIX_FORWARD = "fix_forward"
    FREEZE_ESCALATE = "freeze_escalate"


# --------------------------------------------------------------------------- #
# Data carriers.
# --------------------------------------------------------------------------- #
@dataclass
class Issue:
    """A tracker issue the loop might dispatch.

    ``labeled_by_trusted`` records whether the gating label was applied by a
    trusted identity — the loop must never dispatch an issue made ready by an
    untrusted actor (prompt-injection / drive-by). ``blocked_by`` lists issue
    numbers that must close first; ``priority`` orders the ready set (lower runs
    first, matching tracker conventions).
    """

    number: int
    repo: str
    labels: list[str]
    blocked_by: list[int]
    labeled_by_trusted: bool
    priority: int


@dataclass
class DispatchDecision:
    """An issue the dispatch policy selected to run now, with a human-readable
    ``reason`` (logged + surfaced in notifications, never parsed)."""

    issue: Issue
    reason: str


@dataclass
class Config:
    """Loop configuration. DISABLED BY DEFAULT — ``kill_switch=True`` and an
    empty ``allowlist`` mean a freshly-constructed Config dispatches nothing.
    Enabling is a deliberate manual step (see ``config.from_env`` /
    ``from_configmap``).
    """

    allowlist: list[str]
    kill_switch: bool
    in_progress_label: str = "agent-in-progress"
    ready_label: str = "ready-for-agent"
    budget_usd: float = 100.0
    fix_forward_max_attempts: int = 5
    fix_forward_max_seconds: int = 3600


@dataclass
class RunState:
    """Everything the state machine needs to decide one issue's next move.

    Assembled each tick from the orchestration snapshot (``thread_status``), the
    CI watcher (``ci_status``), and the loop's own bookkeeping (``pushed``,
    ``fix_forward_attempts``, ``elapsed_seconds``). ``thread_status`` /
    ``ci_status`` are ``None`` when not yet known (no snapshot entry / nothing
    pushed to check yet).
    """

    thread_status: ThreadStatus | None
    ci_status: CIStatus | None
    pushed: bool
    fix_forward_attempts: int
    elapsed_seconds: float
