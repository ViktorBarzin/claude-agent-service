"""Tests for ``app.afk.dispatch_policy.select_dispatchable`` — the pure gate that
turns a pile of ready issues into the ordered set the loop may dispatch *now*.

The function is PURE (no IO), so every test here is a plain in-memory call over
the fakes/factories in ``conftest`` (``make_issue`` / ``make_config``); nothing
touches a real T3 server, tracker, or cluster. The suite walks the full
dispatchability matrix — trust gate, allowlist, per-repo lock, blocked_by,
kill switch — plus the priority ordering and the one-agent-per-repo invariant.

Ordering contract under test: **higher ``priority`` first** (per the AFK module
spec), with a deterministic tiebreaker so the output is stable regardless of
input order. NOTE: ``Issue.priority``'s own docstring says "lower runs first";
this module follows the explicit dispatch-policy spec instead — see the module
docstring in ``dispatch_policy.py``.
"""
import itertools

import pytest

from app.afk import dispatch_policy
from app.afk.types import DispatchDecision, Issue


# --------------------------------------------------------------------------- #
# Helpers — keep assertions terse and intent-revealing.
# --------------------------------------------------------------------------- #
def _selected_numbers(decisions: list[DispatchDecision]) -> list[int]:
    """The issue numbers, in the order the policy returned them."""
    return [d.issue.number for d in decisions]


def _selected_set(decisions: list[DispatchDecision]) -> set[int]:
    return {d.issue.number for d in decisions}


# --------------------------------------------------------------------------- #
# Return shape & purity.
# --------------------------------------------------------------------------- #
def test_returns_list_of_dispatch_decisions(make_issue, make_config):
    issue = make_issue(number=7, repo="infra")
    decisions = dispatch_policy.select_dispatchable([issue], make_config(), set())
    assert isinstance(decisions, list)
    assert len(decisions) == 1
    assert isinstance(decisions[0], DispatchDecision)
    assert decisions[0].issue is issue
    assert isinstance(decisions[0].reason, str) and decisions[0].reason  # non-empty


def test_empty_input_yields_empty_output(make_config):
    assert dispatch_policy.select_dispatchable([], make_config(), set()) == []


def test_does_not_mutate_inputs(make_issue, make_config):
    issues = [make_issue(number=1, priority=0), make_issue(number=2, priority=9)]
    issues_snapshot = list(issues)
    config = make_config(allowlist=["infra"])
    in_flight: set[str] = set()

    dispatch_policy.select_dispatchable(issues, config, in_flight)

    # Caller's list (and its order) and the lock set are left untouched.
    assert issues == issues_snapshot
    assert [i.number for i in issues] == [1, 2]
    assert in_flight == set()
    assert config.allowlist == ["infra"]


def test_decision_wraps_the_same_issue_object(make_issue, make_config):
    issue = make_issue(number=42)
    [decision] = dispatch_policy.select_dispatchable([issue], make_config(), set())
    assert decision.issue is issue  # identity, not a copy


# --------------------------------------------------------------------------- #
# Kill switch — highest-precedence short-circuit.
# --------------------------------------------------------------------------- #
def test_kill_switch_returns_empty_even_with_perfect_issues(make_issue, make_config):
    issues = [make_issue(number=n, repo="infra") for n in range(1, 6)]
    config = make_config(allowlist=["infra"], kill_switch=True)
    assert dispatch_policy.select_dispatchable(issues, config, set()) == []


def test_kill_switch_off_dispatches(make_issue, make_config):
    issue = make_issue(repo="infra")
    config = make_config(allowlist=["infra"], kill_switch=False)
    assert len(dispatch_policy.select_dispatchable([issue], config, set())) == 1


def test_production_default_config_dispatches_nothing(make_issue):
    """The shipped default (kill switch ON, empty allowlist) is inert: even a
    pristine, trusted issue is never selected."""
    from app.afk import config as afk_config

    issue = make_issue(repo="infra")
    assert dispatch_policy.select_dispatchable([issue], afk_config.default(), set()) == []


# --------------------------------------------------------------------------- #
# Trust gate.
# --------------------------------------------------------------------------- #
def test_untrusted_issue_is_skipped(make_issue, make_config):
    issue = make_issue(repo="infra", labeled_by_trusted=False)
    assert dispatch_policy.select_dispatchable([issue], make_config(allowlist=["infra"]), set()) == []


def test_trusted_issue_is_eligible(make_issue, make_config):
    issue = make_issue(repo="infra", labeled_by_trusted=True)
    assert len(dispatch_policy.select_dispatchable([issue], make_config(allowlist=["infra"]), set())) == 1


def test_trust_gate_filters_only_untrusted(make_issue, make_config):
    trusted = make_issue(number=1, repo="infra", labeled_by_trusted=True)
    untrusted = make_issue(number=2, repo="infra", labeled_by_trusted=False)
    decisions = dispatch_policy.select_dispatchable(
        [trusted, untrusted], make_config(allowlist=["infra"]), set()
    )
    assert _selected_set(decisions) == {1}


# --------------------------------------------------------------------------- #
# Allowlist membership.
# --------------------------------------------------------------------------- #
def test_repo_not_in_allowlist_is_skipped(make_issue, make_config):
    issue = make_issue(repo="some-other-repo")
    assert dispatch_policy.select_dispatchable([issue], make_config(allowlist=["infra"]), set()) == []


def test_empty_allowlist_dispatches_nothing(make_issue, make_config):
    issue = make_issue(repo="infra")
    # kill switch off but allowlist empty -> still inert (the two-gate posture).
    config = make_config(allowlist=[], kill_switch=False)
    assert dispatch_policy.select_dispatchable([issue], config, set()) == []


def test_allowlist_selects_only_listed_repos(make_issue, make_config):
    a = make_issue(number=1, repo="infra")
    b = make_issue(number=2, repo="realestate-crawler")
    c = make_issue(number=3, repo="not-allowed")
    decisions = dispatch_policy.select_dispatchable(
        [a, b, c], make_config(allowlist=["infra", "realestate-crawler"]), set()
    )
    assert _selected_set(decisions) == {1, 2}


# --------------------------------------------------------------------------- #
# Per-repo lock (in_flight_repos).
# --------------------------------------------------------------------------- #
def test_repo_already_in_flight_is_skipped(make_issue, make_config):
    issue = make_issue(repo="infra")
    decisions = dispatch_policy.select_dispatchable(
        [issue], make_config(allowlist=["infra"]), in_flight_repos={"infra"}
    )
    assert decisions == []


def test_in_flight_lock_is_per_repo(make_issue, make_config):
    locked = make_issue(number=1, repo="infra")
    free = make_issue(number=2, repo="realestate-crawler")
    decisions = dispatch_policy.select_dispatchable(
        [locked, free],
        make_config(allowlist=["infra", "realestate-crawler"]),
        in_flight_repos={"infra"},
    )
    assert _selected_set(decisions) == {2}  # only the unlocked repo's issue runs


def test_all_repos_in_flight_dispatches_nothing(make_issue, make_config):
    a = make_issue(number=1, repo="infra")
    b = make_issue(number=2, repo="realestate-crawler")
    decisions = dispatch_policy.select_dispatchable(
        [a, b],
        make_config(allowlist=["infra", "realestate-crawler"]),
        in_flight_repos={"infra", "realestate-crawler"},
    )
    assert decisions == []


# --------------------------------------------------------------------------- #
# One-agent-per-repo invariant — at most ONE decision per repo per call.
#
# The whole design serialises agents within a repo (two would collide on the
# working tree). A single call must therefore never hand back two issues for the
# same repo, even when both are eligible and the repo is not yet in-flight.
# --------------------------------------------------------------------------- #
def test_at_most_one_decision_per_repo(make_issue, make_config):
    lo = make_issue(number=1, repo="infra", priority=1)
    hi = make_issue(number=2, repo="infra", priority=9)
    decisions = dispatch_policy.select_dispatchable(
        [lo, hi], make_config(allowlist=["infra"]), set()
    )
    assert len(decisions) == 1
    assert decisions[0].issue.number == 2  # the higher-priority one wins the slot


def test_one_decision_per_repo_across_many_repos(make_issue, make_config):
    issues = [
        make_issue(number=10, repo="infra", priority=1),
        make_issue(number=11, repo="infra", priority=5),
        make_issue(number=20, repo="realestate-crawler", priority=3),
        make_issue(number=21, repo="realestate-crawler", priority=2),
    ]
    decisions = dispatch_policy.select_dispatchable(
        issues, make_config(allowlist=["infra", "realestate-crawler"]), set()
    )
    # One per repo, each the repo's highest-priority eligible issue.
    assert _selected_set(decisions) == {11, 20}
    repos = [d.issue.repo for d in decisions]
    assert len(repos) == len(set(repos))  # no repo appears twice


def test_ineligible_higher_priority_does_not_consume_repo_slot(make_issue, make_config):
    """A higher-priority issue that is itself ineligible (e.g. blocked) must not
    suppress a lower-priority *eligible* issue in the same repo — the slot goes
    to the best ELIGIBLE candidate, not merely the highest-priority one."""
    blocked_hi = make_issue(number=1, repo="infra", priority=9, blocked_by=[99])
    ready_lo = make_issue(number=2, repo="infra", priority=1)
    decisions = dispatch_policy.select_dispatchable(
        [blocked_hi, ready_lo], make_config(allowlist=["infra"]), set()
    )
    assert _selected_numbers(decisions) == [2]


# --------------------------------------------------------------------------- #
# blocked_by gating — blocked_by holds OPEN blocker numbers.
# --------------------------------------------------------------------------- #
def test_blocked_issue_is_skipped(make_issue, make_config):
    issue = make_issue(repo="infra", blocked_by=[101])
    assert dispatch_policy.select_dispatchable([issue], make_config(allowlist=["infra"]), set()) == []


def test_unblocked_issue_with_empty_blocked_by_is_eligible(make_issue, make_config):
    issue = make_issue(repo="infra", blocked_by=[])
    assert len(dispatch_policy.select_dispatchable([issue], make_config(allowlist=["infra"]), set())) == 1


@pytest.mark.parametrize("blockers", [[1], [1, 2], [5, 6, 7]])
def test_any_open_blocker_blocks(make_issue, make_config, blockers):
    issue = make_issue(repo="infra", blocked_by=blockers)
    assert dispatch_policy.select_dispatchable([issue], make_config(allowlist=["infra"]), set()) == []


def test_blocked_filters_only_blocked(make_issue, make_config):
    ready = make_issue(number=1, repo="infra", blocked_by=[])
    blocked = make_issue(number=2, repo="realestate-crawler", blocked_by=[7])
    decisions = dispatch_policy.select_dispatchable(
        [ready, blocked], make_config(allowlist=["infra", "realestate-crawler"]), set()
    )
    assert _selected_set(decisions) == {1}


# --------------------------------------------------------------------------- #
# Priority ordering — higher priority first, deterministic tiebreaker.
# --------------------------------------------------------------------------- #
def test_higher_priority_first(make_issue, make_config):
    lo = make_issue(number=1, repo="infra", priority=1)
    mid = make_issue(number=2, repo="realestate-crawler", priority=5)
    hi = make_issue(number=3, repo="SparkyFitness", priority=9)
    decisions = dispatch_policy.select_dispatchable(
        [lo, hi, mid],
        make_config(allowlist=["infra", "realestate-crawler", "SparkyFitness"]),
        set(),
    )
    assert _selected_numbers(decisions) == [3, 2, 1]  # 9, 5, 1


def test_ordering_independent_of_input_order(make_issue, make_config):
    """Whatever order the caller supplies issues in, the dispatch order is the
    same — sorted purely by the policy, not by arrival."""
    base = [
        ("infra", 10, 2),
        ("realestate-crawler", 20, 8),
        ("SparkyFitness", 30, 5),
        ("health", 40, 1),
    ]
    allow = ["infra", "realestate-crawler", "SparkyFitness", "health"]
    config = make_config(allowlist=allow)
    expected = [20, 30, 10, 40]  # priorities 8,5,2,1

    for perm in itertools.permutations(base):
        issues = [make_issue(number=n, repo=r, priority=p) for (r, n, p) in perm]
        decisions = dispatch_policy.select_dispatchable(issues, config, set())
        assert _selected_numbers(decisions) == expected


def test_priority_ties_break_deterministically_by_issue_number(make_issue, make_config):
    """Equal priority across different repos -> a stable, total order. We tie-break
    on ascending issue number so the result never depends on dict/set iteration
    or input order."""
    a = make_issue(number=30, repo="infra", priority=5)
    b = make_issue(number=10, repo="realestate-crawler", priority=5)
    c = make_issue(number=20, repo="SparkyFitness", priority=5)
    config = make_config(allowlist=["infra", "realestate-crawler", "SparkyFitness"])

    for perm in itertools.permutations([a, b, c]):
        decisions = dispatch_policy.select_dispatchable(list(perm), config, set())
        assert _selected_numbers(decisions) == [10, 20, 30]


def test_negative_and_zero_priorities_order_correctly(make_issue, make_config):
    neg = make_issue(number=1, repo="infra", priority=-5)
    zero = make_issue(number=2, repo="realestate-crawler", priority=0)
    pos = make_issue(number=3, repo="SparkyFitness", priority=3)
    decisions = dispatch_policy.select_dispatchable(
        [neg, zero, pos],
        make_config(allowlist=["infra", "realestate-crawler", "SparkyFitness"]),
        set(),
    )
    assert _selected_numbers(decisions) == [3, 2, 1]  # 3 > 0 > -5


# --------------------------------------------------------------------------- #
# Reasons — human-readable, never parsed, but must be present and sensible.
# --------------------------------------------------------------------------- #
def test_every_decision_has_a_nonempty_reason(make_issue, make_config):
    issues = [
        make_issue(number=1, repo="infra", priority=3),
        make_issue(number=2, repo="realestate-crawler", priority=1),
    ]
    decisions = dispatch_policy.select_dispatchable(
        issues, make_config(allowlist=["infra", "realestate-crawler"]), set()
    )
    assert decisions  # sanity
    assert all(d.reason.strip() for d in decisions)


# --------------------------------------------------------------------------- #
# Combined matrix — every gate together. A single eligible needle in a haystack
# of issues that each trip exactly one gate.
# --------------------------------------------------------------------------- #
def test_only_the_fully_eligible_issue_survives_all_gates(make_issue, make_config):
    config = make_config(allowlist=["infra", "realestate-crawler"], kill_switch=False)
    in_flight = {"realestate-crawler"}  # this repo is locked

    issues = [
        make_issue(number=1, repo="infra", priority=5),                      # ELIGIBLE
        make_issue(number=2, repo="not-allowed", priority=9),                # allowlist
        make_issue(number=3, repo="infra", priority=9, labeled_by_trusted=False),  # trust
        make_issue(number=4, repo="infra", priority=9, blocked_by=[1]),      # blocked
        make_issue(number=5, repo="realestate-crawler", priority=9),         # repo locked
    ]
    decisions = dispatch_policy.select_dispatchable(issues, config, in_flight)
    assert _selected_numbers(decisions) == [1]
    assert decisions[0].issue.repo == "infra"


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("blocked", [True, False])
@pytest.mark.parametrize("locked", [True, False])
@pytest.mark.parametrize("killed", [True, False])
def test_full_eligibility_matrix(
    make_issue, make_config, trusted, allowed, blocked, locked, killed
):
    """Exhaustive truth table: an issue is dispatched iff ALL gates pass and the
    kill switch is off. 2**5 = 32 cases, single issue so ordering is moot."""
    issue = make_issue(
        number=1,
        repo="infra",
        priority=0,
        labeled_by_trusted=trusted,
        blocked_by=[99] if blocked else [],
    )
    config = make_config(
        allowlist=["infra"] if allowed else ["other-repo"],
        kill_switch=killed,
    )
    in_flight = {"infra"} if locked else set()

    decisions = dispatch_policy.select_dispatchable([issue], config, in_flight)

    should_dispatch = trusted and allowed and not blocked and not locked and not killed
    assert (len(decisions) == 1) is should_dispatch
    if should_dispatch:
        assert decisions[0].issue is issue
