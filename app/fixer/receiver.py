"""``POST /hooks/forgejo`` — the fixer's front door.

One endpoint, and deliberately little logic of its own: it verifies the delivery
came from our Forgejo, reduces it to a :class:`~app.fixer.gates.Delivery`, asks
the pure gates whether to act, checks the one thing the gates cannot (the live
per-repo lock), and dispatches.

Every refusal answers **200** with a reason, not an error code. Forgejo retries
and disables hooks that keep failing, and "this delivery was not a trigger" is
the normal case, not a fault — the endpoint sees far more deliveries it should
ignore than ones it should act on. The two exceptions are a bad signature (401,
because that is a real fault worth surfacing on the hook's delivery history) and
an unconfigured service (503).

The per-repo lock lives here rather than in the gates because it is a live read:
an issue already carrying ``agent-in-progress`` means a run holds the repo, and
at most one run may hold it (design doc, decision 14). A delivery that arrives
while the lock is held is dropped, not queued in memory — the next poller tick
finds the issue still labelled ``broken`` and starts it when the lock frees.

What counts as a trigger, who may pull it, which agent runs and with which
prompt are per repo (:mod:`app.fixer.profiles`). On a repo with the comment
trigger (f1-stream), a reply from the approver on an approved issue starts a
continue turn carrying the reply. When the repo is busy, the reply takes
``needs-human`` off instead, which is what puts the issue back in the drain's
queue for when the lock frees.
"""
import logging
import time

from fastapi import APIRouter, Request, Response

from app.fixer import config as fixer_config
from app.fixer import gates, profiles, signature
from app.fixer.forgejo import ForgejoClient
from app.fixer.runstate import RunRecord, render_comment

log = logging.getLogger(__name__)

router = APIRouter()

#: Set by ``main`` at startup: (prompt, issue[, agent]) -> job_id. ``agent`` is
#: passed only for a repo whose profile names one. Injected rather than
#: imported so the receiver never reaches into the job runner's internals, and so
#: the tests drive it with a recorder.
_submit = None


def set_submitter(submit) -> None:
    """Wire the job-submission callable the receiver dispatches through."""
    global _submit
    _submit = submit


def _client(cfg: fixer_config.FixerConfig) -> ForgejoClient:
    return ForgejoClient(cfg.forgejo_api, cfg.token, cfg.owner)


def _repo_locked(client: ForgejoClient, cfg, repo: str, in_progress_label: str) -> bool:
    """Whether a run already holds ``repo``.

    Read from the tracker, not from memory: the label is the lock, so it survives
    a pod restart and is visible to a human looking at the issue list.
    """
    return bool(client.list_issues(repo, in_progress_label))


@router.post("/hooks/forgejo")
async def forgejo_hook(request: Request, response: Response) -> dict:
    """Handle one Forgejo webhook delivery."""
    cfg = fixer_config.from_env()
    loop_cfg = fixer_config.loop_config()

    if not cfg.configured:
        response.status_code = 503
        return {"ok": False, "reason": "not-configured"}

    raw = await request.body()
    if not signature.verify(cfg.webhook_secret, raw, dict(request.headers)):
        log.warning("forgejo hook: signature rejected (%d bytes)", len(raw))
        response.status_code = 401
        return {"ok": False, "reason": "bad-signature"}

    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "reason": "unparseable-body"}

    event = signature.event_name(dict(request.headers))
    delivery = gates.parse_delivery(event, payload)
    if delivery is None:
        return {"ok": False, "reason": "not-an-issue-event"}

    client = _client(cfg)
    profile = profiles.profile_for(delivery.repo, cfg)
    if profile.trusted_actors is not None:
        trusted = profile.trusted_actors
    else:
        trusted = client.trusted_actors(delivery.repo) if delivery.repo else frozenset()

    verdict = gates.decide(
        delivery,
        loop_cfg,
        trigger_label=profile.trigger_label,
        bot_actor=profile.ignored_actors,
        trusted_actors=trusted,
        paused_label=cfg.paused_label,
        comment_trigger=profile.comment_trigger,
        in_progress_label=loop_cfg.in_progress_label,
    )
    continuing = verdict is gates.Verdict.CONTINUE
    needs_human = cfg.human_label in delivery.labels
    if verdict is not gates.Verdict.DISPATCH and not continuing:
        log.info(
            "forgejo hook: %s#%s refused by gate %s (actor=%s action=%s)",
            delivery.repo, delivery.number, verdict.value, delivery.actor, delivery.action,
        )
        return {"ok": False, "reason": verdict.value}

    if _repo_locked(client, loop_cfg, delivery.repo, loop_cfg.in_progress_label):
        log.info(
            "forgejo hook: %s#%s deferred — a run already holds the repo",
            delivery.repo, delivery.number,
        )
        if continuing and needs_human:
            # The drain skips issues a human holds; releasing it queues the reply.
            client.remove_label(delivery.repo, delivery.number, cfg.human_label)
        return {"ok": False, "reason": "repo-locked"}

    issue = client.get_issue(delivery.repo, delivery.number)
    common = dict(
        owner=cfg.owner,
        repo=delivery.repo,
        number=delivery.number,
        title=str(issue.get("title") or ""),
        issue_url=cfg.issue_url(delivery.repo, delivery.number),
    )
    if continuing:
        prompt = profiles.continue_turn(profile, **common, reply=delivery.body)
    else:
        prompt = profiles.first_turn(profile, **common)

    if _submit is None:  # pragma: no cover - wiring error, not a runtime path
        response.status_code = 503
        return {"ok": False, "reason": "no-submitter"}

    label = f"{delivery.repo}#{delivery.number}"
    if profile.agent is None:
        job_id = _submit(prompt, label)
    else:
        job_id = _submit(prompt, label, profile.agent)
    record = RunRecord(job_id=job_id, started_at=time.time())

    # Label AFTER a successful dispatch, so a submission that raises never leaves
    # a phantom lock freezing the repo — the same ordering poller.py uses.
    client.add_label(delivery.repo, delivery.number, loop_cfg.in_progress_label)
    if continuing:
        if needs_human:
            client.remove_label(delivery.repo, delivery.number, cfg.human_label)
        visible = f"Picked up Viktor's reply, continuing.\n\n_Fixer run `{job_id}`._"
    else:
        visible = (f"Picked this up — investigating. I will report what I find here, "
                   f"including anything I cannot fix.\n\n"
                   f"_Fixer run `{job_id}`._")
    client.comment(delivery.repo, delivery.number, render_comment(visible, record))
    reason = "continued" if continuing else "dispatched"
    log.info("forgejo hook: %s %s#%s as job %s",
             reason, delivery.repo, delivery.number, job_id)
    return {"ok": True, "reason": reason, "job_id": job_id, "issue": label}
