"""One fixer tick: drain the queue, then drive whatever is in flight.

The webhook dispatches immediately when the repo is free, which covers the
common case. This module covers the two things a webhook cannot:

  * **Draining.** A ``broken`` issue that arrived while a run held the repo was
    refused, not queued in memory. Each tick re-reads the ready set and starts
    the best candidate once the lock frees, so nothing is lost by being second.
  * **Following through.** A run's commit has to be watched to green, and a red
    pipeline has to become a corrective turn. That is ``watcher.tick`` per
    in-flight issue, with the run's bookkeeping read from the issue footer.

Run as a CronJob against the service's own HTTP API. It holds no state of its
own: everything it needs is in the tracker (the ``agent-in-progress`` label) and
in the issue (the footer), which is what lets a tick run in a fresh pod and pick
up exactly where the last one left off.
"""
import argparse
import logging
import os
import sys
import time
import urllib.error
import urllib.request

from app.afk.ci_watcher import StageResult  # noqa: F401  (documents the CI contract)
from app.afk.notifier import Notifier
from app.afk.phase_checklist import FIXER_LABELS
from app.afk.poller import Poller
from app.afk.tracker import Tracker
from app.afk.types import Action, Config
from app.afk.watcher import InFlightRun, Watcher
from app.fixer import config as fixer_config
from app.fixer import ntfy, prompts
from app.fixer.checklist_tracker import ChecklistCollapsingTracker
from app.fixer.execute_client import UNREACHABLE, ExecuteClient
from app.fixer.forgejo import ForgejoClient
from app.fixer.runstate import (
    RunRecord,
    all_job_ids,
    find_pushed_commit,
    latest_record,
    render_comment,
)

log = logging.getLogger("fixer.tick")


class ServiceJobs:
    """The service's own ``/execute`` and ``/jobs/{id}``, over HTTP.

    A tick runs in its own pod, so it reaches the runner the same way any other
    in-cluster caller does. The bearer token is the service's existing API token.
    """

    def __init__(self, base_url: str, token: str, agent: str, cfg: fixer_config.FixerConfig):
        self._base = base_url.rstrip("/")
        self._token = token
        self._agent = agent
        self._cfg = cfg

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        import json
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self._token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def submit(self, prompt: str, issue: str = "") -> str:
        payload: dict = {"prompt": prompt, "agent": self._agent,
                         "metadata": {"source": "fixer-tick", "issue": issue}}
        # Omit the ceilings entirely when unset, so the service's own optional
        # defaults apply rather than a value invented here.
        if self._cfg.max_budget_usd is not None:
            payload["max_budget_usd"] = self._cfg.max_budget_usd
        if self._cfg.timeout_seconds is not None:
            payload["timeout_seconds"] = self._cfg.timeout_seconds
        return str(self._call("POST", "/execute", payload).get("job_id") or "")

    def fetch(self, job_id: str) -> dict | None:
        """The job record, ``None`` if the runner has never heard of it, or
        ``UNREACHABLE`` if we could not ask.

        The distinction is the whole point. A 404 is an assertion — the runner is
        up and does not know this job, so the run is gone and should escalate.
        Anything else (connection refused mid-roll, a timeout, a 5xx) means we
        failed to ask, which says nothing about the run; treating that as death
        escalated a run that was two minutes into working.
        """
        try:
            return self._call("GET", f"/jobs/{job_id}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            log.warning("job fetch %s -> HTTP %s (treating as unknown)", job_id, exc.code)
            return dict(UNREACHABLE)
        except Exception as exc:  # noqa: BLE001 - any transport failure
            log.warning("job fetch %s unreachable (%s)", job_id, exc)
            return dict(UNREACHABLE)


class FixerDispatcher:
    """``ExecuteClient`` with the fixer's own prompt for a first turn.

    The poller builds a terse prompt of its own, which suits an agent that
    already carries a standing preamble. The fixer's runs are one-shot, so the
    prompt has to carry the context itself — this substitutes it, and leaves
    everything else about the port untouched.
    """

    def __init__(self, inner: ExecuteClient, forgejo: ForgejoClient,
                 cfg: fixer_config.FixerConfig):
        self._inner = inner
        self._forgejo = forgejo
        self._cfg = cfg

    def dispatch(self, repo: str, issue: int, prompt: str,
                 *, kind: str = "first") -> str:
        """Dispatch a turn of ``kind`` for ``repo#issue``.

        ``prompt`` is accepted to satisfy the port and deliberately unused: the
        caller's prompt assumes a standing preamble that a one-shot run does not
        have, so the right prompt is built here from the issue and the run's own
        recorded state. ``kind`` says which one — an unrecognised kind raises
        rather than falling back to the first-turn prompt, because sending "fix
        this issue" to a turn that is mid-chain loses everything its predecessor
        established.
        """
        self._inner.label = f"{repo}#{issue}"
        issue_obj = self._forgejo.get_issue(repo, issue)
        common = dict(
            owner=self._cfg.owner, repo=repo, number=issue,
            title=str(issue_obj.get("title") or ""),
            issue_url=self._cfg.issue_url(repo, issue),
        )
        if kind == "first":
            own = prompts.first_turn(
                **common, trigger_label=self._cfg.trigger_label
            )
        elif kind == "fix_forward":
            bodies = [str(c.get("body") or "")
                      for c in self._forgejo.list_comments(repo, issue)]
            record = latest_record(bodies) or RunRecord(job_id="", started_at=0.0)
            own = prompts.fix_forward_turn(**common, record=record)
        elif kind == "redispatch":
            own = prompts.redispatch_turn(**common)
        else:
            raise ValueError(f"unknown dispatch kind {kind!r}")
        return self._inner.dispatch(repo, issue, own)

    def snapshot(self) -> dict:
        return self._inner.snapshot()

    def track(self, job_id: str) -> None:
        self._inner.track(job_id)


def build(cfg: fixer_config.FixerConfig, service_url: str, service_token: str):
    """Wire the real adapters. Returns everything a tick needs."""
    forgejo = ForgejoClient(cfg.forgejo_api, cfg.token, cfg.owner)
    tracker = ChecklistCollapsingTracker(
        Tracker(forgejo, ready_label=cfg.trigger_label), forgejo, cfg.bot_actor,
    )
    jobs = ServiceJobs(service_url, service_token, cfg.agent, cfg)
    dispatcher = FixerDispatcher(ExecuteClient(jobs.submit, jobs.fetch), forgejo, cfg)
    notifier = Notifier(
        ntfy.make_sender(cfg.ntfy_url, cfg.ntfy_topic, cfg.ntfy_token),
        base_url=f"{cfg.forgejo_web.rstrip('/')}/{cfg.owner}",
        link_builder=ntfy.forgejo_link,
    )
    return forgejo, tracker, dispatcher, notifier


def drain(tracker, dispatcher, forgejo, loop_cfg: Config, cfg) -> int:
    """Start the best queued ``broken`` issue, if the repo is free. Returns the count."""
    result = Poller(tracker, dispatcher).run_once(loop_cfg)
    for started in result.dispatched:
        record = RunRecord(job_id=started.thread_id, started_at=time.time())
        forgejo.comment(
            started.issue.repo, started.issue.number,
            render_comment(
                "Picked this up from the queue — investigating.\n\n"
                f"_Fixer run `{started.thread_id}`._",
                record,
            ),
        )
        log.info("drained %s#%s -> job %s (%s)", started.issue.repo,
                 started.issue.number, started.thread_id, started.reason)
    return len(result.dispatched)


def watch(forgejo, tracker, dispatcher, notifier, loop_cfg: Config, cfg) -> list[str]:
    """Drive every in-flight run one step. Returns one summary line per run."""
    watcher = Watcher(
        t3_client=dispatcher, tracker=tracker, ci_watcher=_ci_watcher(),
        notifier=notifier, ready_for_human_label=cfg.human_label,
        checklist_labels=FIXER_LABELS,
    )
    lines: list[str] = []
    for repo in loop_cfg.allowlist:
        for raw in forgejo.list_issues(repo, loop_cfg.in_progress_label):
            number = int(raw.get("number") or 0)
            bodies = [str(c.get("body") or "") for c in forgejo.list_comments(repo, number)]
            record = latest_record(bodies)
            if record is None:
                # An in-progress label with no footer behind it: nothing here can
                # drive it, so hand it over rather than leaving it parked.
                forgejo.remove_label(repo, number, loop_cfg.in_progress_label)
                forgejo.add_label(repo, number, cfg.human_label)
                forgejo.comment(repo, number,
                                "This was marked in progress but carries no run state, "
                                "so no tick can follow it through. Handing it over.")
                lines.append(f"{repo}#{number}: orphaned, escalated")
                continue

            # EVERY job id the thread has carried is hex and appears in prose, so
            # none of them may be read as a pushed commit — not just this run's.
            commit = (find_pushed_commit(bodies, exclude=all_job_ids(bodies))
                      or record.commit)
            dispatcher.track(record.job_id)
            issue = _issue_for(tracker, repo, number, raw)
            run = InFlightRun(
                issue=issue,
                thread_id=record.job_id,
                commit=commit,
                fix_forward_attempts=record.fix_forward_attempts,
                elapsed_seconds=record.elapsed_seconds(time.time()),
                redispatch_attempts=record.redispatch_attempts,
            )
            observed = dispatcher.snapshot()
            state = next((t.get("latestTurn", {}).get("state")
                          for t in observed.get("threads", [])
                          if t.get("id") == record.job_id), None)
            result = watcher.tick(run, loop_cfg)
            log.info("watch %s#%s: job=%s turn=%s commit=%s attempts=%d "
                     "restarts=%d -> %s",
                     repo, number, record.job_id, state, commit,
                     record.fix_forward_attempts, record.redispatch_attempts,
                     result.action.value)
            _persist(forgejo, repo, number, record, result, commit,
                     observed_turn=state)
            lines.append(f"{repo}#{number}: {result.action.value}")
    return lines


def _persist(forgejo, repo: str, number: int, record: RunRecord, result, commit,
             *, observed_turn: str | None = None) -> None:
    """Write the run's new state back into the issue, when it changed.

    Two actions change state a later tick needs, both because they start a new
    job: a fix-forward turn (new job id, bumped attempt count) and a re-dispatch
    after the runner lost the job (new job id, bumped restart count). Terminal
    actions have already had their say via the watcher's own comments, and WAIT
    changes nothing worth a comment — a tick that posted on every WAIT would bury
    the issue in noise.
    """
    if result.action is Action.CLOSE_SUCCESS and observed_turn == "running":
        # The defer ceiling released this verdict with the turn still going. The
        # close is right — the commit landed and CI is green — but the run is now
        # unwatched while a job may still hold a clone and push rights, and the
        # last footer names that job with nothing marking it abandoned. Leave the
        # note so a later stray commit can be traced back here.
        forgejo.comment(repo, number, (
            f"Closing on green CI, but job `{record.job_id}` was still running "
            f"when this closed — it passed the deferral ceiling, so the verdict "
            f"was released rather than waiting longer. Nothing more is watching "
            f"this issue; if a commit from that job appears later, this is where "
            f"it came from."
        ))
        return
    if result.action not in (Action.FIX_FORWARD, Action.REDISPATCH):
        return
    fix_forwards = record.fix_forward_attempts
    redispatches = record.redispatch_attempts
    if result.action is Action.FIX_FORWARD:
        fix_forwards += 1
        visible = (f"CI came back red on `{commit}`. Dispatched a corrective turn "
                   f"(attempt {fix_forwards}).")
    else:
        redispatches += 1
        visible = (
            f"The run's job was gone from the runner and nothing had been pushed "
            f"— usually the service being replaced mid-run. Started it again "
            f"(restart {redispatches}); no work was lost, because none had landed."
        )
    updated = RunRecord(
        job_id=result.thread_id or record.job_id,
        started_at=record.started_at,
        commit=commit,
        fix_forward_attempts=fix_forwards,
        redispatch_attempts=redispatches,
        chain_parent=record.chain_parent,
        notes=record.notes,
    )
    forgejo.comment(repo, number, render_comment(visible, updated))


def _issue_for(tracker, repo: str, number: int, raw: dict):
    """The ``Issue`` record for one raw issue, via the tracker's own conversion."""
    return tracker._to_issue(repo, raw)  # noqa: SLF001 - the conversion is the tracker's


def _ci_watcher():
    """The CI verdict source. Imported lazily so a tick that never watches does
    not need the adapter's own dependencies."""
    from app.fixer.ci import build_ci_watcher
    return build_ci_watcher()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="One fixer tick.")
    parser.add_argument("--service-url",
                        default=os.environ.get("FIXER_SERVICE_URL",
                                               "http://localhost:8080"))
    parser.add_argument("--dry-run", action="store_true",
                        help="report what a tick would do, change nothing")
    args = parser.parse_args(argv)

    cfg = fixer_config.from_env()
    loop_cfg = fixer_config.loop_config()
    if loop_cfg.kill_switch:
        log.info("kill switch set — tick does nothing")
        return 0
    if not cfg.token:
        log.error("no FIXER_FORGEJO_TOKEN — cannot reach the tracker")
        return 1

    token = os.environ.get("API_BEARER_TOKEN", "")
    forgejo, tracker, dispatcher, notifier = build(cfg, args.service_url, token)

    if args.dry_run:
        ready = tracker.list_ready(loop_cfg.allowlist)
        log.info("dry run: %d ready, %s", len(ready),
                 [f"{i.repo}#{i.number}" for i in ready])
        return 0

    # The two phases are independent and each is retried by the next tick, so one
    # failing must not skip the other. This matters most during a deployment
    # roll: /execute is briefly unreachable, and a tick that died on the drain
    # would also skip driving every in-flight run.
    started, lines = 0, []
    drain_error = watch_error = None
    try:
        started = drain(tracker, dispatcher, forgejo, loop_cfg, cfg)
    except Exception as exc:  # noqa: BLE001 - a tick reports and moves on
        drain_error = exc
        log.warning("tick: drain failed (%s) — the next tick retries", exc)
    try:
        lines = watch(forgejo, tracker, dispatcher, notifier, loop_cfg, cfg)
    except Exception as exc:  # noqa: BLE001
        watch_error = exc
        log.warning("tick: watch failed (%s) — the next tick retries", exc)

    log.info("tick: %d dispatched, %d in flight%s", started, len(lines),
             (" — " + "; ".join(lines)) if lines else "")

    # Non-zero only when NOTHING worked: a half-successful tick is a normal
    # transient (a rolling pod), and marking the job Failed for it buries the
    # real failures in noise.
    if drain_error is not None and watch_error is not None:
        log.error("tick: both phases failed — drain=%s watch=%s", drain_error, watch_error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
