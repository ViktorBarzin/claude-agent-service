"""Integration tests for ``app.afk.poller`` — the CronJob dispatch tick.

Unlike the unit suites, these wire the REAL pure cores (the actual
``dispatch_policy.select_dispatchable``) to the in-memory adapter FAKES from
``conftest`` (``FakeTracker`` / ``FakeT3Client``). No test touches a real T3
server, GitHub/Forgejo, or the cluster — the poller is exercised end to end with
fakes standing in only for the I/O edges.

What the tick must do (the poller contract):

  * **kill switch** — a disabled config dispatches nothing AND never calls the
    tracker or T3 (the CronJob does no I/O when the loop is off);
  * read the ready set via ``tracker.list_ready(config.allowlist)``;
  * derive the **per-repo lock** from the ready set itself — a repo with an issue
    already carrying the ``in_progress_label`` is in flight and is skipped (the
    CronJob is stateless between ticks, so the tracker is the source of truth);
  * run the real ``select_dispatchable`` over (ready issues, config, in-flight
    repos) and, for each decision, ``t3_client.dispatch(...)`` then
    ``tracker.add_label(repo, issue, in_progress_label)`` — label AFTER a
    successful dispatch so a dispatch failure never leaves a phantom lock.
"""
import pytest

from app.afk import poller
from app.afk.types import Config


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def _poller(fake_tracker, fake_t3) -> poller.Poller:
    """A Poller wired to the conftest fakes and the real dispatch policy."""
    return poller.Poller(tracker=fake_tracker, t3_client=fake_t3)


def _dispatched_pairs(fake_t3) -> set[tuple[str, int]]:
    return {(d["repo"], d["issue"]) for d in fake_t3.dispatched}


def _added_in_progress(fake_tracker, label: str = "agent-in-progress") -> set[tuple[str, int]]:
    return {
        (repo, issue)
        for (op, repo, issue, lbl) in fake_tracker.label_ops
        if op == "add" and lbl == label
    }


# --------------------------------------------------------------------------- #
# Kill switch — no dispatch, no I/O at all.
# --------------------------------------------------------------------------- #
def test_kill_switch_dispatches_nothing(fake_tracker, fake_t3, make_issue):
    fake_tracker.seed("infra", [make_issue(number=1, repo="infra")])
    config = Config(allowlist=["infra"], kill_switch=True)

    result = _poller(fake_tracker, fake_t3).run_once(config)

    assert result.dispatched == []
    assert fake_t3.dispatched == []


def test_kill_switch_does_not_even_read_the_tracker(fake_t3):
    """When the loop is off the CronJob must do zero I/O — not a single tracker
    or T3 call. A tracker that explodes if touched proves it."""
    class ExplodingTracker:
        def list_ready(self, repos):
            raise AssertionError("tracker must not be read when kill switch is on")

    config = Config(allowlist=["infra"], kill_switch=True)
    result = poller.Poller(tracker=ExplodingTracker(), t3_client=fake_t3).run_once(config)
    assert result.dispatched == []


# --------------------------------------------------------------------------- #
# Empty allowlist — armed kill switch but nothing to run.
# --------------------------------------------------------------------------- #
def test_empty_allowlist_dispatches_nothing(fake_tracker, fake_t3, make_issue):
    # list_ready([]) returns nothing, and even if it didn't the policy gates on
    # the (empty) allowlist. The shipped default posture.
    config = Config(allowlist=[], kill_switch=False)
    result = _poller(fake_tracker, fake_t3).run_once(config)
    assert result.dispatched == []
    assert fake_t3.dispatched == []


# --------------------------------------------------------------------------- #
# Happy path — one ready issue gets dispatched and labelled.
# --------------------------------------------------------------------------- #
def test_dispatches_a_ready_issue(fake_tracker, fake_t3, make_issue):
    fake_tracker.seed("infra", [make_issue(number=7, repo="infra")])
    config = Config(allowlist=["infra"], kill_switch=False)

    result = _poller(fake_tracker, fake_t3).run_once(config)

    assert _dispatched_pairs(fake_t3) == {("infra", 7)}
    assert len(result.dispatched) == 1
    assert result.dispatched[0].thread_id == "thread-0"
    assert result.dispatched[0].issue.number == 7


def test_labels_in_progress_after_dispatch(fake_tracker, fake_t3, make_issue):
    fake_tracker.seed("infra", [make_issue(number=7, repo="infra")])
    config = Config(allowlist=["infra"], kill_switch=False)

    _poller(fake_tracker, fake_t3).run_once(config)

    assert _added_in_progress(fake_tracker) == {("infra", 7)}


def test_in_progress_label_honours_config_override(fake_tracker, fake_t3, make_issue):
    fake_tracker.seed("infra", [make_issue(number=7, repo="infra")])
    config = Config(allowlist=["infra"], kill_switch=False, in_progress_label="busy")

    _poller(fake_tracker, fake_t3).run_once(config)

    assert _added_in_progress(fake_tracker, "busy") == {("infra", 7)}


def test_dispatch_prompt_references_the_issue(fake_tracker, fake_t3, make_issue):
    """The agent runs full-access and fetches the body itself, so the prompt the
    poller sends must at minimum point at the concrete repo#issue."""
    fake_tracker.seed("infra", [make_issue(number=7, repo="infra")])
    config = Config(allowlist=["infra"], kill_switch=False)

    _poller(fake_tracker, fake_t3).run_once(config)

    prompt = fake_t3.dispatched[0]["prompt"]
    assert "7" in prompt and "infra" in prompt
    assert prompt.strip()  # non-empty


# --------------------------------------------------------------------------- #
# Per-repo lock — an issue already carrying the in-progress label means an agent
# is in flight on that repo, so the repo is skipped this tick.
# --------------------------------------------------------------------------- #
def test_repo_with_in_progress_issue_is_locked(fake_tracker, fake_t3, make_issue):
    in_flight = make_issue(
        number=1, repo="infra", labels=["ready-for-agent", "agent-in-progress"]
    )
    waiting = make_issue(number=2, repo="infra", labels=["ready-for-agent"])
    fake_tracker.seed("infra", [in_flight, waiting])
    config = Config(allowlist=["infra"], kill_switch=False)

    result = _poller(fake_tracker, fake_t3).run_once(config)

    # Repo already busy → nothing new dispatched, no new in-progress label.
    assert result.dispatched == []
    assert fake_t3.dispatched == []
    assert _added_in_progress(fake_tracker) == set()


def test_lock_is_per_repo_not_global(fake_tracker, fake_t3, make_issue):
    # infra is busy; a different repo is free and should still dispatch.
    fake_tracker.seed(
        "infra",
        [make_issue(number=1, repo="infra", labels=["ready-for-agent", "agent-in-progress"])],
    )
    fake_tracker.seed("dotfiles", [make_issue(number=2, repo="dotfiles")])
    config = Config(allowlist=["infra", "dotfiles"], kill_switch=False)

    result = _poller(fake_tracker, fake_t3).run_once(config)

    assert _dispatched_pairs(fake_t3) == {("dotfiles", 2)}
    assert {d.issue.repo for d in result.dispatched} == {"dotfiles"}


def test_custom_in_progress_label_drives_the_lock(fake_tracker, fake_t3, make_issue):
    # The lock keys off config.in_progress_label, not the hardcoded default.
    fake_tracker.seed(
        "infra",
        [make_issue(number=1, repo="infra", labels=["ready-for-agent", "busy"])],
    )
    config = Config(allowlist=["infra"], kill_switch=False, in_progress_label="busy")
    result = _poller(fake_tracker, fake_t3).run_once(config)
    assert result.dispatched == []


# --------------------------------------------------------------------------- #
# One dispatch per repo per tick (the policy's one-agent-per-repo invariant,
# observed through the poller): the most urgent (lowest-value) eligible issue
# wins the slot.
# --------------------------------------------------------------------------- #
def test_one_dispatch_per_repo_per_tick(fake_tracker, fake_t3, make_issue):
    fake_tracker.seed(
        "infra",
        [
            make_issue(number=1, repo="infra", priority=1),  # most urgent (lowest value)
            make_issue(number=2, repo="infra", priority=9),
            make_issue(number=3, repo="infra", priority=5),
        ],
    )
    config = Config(allowlist=["infra"], kill_switch=False)

    _poller(fake_tracker, fake_t3).run_once(config)

    assert _dispatched_pairs(fake_t3) == {("infra", 1)}
    assert _added_in_progress(fake_tracker) == {("infra", 1)}


# --------------------------------------------------------------------------- #
# Gating still applies through the poller (the pure policy enforces it; the
# poller must not bypass it).
# --------------------------------------------------------------------------- #
def test_untrusted_issue_is_not_dispatched(fake_tracker, fake_t3, make_issue):
    fake_tracker.seed(
        "infra", [make_issue(number=1, repo="infra", labeled_by_trusted=False)]
    )
    config = Config(allowlist=["infra"], kill_switch=False)
    result = _poller(fake_tracker, fake_t3).run_once(config)
    assert result.dispatched == []
    assert fake_t3.dispatched == []


def test_blocked_issue_is_not_dispatched(fake_tracker, fake_t3, make_issue):
    fake_tracker.seed(
        "infra", [make_issue(number=2, repo="infra", blocked_by=[1])]
    )
    config = Config(allowlist=["infra"], kill_switch=False)
    result = _poller(fake_tracker, fake_t3).run_once(config)
    assert result.dispatched == []


def test_repo_outside_allowlist_is_not_dispatched(fake_tracker, fake_t3, make_issue):
    # list_ready only queries the allowlist, but even if a stray repo's issues
    # arrive the policy's allowlist gate drops them.
    fake_tracker.seed("secret", [make_issue(number=1, repo="secret")])
    config = Config(allowlist=["infra"], kill_switch=False)
    result = _poller(fake_tracker, fake_t3).run_once(config)
    assert result.dispatched == []


# --------------------------------------------------------------------------- #
# Dispatch failure must not leave a phantom lock (label only AFTER success).
# --------------------------------------------------------------------------- #
def test_dispatch_failure_does_not_label_in_progress(fake_tracker, make_issue):
    class FailingT3:
        def __init__(self):
            self.dispatched = []

        def dispatch(self, repo, issue, prompt):
            raise RuntimeError("T3 down")

    fake_tracker.seed("infra", [make_issue(number=7, repo="infra")])
    config = Config(allowlist=["infra"], kill_switch=False)

    with pytest.raises(RuntimeError):
        poller.Poller(tracker=fake_tracker, t3_client=FailingT3()).run_once(config)

    # No in-progress label was applied — the issue stays purely ready, so the
    # next tick retries it rather than treating it as locked.
    assert _added_in_progress(fake_tracker) == set()


# --------------------------------------------------------------------------- #
# list_ready is called with exactly the allowlist (not all repos).
# --------------------------------------------------------------------------- #
def test_queries_only_the_allowlisted_repos(fake_t3, make_issue):
    seen_repos: list[list[str]] = []

    class RecordingTracker:
        def list_ready(self, repos):
            seen_repos.append(list(repos))
            return []

        def add_label(self, *a):  # pragma: no cover - not reached here
            raise AssertionError("nothing to label")

    config = Config(allowlist=["infra", "dotfiles"], kill_switch=False)
    poller.Poller(tracker=RecordingTracker(), t3_client=fake_t3).run_once(config)

    assert seen_repos == [["infra", "dotfiles"]]
