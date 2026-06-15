"""Dispatch policy — the PURE gate deciding which ready issues to run *now*.

``select_dispatchable`` is the loop's first decision each tick: given every
issue the tracker reported ready, the loop config, and the set of repos that
already have an agent in flight, it returns the ordered list of issues to
dispatch this round. It does **no IO** — no tracker calls, no T3, no clock — so
it is exhaustively unit-testable and the loop stays a thin shell around it.

What it encapsulates (the dispatch predicate from the AFK pipeline design doc):

  * **Kill switch** — ``config.kill_switch`` short-circuits to ``[]`` before any
    per-issue work. The whole loop ships disabled; this is the master off.
  * **Trust gate** — only ``issue.labeled_by_trusted`` issues are eligible. On a
    private repo the gating label *is* the authorization, so an issue made ready
    by an untrusted/bot actor must never auto-run (prompt-injection defense).
  * **Allowlist** — ``issue.repo`` must be in ``config.allowlist``. An empty
    allowlist dispatches nothing even with the kill switch off (the deliberate
    two-gate posture: arming the loop takes both).
  * **Per-repo lock** — any repo already in ``in_flight_repos`` is skipped; at
    most one agent runs per repo (two would collide on the working tree).
  * **blocked_by gating** — ``issue.blocked_by`` lists the issue numbers of
    blockers that are still OPEN, so a non-empty list means "still blocked" and
    the issue is skipped.
  * **One-agent-per-repo within the batch** — because a repo hosts only one
    in-flight agent, a single call returns at most ONE decision per repo: the
    most-urgent eligible issue in that repo wins the slot. (A more-urgent issue
    that is itself ineligible does not consume the slot — the best *eligible*
    candidate does.)
  * **Priority ordering** — the surviving per-repo winners are returned
    lowest-``priority``-value-first (P0 before P1 before P2), with a deterministic
    tiebreaker (ascending issue number) so the output is a total, stable order
    independent of input order.

PRIORITY DIRECTION — lower ``Issue.priority`` runs first, matching tracker
conventions (P0/P1 are more urgent than P2) and ``Issue.priority``'s own
docstring in ``types``. The ordering lives here (the one place that consumes
``priority`` for dispatch), so this module is the source of truth for the
direction.

Pure: it never mutates its inputs — the caller's issue list, the config, and the
``in_flight_repos`` set are all left exactly as passed.
"""
from .types import Config, DispatchDecision, Issue


def select_dispatchable(
    issues: list[Issue],
    config: Config,
    in_flight_repos: set[str],
) -> list[DispatchDecision]:
    """Return the ordered issues to dispatch this tick (see module docstring).

    Empty when the kill switch is on, the allowlist excludes everything, or no
    issue clears every gate. At most one decision per repo; ordered
    lowest-priority-value-first (most urgent), ties broken by ascending issue
    number.
    """
    # Kill switch: master off-ramp, evaluated before any per-issue work.
    if config.kill_switch:
        return []

    allowlist = frozenset(config.allowlist)

    # First pass: keep only issues that clear every per-issue gate. Repos already
    # in flight are excluded here, so the lock is enforced before slot selection.
    eligible: list[Issue] = [
        issue
        for issue in issues
        if _is_eligible(issue, allowlist, in_flight_repos)
    ]

    # One slot per repo: among the eligible issues sharing a repo, the best
    # candidate (the global sort order) takes it; the rest are dropped this tick.
    best_per_repo: dict[str, Issue] = {}
    for issue in sorted(eligible, key=_dispatch_sort_key):
        best_per_repo.setdefault(issue.repo, issue)

    # Final order: the per-repo winners, most urgent first (total + stable).
    winners = sorted(best_per_repo.values(), key=_dispatch_sort_key)
    return [DispatchDecision(issue=issue, reason=_reason(issue)) for issue in winners]


# --------------------------------------------------------------------------- #
# Internals.
# --------------------------------------------------------------------------- #
def _is_eligible(
    issue: Issue,
    allowlist: frozenset[str],
    in_flight_repos: set[str],
) -> bool:
    """True iff the issue clears the trust, allowlist, per-repo-lock, and
    blocked_by gates. Kept boolean (not "which gate failed") because the policy
    only ever needs the survivors; reasons are attached to survivors only."""
    if not issue.labeled_by_trusted:
        return False
    if issue.repo not in allowlist:
        return False
    if issue.repo in in_flight_repos:
        return False
    if issue.blocked_by:  # non-empty == at least one OPEN blocker remains
        return False
    return True


def _dispatch_sort_key(issue: Issue) -> tuple[int, int]:
    """Sort key giving a total, deterministic order: lowest ``priority`` value
    first (P0 before P1 — most urgent wins), then lowest issue number as the
    tiebreaker so equal-priority issues never depend on input/iteration order."""
    return (issue.priority, issue.number)


def _reason(issue: Issue) -> str:
    """Human-readable justification, logged and surfaced in notifications, never
    parsed. Records that every gate passed and the priority that ordered it."""
    return (
        f"{issue.repo}#{issue.number}: eligible "
        f"(trusted, allowlisted, unblocked, repo free) — priority {issue.priority}"
    )
