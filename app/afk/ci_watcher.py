"""CI watcher — fold a pushed commit's pipeline into a single ``CIStatus``.

A commit the agent pushed to ``master`` is only "done" once it has both *built*
and *deployed*: the CI/CD chain is GHA → ghcr → Woodpecker → Keel
(``docs/2026-06-14-afk-implementation-pipeline-design.md``). This adapter
collapses that multi-stage reality into the three-value verdict the state
machine speaks (:class:`~app.afk.types.CIStatus`): ``PENDING`` / ``GREEN`` /
``RED``.

It checks three stages in order and stops at the first that decides the verdict:

  1. **build** — the GitHub Actions run for the commit (build + test + lint);
  2. **deploy** — the Woodpecker pipeline that ships the built image;
  3. **rollout** — the image actually reaching the cluster (Keel/k8s rollout).

Folding rule, applied stage by stage: a ``FAILURE`` anywhere is ``RED`` (and we
short-circuit — a red build is never "rolled out", and we don't bother the later
clients); a stage that hasn't concluded (``NONE`` = no run yet, ``PENDING`` =
in progress) makes the whole verdict ``PENDING`` (the state machine waits on
either); only when *every* stage has succeeded is the commit ``GREEN``.

The three stage clients are **injected**, each behind a tiny structural
:class:`typing.Protocol`, so this module never imports ``gh`` / ``woodpecker`` /
``kubectl`` and the tests drive it entirely with fakes. The rollout client is
**optional** — the pilot keeps cluster/``state.sqlite`` reads optional, so a
watcher built without one treats a green deploy as the terminal ``GREEN``. The
real client wiring (subprocess argv, JSON parsing, kubectl-exec) lives in the
adapters that *implement* these Protocols, not here; keeping this module pure
keeps the folding logic the only thing under test.
"""
from enum import Enum
from typing import Protocol

from .types import CIStatus


class StageResult(Enum):
    """Outcome of one CI/CD stage for a commit, before folding into ``CIStatus``.

    Each injected client returns one of these per ``(repo, commit)``:

    ``NONE`` — no run exists yet for this commit (e.g. the webhook hasn't fired);
    ``PENDING`` — a run exists and is still in progress;
    ``SUCCESS`` — the stage concluded green;
    ``FAILURE`` — the stage concluded red.

    ``NONE`` and ``PENDING`` are distinct on purpose so a client can report
    "nothing here yet" vs "running" even though both fold to ``CIStatus.PENDING``;
    keeping them separate lets callers/log lines tell the two apart.
    """

    NONE = "none"
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


# --------------------------------------------------------------------------- #
# Injected client Protocols — structural, so any object with the right method
# (real adapter or test fake) satisfies them. No ``Any``: every method is typed
# (repo, commit) -> StageResult.
# --------------------------------------------------------------------------- #
class GitHubChecksClient(Protocol):
    """Reads the GitHub Actions run (build + test + lint) for a commit."""

    def run_conclusion(self, repo: str, commit: str) -> StageResult: ...


class WoodpeckerClient(Protocol):
    """Reads the Woodpecker deploy pipeline triggered for a commit's image."""

    def deploy_conclusion(self, repo: str, commit: str) -> StageResult: ...


class RolloutClient(Protocol):
    """Reads whether the commit's image has rolled out to the cluster."""

    def rollout_status(self, repo: str, commit: str) -> StageResult: ...


class CIWatcher:
    """Folds build → deploy → rollout into a single :class:`CIStatus`.

    Inject the three stage clients (``github`` and ``woodpecker`` are required;
    ``rollout`` is optional — omit it to stop the verdict at the deploy stage,
    matching the pilot's "cluster reads optional" posture). The clients are the
    only I/O surface, so production passes real adapters and tests pass fakes;
    :meth:`status` itself is pure.
    """

    def __init__(
        self,
        github: GitHubChecksClient,
        woodpecker: WoodpeckerClient,
        rollout: RolloutClient | None = None,
    ) -> None:
        self._github = github
        self._woodpecker = woodpecker
        self._rollout = rollout

    def status(self, repo: str, commit: str) -> CIStatus:
        """Return the folded CI verdict for ``commit`` in ``repo``.

        Stages are queried lazily in order and the first decisive one wins: a
        ``FAILURE`` yields ``RED``, an unconcluded stage (``NONE``/``PENDING``)
        yields ``PENDING``, and only when every stage has ``SUCCESS`` does the
        verdict reach ``GREEN``. Short-circuiting is real — a stage is only
        queried if every earlier stage succeeded, so a red/pending build never
        touches the deploy or rollout client (the assertions in the tests, and
        avoiding a needless kubectl-exec, both depend on this). With no rollout
        client the deploy stage is terminal.
        """
        # Each entry is a thunk so a later stage's client is never called once an
        # earlier stage has already decided the verdict.
        probes = [
            lambda: self._github.run_conclusion(repo, commit),
            lambda: self._woodpecker.deploy_conclusion(repo, commit),
        ]
        if self._rollout is not None:
            rollout = self._rollout  # bind for the closure (narrowed, non-None)
            probes.append(lambda: rollout.rollout_status(repo, commit))

        for probe in probes:
            verdict = _stage_verdict(probe())
            if verdict is not None:
                return verdict  # FAILURE → RED, NONE/PENDING → PENDING
        return CIStatus.GREEN


def _stage_verdict(stage: StageResult) -> CIStatus | None:
    """Decisive verdict for a single stage, or ``None`` to "keep going".

    ``FAILURE`` decides ``RED``; an unconcluded stage (``NONE``/``PENDING``)
    decides ``PENDING``; ``SUCCESS`` is non-decisive (``None``) — the next stage
    gets to speak, and only the last stage's success folds to ``GREEN``.
    """
    if stage is StageResult.FAILURE:
        return CIStatus.RED
    if stage in (StageResult.NONE, StageResult.PENDING):
        return CIStatus.PENDING
    return None
