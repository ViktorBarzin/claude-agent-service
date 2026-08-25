"""The webhook admission gates — pure, so the whole matrix is testable.

A Forgejo webhook delivery reaches ``POST /hooks/forgejo`` and this module
answers one question about it: **should this delivery start a fixer run?** It
reads only its arguments — no clock, no network, no tracker — so the receiver
owns every side effect and the policy stays a decision table the tests can
exhaust.

The gates, first match wins (design doc "Trigger and authorization"):

  1. **Event shape** — the delivery must be an issue event that either opened an
     issue already carrying the trigger label, or added the trigger label to an
     existing one. Everything else (a comment, a push, a milestone, an issue
     closed) is not a trigger.
  2. **Loop guard** — a delivery whose actor is the fixer's own bot account is
     ignored. The fixer labels issues during triage (``incident``, ``sev2``,
     ``agent-in-progress``), and without this an agent's own label would
     re-dispatch it.
  3. **Brake** — the ``paused`` label on the issue stops the next dispatch for
     that issue, instantly and without a deploy.
  4. **Kill switch** — ``config.kill_switch`` stops all dispatch globally.
  5. **Trust** — the actor must be a trusted collaborator. On a private repo only
     collaborators can label at all, so the label is the authorization; this gate
     is defence in depth against a delivery that was not what it claimed. The
     caller supplies the trusted set, so this module never asks the network who
     is a collaborator.
  6. **Repo enrolment** — the repo must be in ``config.allowlist``. An empty
     allowlist admits nothing, which is the shipped default.

The per-repo lock is deliberately NOT here. It is a live read of what is
in flight, so it belongs to the dispatch path in the receiver, which already
holds a tracker; keeping it out keeps this function pure.
"""
from dataclasses import dataclass
from enum import Enum

from app.afk.types import Config

# Forgejo delivery actions that can carry a newly-applied label. ``opened``
# covers "filed with the label already on it" (what a filing tool does);
# ``label_updated`` covers "someone added it afterwards". Verified against a
# live delivery before this shipped — see the design doc's open questions.
ACTION_OPENED = "opened"
ACTION_LABEL_UPDATED = "label_updated"
TRIGGERING_ACTIONS = frozenset({ACTION_OPENED, ACTION_LABEL_UPDATED})

# The event name Forgejo sends in X-Forgejo-Event for issue and issue-label
# deliveries. Both arrive under the same header value in Forgejo 11.x; the
# action field is what distinguishes them.
EVENT_ISSUES = "issues"
EVENT_ISSUE_LABEL = "issue_label"
TRIGGERING_EVENTS = frozenset({EVENT_ISSUES, EVENT_ISSUE_LABEL})


class Verdict(Enum):
    """Why a delivery was admitted or refused. One value per gate, so a refusal
    is self-explaining in the log line and in the tests."""

    DISPATCH = "dispatch"
    NOT_A_TRIGGER = "not-a-trigger"          # wrong event, action, or no label
    OWN_ACTION = "own-action"                # the fixer's own bot labelled it
    PAUSED = "paused"                        # the per-issue brake
    KILL_SWITCH = "kill-switch"              # the global brake
    UNTRUSTED_ACTOR = "untrusted-actor"      # not a collaborator
    REPO_NOT_ENROLLED = "repo-not-enrolled"  # not in the allowlist


@dataclass(frozen=True)
class Delivery:
    """One webhook delivery, reduced to the fields the gates read.

    ``labels`` is the issue's label set *after* the event, which is what both
    triggering actions leave behind: an issue opened with ``broken`` on it, and
    an issue that just gained ``broken``, are indistinguishable here — and should
    be.
    """

    event: str
    action: str
    repo: str
    number: int
    actor: str
    labels: frozenset[str]
    state: str = "open"


def decide(
    delivery: Delivery,
    config: Config,
    *,
    trigger_label: str,
    bot_actor: str,
    trusted_actors: frozenset[str],
    paused_label: str = "paused",
) -> Verdict:
    """Whether ``delivery`` should start a fixer run, and if not, which gate said no.

    ``trusted_actors`` is supplied by the caller (from the repo's collaborator
    list) rather than looked up here, so this stays a pure function.
    """
    if delivery.event not in TRIGGERING_EVENTS:
        return Verdict.NOT_A_TRIGGER
    if delivery.action not in TRIGGERING_ACTIONS:
        return Verdict.NOT_A_TRIGGER
    if delivery.state != "open":
        return Verdict.NOT_A_TRIGGER
    if trigger_label not in delivery.labels:
        return Verdict.NOT_A_TRIGGER

    # Loop guard before every remaining gate: the bot's own labelling must never
    # reach a dispatch decision, whatever else is true of the delivery.
    if delivery.actor == bot_actor:
        return Verdict.OWN_ACTION

    if paused_label in delivery.labels:
        return Verdict.PAUSED
    if config.kill_switch:
        return Verdict.KILL_SWITCH
    if delivery.actor not in trusted_actors:
        return Verdict.UNTRUSTED_ACTOR
    if delivery.repo not in config.allowlist:
        return Verdict.REPO_NOT_ENROLLED
    return Verdict.DISPATCH


def parse_delivery(event: str, payload: dict, repo_name: str | None = None) -> Delivery | None:
    """Reduce a raw Forgejo issue payload to a :class:`Delivery`.

    Returns ``None`` when the payload is not an issue event at all (no ``issue``
    object), which the receiver answers 204 rather than 400: Forgejo will send
    other event types if the hook's subscription is ever widened, and an
    unrecognised delivery is a no-op, not an error.

    ``repo_name`` overrides the repository's own name, which the receiver uses to
    keep the enrolment check keyed on the short name the allowlist speaks
    (``infra``, not ``viktor/infra``).
    """
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return None
    repo = payload.get("repository") or {}
    labels = issue.get("labels") or []
    sender = payload.get("sender") or {}
    return Delivery(
        event=event,
        action=str(payload.get("action") or ""),
        repo=repo_name or str(repo.get("name") or ""),
        number=int(issue.get("number") or 0),
        actor=str(sender.get("login") or ""),
        labels=frozenset(
            str(lbl.get("name")) for lbl in labels if isinstance(lbl, dict) and lbl.get("name")
        ),
        state=str(issue.get("state") or "open"),
    )
