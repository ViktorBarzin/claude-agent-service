"""Tests for ``app.afk.ci_watcher`` — the commit → ``CIStatus`` adapter.

The watcher folds two independent signals into one verdict the state machine
reads: the **GHA run** for a pushed commit (build/test/lint) and the
**deploy/rollout** that reaches the cluster (Woodpecker pipeline → Keel/k8s
rollout). The CI/CD chain is GHA → ghcr → Woodpecker → Keel
(``docs/2026-06-14-afk-implementation-pipeline-design.md``), so a commit is only
truly GREEN once *both* the build passed AND its image actually rolled out.

Every test injects FAKE clients — no test ever shells out to ``gh``,
``woodpecker``, or ``kubectl``, or reaches the network. The fakes implement the
``ci_watcher`` client Protocols and return staged ``StageResult`` values per
``(repo, commit)``; the watcher's only job is to query them and fold the result,
so the folding table is what these tests pin.
"""
import pytest

from app.afk.ci_watcher import (
    CIWatcher,
    StageResult,
)
from app.afk.types import CIStatus


# --------------------------------------------------------------------------- #
# Fakes for the three injected clients.
#
# Each maps (repo, commit) → StageResult and records every query, so tests can
# assert both the folded verdict AND that short-circuiting skips later stages
# (a RED build must not even ask the rollout client).
# --------------------------------------------------------------------------- #
class _FakeStageClient:
    """A recording stand-in for any of the three stage clients. ``default`` is
    returned for an unstaged ``(repo, commit)`` — defaults to ``PENDING`` so an
    un-seeded stage reads "not done yet", never a false GREEN."""

    def __init__(self, default: StageResult = StageResult.PENDING) -> None:
        self._results: dict[tuple[str, str], StageResult] = {}
        self._default = default
        self.queries: list[tuple[str, str]] = []

    def set(self, repo: str, commit: str, result: StageResult) -> None:
        self._results[(repo, commit)] = result

    def _lookup(self, repo: str, commit: str) -> StageResult:
        self.queries.append((repo, commit))
        return self._results.get((repo, commit), self._default)


class FakeGitHubChecks(_FakeStageClient):
    def run_conclusion(self, repo: str, commit: str) -> StageResult:
        return self._lookup(repo, commit)


class FakeWoodpecker(_FakeStageClient):
    def deploy_conclusion(self, repo: str, commit: str) -> StageResult:
        return self._lookup(repo, commit)


class FakeRollout(_FakeStageClient):
    def rollout_status(self, repo: str, commit: str) -> StageResult:
        return self._lookup(repo, commit)


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #
REPO = "infra"
COMMIT = "deadbeefcafe"


@pytest.fixture
def gha() -> FakeGitHubChecks:
    return FakeGitHubChecks()


@pytest.fixture
def woodpecker() -> FakeWoodpecker:
    return FakeWoodpecker()


@pytest.fixture
def rollout() -> FakeRollout:
    return FakeRollout()


@pytest.fixture
def watcher(gha, woodpecker, rollout) -> CIWatcher:
    return CIWatcher(github=gha, woodpecker=woodpecker, rollout=rollout)


def _stage_all(gha, woodpecker, rollout, *, build, deploy, roll) -> None:
    """Stage all three clients for the canonical ``(REPO, COMMIT)`` at once."""
    gha.set(REPO, COMMIT, build)
    woodpecker.set(REPO, COMMIT, deploy)
    rollout.set(REPO, COMMIT, roll)


# --------------------------------------------------------------------------- #
# StageResult vocabulary.
# --------------------------------------------------------------------------- #
def test_stageresult_has_the_four_outcomes():
    assert {s.name for s in StageResult} == {"NONE", "PENDING", "SUCCESS", "FAILURE"}


# --------------------------------------------------------------------------- #
# The happy path: every stage green ⇒ GREEN.
# --------------------------------------------------------------------------- #
def test_all_stages_success_is_green(watcher, gha, woodpecker, rollout):
    _stage_all(gha, woodpecker, rollout,
               build=StageResult.SUCCESS,
               deploy=StageResult.SUCCESS,
               roll=StageResult.SUCCESS)
    assert watcher.status(REPO, COMMIT) is CIStatus.GREEN


# --------------------------------------------------------------------------- #
# GHA build stage gates everything below it.
# --------------------------------------------------------------------------- #
def test_build_failure_is_red(watcher, gha):
    gha.set(REPO, COMMIT, StageResult.FAILURE)
    assert watcher.status(REPO, COMMIT) is CIStatus.RED


@pytest.mark.parametrize("build", [StageResult.NONE, StageResult.PENDING])
def test_build_not_yet_concluded_is_pending(watcher, gha, build):
    # No run yet (NONE) and in-progress (PENDING) both read PENDING — the state
    # machine waits on either.
    gha.set(REPO, COMMIT, build)
    assert watcher.status(REPO, COMMIT) is CIStatus.PENDING


def test_build_failure_short_circuits_before_deploy_and_rollout(
    watcher, gha, woodpecker, rollout
):
    gha.set(REPO, COMMIT, StageResult.FAILURE)
    # Even if later stages would (nonsensically) be green, a red build wins...
    woodpecker.set(REPO, COMMIT, StageResult.SUCCESS)
    rollout.set(REPO, COMMIT, StageResult.SUCCESS)
    assert watcher.status(REPO, COMMIT) is CIStatus.RED
    # ...and the later clients are never even queried.
    assert woodpecker.queries == []
    assert rollout.queries == []


def test_build_pending_short_circuits_before_deploy_and_rollout(
    watcher, gha, woodpecker, rollout
):
    gha.set(REPO, COMMIT, StageResult.PENDING)
    assert watcher.status(REPO, COMMIT) is CIStatus.PENDING
    assert woodpecker.queries == []
    assert rollout.queries == []


# --------------------------------------------------------------------------- #
# Deploy (Woodpecker) stage — only consulted once the build is green.
# --------------------------------------------------------------------------- #
def test_deploy_failure_is_red_even_with_green_build(watcher, gha, woodpecker):
    gha.set(REPO, COMMIT, StageResult.SUCCESS)
    woodpecker.set(REPO, COMMIT, StageResult.FAILURE)
    assert watcher.status(REPO, COMMIT) is CIStatus.RED


@pytest.mark.parametrize("deploy", [StageResult.NONE, StageResult.PENDING])
def test_deploy_not_yet_concluded_is_pending(watcher, gha, woodpecker, deploy):
    gha.set(REPO, COMMIT, StageResult.SUCCESS)
    woodpecker.set(REPO, COMMIT, deploy)
    assert watcher.status(REPO, COMMIT) is CIStatus.PENDING


def test_deploy_failure_short_circuits_before_rollout(
    watcher, gha, woodpecker, rollout
):
    gha.set(REPO, COMMIT, StageResult.SUCCESS)
    woodpecker.set(REPO, COMMIT, StageResult.FAILURE)
    rollout.set(REPO, COMMIT, StageResult.SUCCESS)
    assert watcher.status(REPO, COMMIT) is CIStatus.RED
    assert rollout.queries == []
    # The build WAS consulted (it had to pass to reach deploy).
    assert gha.queries == [(REPO, COMMIT)]


# --------------------------------------------------------------------------- #
# Rollout stage — the final gate. Green build + green deploy is still only
# PENDING until the image actually reaches the cluster.
# --------------------------------------------------------------------------- #
def test_rollout_failure_is_red(watcher, gha, woodpecker, rollout):
    _stage_all(gha, woodpecker, rollout,
               build=StageResult.SUCCESS,
               deploy=StageResult.SUCCESS,
               roll=StageResult.FAILURE)
    assert watcher.status(REPO, COMMIT) is CIStatus.RED


@pytest.mark.parametrize("roll", [StageResult.NONE, StageResult.PENDING])
def test_green_build_and_deploy_but_unfinished_rollout_is_pending(
    watcher, gha, woodpecker, rollout, roll
):
    _stage_all(gha, woodpecker, rollout,
               build=StageResult.SUCCESS,
               deploy=StageResult.SUCCESS,
               roll=roll)
    assert watcher.status(REPO, COMMIT) is CIStatus.PENDING


def test_green_requires_all_three_stages_consulted(
    watcher, gha, woodpecker, rollout
):
    _stage_all(gha, woodpecker, rollout,
               build=StageResult.SUCCESS,
               deploy=StageResult.SUCCESS,
               roll=StageResult.SUCCESS)
    assert watcher.status(REPO, COMMIT) is CIStatus.GREEN
    assert gha.queries == [(REPO, COMMIT)]
    assert woodpecker.queries == [(REPO, COMMIT)]
    assert rollout.queries == [(REPO, COMMIT)]


# --------------------------------------------------------------------------- #
# Plumbing: the commit and repo are passed through verbatim to every client,
# and an entirely un-seeded commit reads PENDING (not GREEN, not RED).
# --------------------------------------------------------------------------- #
def test_repo_and_commit_passed_through_to_clients(watcher, gha):
    gha.set("realestate-crawler", "abc123", StageResult.FAILURE)
    assert watcher.status("realestate-crawler", "abc123") is CIStatus.RED
    assert gha.queries == [("realestate-crawler", "abc123")]


def test_unknown_commit_defaults_to_pending(watcher):
    # Nothing staged anywhere ⇒ the build stage reads PENDING by default ⇒ the
    # whole verdict is PENDING. A never-pushed/just-pushed commit is never a
    # false GREEN.
    assert watcher.status(REPO, "never-seen") is CIStatus.PENDING


# --------------------------------------------------------------------------- #
# The default rollout client is OPTIONAL — per the pilot facts, state.sqlite /
# kubectl reads are optional, so a CIWatcher built without a rollout client must
# still work, treating "build green + deploy green" as the terminal GREEN.
# --------------------------------------------------------------------------- #
def test_rollout_client_is_optional_deploy_green_is_green(gha, woodpecker):
    w = CIWatcher(github=gha, woodpecker=woodpecker)  # no rollout client
    gha.set(REPO, COMMIT, StageResult.SUCCESS)
    woodpecker.set(REPO, COMMIT, StageResult.SUCCESS)
    assert w.status(REPO, COMMIT) is CIStatus.GREEN


def test_rollout_client_optional_still_honours_build_and_deploy_failures(
    gha, woodpecker
):
    w = CIWatcher(github=gha, woodpecker=woodpecker)
    gha.set(REPO, COMMIT, StageResult.SUCCESS)
    woodpecker.set(REPO, COMMIT, StageResult.FAILURE)
    assert w.status(REPO, COMMIT) is CIStatus.RED


# --------------------------------------------------------------------------- #
# Full folding table — exhaustive over (build, deploy, rollout) so the
# precedence rules (FAILURE short-circuits red; otherwise any PENDING/NONE keeps
# it pending; all-success ⇒ green) can never silently drift.
# --------------------------------------------------------------------------- #
_N, _P, _S, _F = (
    StageResult.NONE,
    StageResult.PENDING,
    StageResult.SUCCESS,
    StageResult.FAILURE,
)


def _expected(build: StageResult, deploy: StageResult, roll: StageResult) -> CIStatus:
    # Reference fold, independent of the implementation, evaluated stage by stage.
    for stage in (build, deploy, roll):
        if stage is _F:
            return CIStatus.RED
        if stage in (_N, _P):
            return CIStatus.PENDING
    return CIStatus.GREEN


@pytest.mark.parametrize("build", [_N, _P, _S, _F])
@pytest.mark.parametrize("deploy", [_N, _P, _S, _F])
@pytest.mark.parametrize("roll", [_N, _P, _S, _F])
def test_full_folding_table(watcher, gha, woodpecker, rollout, build, deploy, roll):
    _stage_all(gha, woodpecker, rollout, build=build, deploy=deploy, roll=roll)
    assert watcher.status(REPO, COMMIT) is _expected(build, deploy, roll)
