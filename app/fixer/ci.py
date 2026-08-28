"""CI verdict sources for the fixer.

``app.afk.ci_watcher`` folds three stages — build, deploy, rollout — into one
``PENDING``/``GREEN``/``RED`` verdict, with each stage behind an injected
Protocol. This module supplies the concrete clients for the fixer's one enrolled
repo, ``infra``.

For an infra change, **Woodpecker is the decisive stage**: `.woodpecker/default.yml`
runs on every push to master and is what applies the change to the cluster. So
the deploy client is a real implementation and the watcher's rollout stage stays
unset, which makes a green deploy terminal.

The build stage is the honest gap. Infra's build/test/lint runs on GitHub Actions
against the mirror (infra ADR-0002), which needs a GitHub credential the fixer
otherwise has no use for. Rather than pretend, the stage is explicit about which
mode it is in:

  * with ``FIXER_GITHUB_TOKEN`` set, it queries the real check runs;
  * without it, :class:`UnobservedStage` reports ``SUCCESS`` and logs, once, that
    the stage is unobserved — so a reader of the logs knows the verdict rests on
    Woodpecker alone rather than discovering it from behaviour.

That default is a deliberate trade: an infra change's real gate is the apply, and
treating an unobservable stage as failing would freeze every run instead.
"""
import json
import logging
import os
import urllib.error
import urllib.request

from app.afk.ci_watcher import CIWatcher, StageResult

log = logging.getLogger(__name__)

ENV_WOODPECKER_URL = "FIXER_WOODPECKER_URL"
ENV_WOODPECKER_TOKEN = "FIXER_WOODPECKER_TOKEN"
ENV_WOODPECKER_REPO_ID = "FIXER_WOODPECKER_REPO_ID"
ENV_GITHUB_TOKEN = "FIXER_GITHUB_TOKEN"
ENV_GITHUB_REPO = "FIXER_GITHUB_REPO"
#: DRILL AFFORDANCE, off by default. With this set, the FIRST verdict asked for
#: any given commit comes back RED, and every later verdict for that same commit
#: is the real one. It exists because the fix-forward path is otherwise
#: unreachable in a drill: this repo's CI is apply-only, so a docs commit can
#: never turn it red, and the alternative — breaking a shared Terraform module to
#: force a failing plan — risks every stack's apply.
#:
#: What it does NOT simulate is Woodpecker's own red detection; that is covered by
#: unit tests over every status string the API returns.
ENV_FORCE_RED_ONCE = "FIXER_CI_FORCE_RED_ONCE"
FORCE_RED_STATE = "/persistent/fixer-runs/.forced-red"

DEFAULT_WOODPECKER_URL = "http://woodpecker-server.woodpecker.svc.cluster.local"
DEFAULT_WOODPECKER_REPO_ID = "1"
DEFAULT_GITHUB_REPO = "ViktorBarzin/infra"

#: Woodpecker pipeline status -> stage result. Anything unrecognised is PENDING,
#: never SUCCESS: an unknown status must not be able to close an issue.
_WOODPECKER_STATUS = {
    "success": StageResult.SUCCESS,
    "failure": StageResult.FAILURE,
    "error": StageResult.FAILURE,
    "killed": StageResult.FAILURE,
    "declined": StageResult.FAILURE,
    "blocked": StageResult.PENDING,
    "pending": StageResult.PENDING,
    "running": StageResult.PENDING,
    "started": StageResult.PENDING,
    "skipped": StageResult.SUCCESS,
}

_GITHUB_CONCLUSION = {
    "success": StageResult.SUCCESS,
    "neutral": StageResult.SUCCESS,
    "skipped": StageResult.SUCCESS,
    "failure": StageResult.FAILURE,
    "timed_out": StageResult.FAILURE,
    "cancelled": StageResult.FAILURE,
    "action_required": StageResult.FAILURE,
}


def _get_json(url: str, headers: dict[str, str]) -> object | None:
    req = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
        log.warning("ci: %s -> %s", url, exc)
        return None


class UnobservedStage:
    """A stage this deployment cannot see, reported as SUCCESS with a log line.

    Used for the GitHub Actions build stage when no GitHub credential is
    configured. It logs the first time it is consulted per process, so the
    reason a verdict rests on fewer stages is visible without reading the code.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._announced = False

    def _announce(self) -> None:
        if not self._announced:
            log.info("ci: the %s stage is unobserved in this deployment; "
                     "verdicts rest on the remaining stages", self._name)
            self._announced = True

    def run_conclusion(self, repo: str, commit: str) -> StageResult:
        self._announce()
        return StageResult.SUCCESS


class GitHubChecks:
    """The GitHub Actions build stage, over the mirror's check runs."""

    def __init__(self, token: str, repo_slug: str = DEFAULT_GITHUB_REPO) -> None:
        self._token = token
        self._slug = repo_slug

    def run_conclusion(self, repo: str, commit: str) -> StageResult:
        data = _get_json(
            f"https://api.github.com/repos/{self._slug}/commits/{commit}/check-runs",
            {"Authorization": f"token {self._token}",
             "Accept": "application/vnd.github+json"},
        )
        runs = (data or {}).get("check_runs") if isinstance(data, dict) else None
        if not runs:
            return StageResult.NONE
        worst = StageResult.SUCCESS
        for run in runs:
            if run.get("status") != "completed":
                return StageResult.PENDING
            result = _GITHUB_CONCLUSION.get(str(run.get("conclusion")), StageResult.PENDING)
            if result is StageResult.FAILURE:
                return StageResult.FAILURE
            if result is StageResult.PENDING:
                worst = StageResult.PENDING
        return worst


def _force_red_once(commit: str) -> bool:
    """True the first time a verdict is asked for ``commit``, while armed.

    The seen-set is a file on the persistent volume rather than process state,
    because each tick runs in a fresh pod — in memory it would fire every tick
    and the run would never leave fix-forward.
    """
    if not (os.environ.get(ENV_FORCE_RED_ONCE) or "").strip():
        return False
    try:
        os.makedirs(os.path.dirname(FORCE_RED_STATE), exist_ok=True)
        seen: set[str] = set()
        if os.path.exists(FORCE_RED_STATE):
            with open(FORCE_RED_STATE) as fh:
                seen = {line.strip() for line in fh if line.strip()}
        if commit in seen:
            return False
        with open(FORCE_RED_STATE, "a") as fh:
            fh.write(commit + "\n")
        log.warning("ci: FORCING RED once for %s (drill affordance %s is set)",
                    commit, ENV_FORCE_RED_ONCE)
        return True
    except OSError as exc:
        # Armed but unable to keep its state. Say so: read as "not armed" this
        # cost three days of ticks in which the affordance looked active and
        # never fired, because the tick pod did not mount the volume this path
        # lives on and the write raised PermissionError.
        log.warning("ci: %s is set but the once-marker at %s is unwritable "
                    "(%s) — NOT forcing red", ENV_FORCE_RED_ONCE,
                    FORCE_RED_STATE, exc)
        return False


class WoodpeckerPipelines:
    """The deploy stage: Woodpecker's pipeline for the pushed commit.

    For infra this is the apply, so it is the stage that decides whether a change
    actually reached the cluster.
    """

    def __init__(self, base_url: str, token: str, repo_id: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._repo_id = repo_id

    def deploy_conclusion(self, repo: str, commit: str) -> StageResult:
        if _force_red_once(commit):
            return StageResult.FAILURE
        data = _get_json(
            f"{self._base}/api/repos/{self._repo_id}/pipelines?perPage=50",
            {"Authorization": f"Bearer {self._token}"},
        )
        if not isinstance(data, list):
            return StageResult.NONE
        for pipeline in data:
            if str(pipeline.get("commit") or "").startswith(commit[:7]):
                status = str(pipeline.get("status") or "")
                return _WOODPECKER_STATUS.get(status, StageResult.PENDING)
        # No pipeline for this commit yet — the webhook may not have fired.
        return StageResult.NONE


def build_ci_watcher(env: dict[str, str] | None = None) -> CIWatcher:
    """Assemble the watcher's CI source from the environment."""
    e = env if env is not None else dict(os.environ)
    github_token = (e.get(ENV_GITHUB_TOKEN) or "").strip()
    build_stage = (
        GitHubChecks(github_token, e.get(ENV_GITHUB_REPO) or DEFAULT_GITHUB_REPO)
        if github_token
        else UnobservedStage("GitHub Actions build")
    )
    deploy_stage = WoodpeckerPipelines(
        e.get(ENV_WOODPECKER_URL) or DEFAULT_WOODPECKER_URL,
        (e.get(ENV_WOODPECKER_TOKEN) or "").strip(),
        e.get(ENV_WOODPECKER_REPO_ID) or DEFAULT_WOODPECKER_REPO_ID,
    )
    # No rollout client: for infra the Woodpecker apply IS the landing, so a
    # green deploy is terminal (ci_watcher documents that behaviour).
    return CIWatcher(github=build_stage, woodpecker=deploy_stage)
