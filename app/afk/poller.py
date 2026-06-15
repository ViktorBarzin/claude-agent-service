"""CronJob entrypoint: one dispatch tick of the AFK loop.

The poller is the *first half* of the loop — the part that decides what to start.
It runs once per CronJob invocation (the loop is stateless between ticks: the
issue tracker, not in-process memory, is the source of truth for what's already
in flight). Each tick:

  1. **kill switch** — if ``config.kill_switch`` is set the tick does NOTHING,
     not even a tracker read. A disabled loop must be inert: zero I/O, zero
     dispatches. (The pure policy also short-circuits on the kill switch, but the
     poller bails first so a disabled CronJob never touches the network.)
  2. read the ready set: ``tracker.list_ready(config.allowlist)`` — every open
     issue carrying the ready label across the allowlisted repos.
  3. derive the **per-repo lock**: a repo is "in flight" if any ready issue
     already carries ``config.in_progress_label`` (the poller stamps that label
     when it dispatches, so on the next tick the still-open issue re-appears and
     locks the repo). At most one agent per repo — two would collide on the
     working tree.
  4. run the pure ``dispatch_policy.select_dispatchable`` over (ready issues,
     config, in-flight repos) to get the ordered set to start this tick.
  5. for each decision: ``t3_client.dispatch(repo, issue, prompt)`` to spawn the
     worker thread, THEN ``tracker.add_label(repo, issue, in_progress_label)`` —
     label strictly *after* a successful dispatch, so a dispatch that raises
     never leaves a phantom lock that would freeze the repo forever.

It owns no policy of its own — the decision lives in ``dispatch_policy`` and the
agent's behaviour rides in the dispatched prompt's preamble (``t3_client``). The
two adapters (tracker, T3) are injected behind structural Protocols, so
production wires the real ``Tracker`` / ``T3Client`` and the tests wire the
in-memory fakes; nothing here opens a socket on its own.

DISABLED BY DEFAULT: a freshly-loaded ``Config`` has ``kill_switch=True`` and an
empty allowlist (see ``config.py``), so importing or scheduling this poller
dispatches nothing. Arming the loop — clearing the kill switch AND enrolling a
repo — is a deliberate manual step, performed later, never by this code.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from . import dispatch_policy
from .types import Config, DispatchDecision, Issue


# --------------------------------------------------------------------------- #
# Injected adapter Protocols — the I/O edges. Structural, so the real
# ``Tracker`` / ``T3Client`` and the test fakes both satisfy them with no
# explicit subclassing. Only the methods the poller actually calls appear here.
# --------------------------------------------------------------------------- #
class TrackerPort(Protocol):
    """The slice of ``tracker.Tracker`` the dispatch tick needs."""

    def list_ready(self, repos: list[str]) -> list[Issue]: ...
    def add_label(self, repo: str, issue: int, label: str) -> None: ...


class T3Port(Protocol):
    """The slice of ``t3_client.T3Client`` the dispatch tick needs."""

    def dispatch(self, repo: str, issue: int, prompt: str) -> str: ...


#: The pure dispatch gate's signature, injected so the tick can be tested with a
#: stub policy without reaching into module internals. Defaults to the real one.
DispatchFn = Callable[[list[Issue], Config, set[str]], list[DispatchDecision]]


@dataclass
class Dispatched:
    """One issue the tick actually started, with the T3 thread it spawned.

    Returned (not just logged) so the caller — and the tests — can see exactly
    what was launched. ``thread_id`` is what the watcher half later polls to
    drive this run to completion; ``reason`` carries the policy's human-readable
    justification through unchanged.
    """

    issue: Issue
    thread_id: str
    reason: str


@dataclass
class PollResult:
    """The outcome of one dispatch tick.

    ``dispatched`` is empty whenever the loop is disabled, the allowlist is
    empty, every repo is already in flight, or nothing clears the dispatch gate
    — i.e. the common steady-state of a quiet tick.
    """

    dispatched: list[Dispatched] = field(default_factory=list)


class Poller:
    """Runs one dispatch tick over injected tracker + T3 adapters.

    ``dispatch`` defaults to the real pure ``select_dispatchable`` policy; it is
    injectable purely so a test can substitute a stub without monkeypatching.
    The poller holds no state between ticks — each ``run_once`` is self-contained.
    """

    def __init__(
        self,
        tracker: TrackerPort,
        t3_client: T3Port,
        dispatch: DispatchFn = dispatch_policy.select_dispatchable,
    ) -> None:
        self._tracker = tracker
        self._t3 = t3_client
        self._dispatch = dispatch

    def run_once(self, config: Config) -> PollResult:
        """Execute one dispatch tick (see module docstring). Returns what it
        started; an empty result is the normal quiet-tick outcome."""
        # Kill switch: bail before any I/O — a disabled loop touches nothing.
        if config.kill_switch:
            return PollResult()

        ready = self._tracker.list_ready(config.allowlist)
        in_flight = _in_flight_repos(ready, config.in_progress_label)

        result = PollResult()
        for decision in self._dispatch(ready, config, in_flight):
            issue = decision.issue
            # Dispatch FIRST; only stamp the lock once the thread exists, so a
            # failed dispatch leaves the issue purely ready for the next tick to
            # retry rather than wedged behind a phantom in-progress label.
            thread_id = self._t3.dispatch(
                issue.repo, issue.number, _dispatch_prompt(issue)
            )
            self._tracker.add_label(issue.repo, issue.number, config.in_progress_label)
            result.dispatched.append(
                Dispatched(issue=issue, thread_id=thread_id, reason=decision.reason)
            )
        return result


# --------------------------------------------------------------------------- #
# Internals — pure helpers.
# --------------------------------------------------------------------------- #
def _in_flight_repos(ready: list[Issue], in_progress_label: str) -> set[str]:
    """Repos that already have an agent in flight, read off the ready set.

    A repo is in flight if any of its ready issues still carries the in-progress
    label — the stamp the poller applied on a previous tick's dispatch. Because
    the dispatched issue keeps its ready label until the watcher closes/relabels
    it, it re-appears here and locks the repo until the run finishes.
    """
    return {issue.repo for issue in ready if in_progress_label in issue.labels}


def _dispatch_prompt(issue: Issue) -> str:
    """The turn prompt for one issue's worker thread.

    The full-access agent fetches the issue body itself (it has ``gh``), so the
    prompt only needs to point unambiguously at the concrete ``repo#number``; the
    standing rules are prepended by ``t3_client`` as the issue-implementer
    preamble. Kept deliberately terse — one canonical instruction, no per-issue
    templating to drift.
    """
    return (
        f"Implement issue #{issue.number} in the `{issue.repo}` repository. "
        f"Fetch the issue with `gh issue view {issue.number} --repo {issue.repo}` "
        f"(and its comments) to get the full task, then implement it end to end."
    )
