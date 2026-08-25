"""Fixer configuration, read from the environment.

The loop's own knobs (allowlist, kill switch, labels) already live in
``app.afk.config``; this holds only what is specific to the Forgejo trigger —
where the forge is, who the bot is, what to dispatch, and where the doorbell
rings.

Two deliberate defaults:

  * **No budget or timeout ceiling.** ``max_budget_usd`` and ``timeout_seconds``
    are ``None``, so a run is never truncated mid-diagnosis (design doc,
    decision 14). Burn rate is bounded by the per-repo lock instead — one run at
    a time — not by caps. Both remain overridable per environment.
  * **Fail closed on the secret.** With no ``FIXER_WEBHOOK_SECRET`` the receiver
    refuses every delivery: an unsigned webhook endpoint that dispatches an agent
    with cluster write is not a state worth having, even briefly.
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass

ENV_FORGEJO_API = "FIXER_FORGEJO_API"
ENV_FORGEJO_WEB = "FIXER_FORGEJO_WEB"
ENV_FORGEJO_OWNER = "FIXER_FORGEJO_OWNER"
ENV_FORGEJO_TOKEN = "FIXER_FORGEJO_TOKEN"
ENV_WEBHOOK_SECRET = "FIXER_WEBHOOK_SECRET"
ENV_BOT_ACTOR = "FIXER_BOT_ACTOR"
ENV_TRIGGER_LABEL = "FIXER_TRIGGER_LABEL"
ENV_PAUSED_LABEL = "FIXER_PAUSED_LABEL"
ENV_HUMAN_LABEL = "FIXER_HUMAN_LABEL"
ENV_ESCALATE_TO = "FIXER_ESCALATE_TO"
ENV_AGENT = "FIXER_AGENT"
ENV_NTFY_URL = "FIXER_NTFY_URL"
ENV_NTFY_TOPIC = "FIXER_NTFY_TOPIC"
ENV_MAX_BUDGET_USD = "FIXER_MAX_BUDGET_USD"
ENV_TIMEOUT_SECONDS = "FIXER_TIMEOUT_SECONDS"

DEFAULT_FORGEJO_API = "https://forgejo.viktorbarzin.me/api/v1"
DEFAULT_FORGEJO_WEB = "https://forgejo.viktorbarzin.me"
DEFAULT_OWNER = "viktor"
DEFAULT_BOT_ACTOR = "infra-agent"
DEFAULT_TRIGGER_LABEL = "broken"
DEFAULT_PAUSED_LABEL = "paused"
DEFAULT_HUMAN_LABEL = "needs-human"
DEFAULT_ESCALATE_TO = "viktor"
DEFAULT_AGENT = ".claude/agents/issue-responder"
DEFAULT_NTFY_URL = "https://ntfy.viktorbarzin.me"
DEFAULT_NTFY_TOPIC = "fixer"


@dataclass(frozen=True)
class FixerConfig:
    """Everything the Forgejo trigger needs to know about its surroundings."""

    forgejo_api: str = DEFAULT_FORGEJO_API
    forgejo_web: str = DEFAULT_FORGEJO_WEB
    owner: str = DEFAULT_OWNER
    token: str = ""
    webhook_secret: str = ""
    bot_actor: str = DEFAULT_BOT_ACTOR
    trigger_label: str = DEFAULT_TRIGGER_LABEL
    paused_label: str = DEFAULT_PAUSED_LABEL
    human_label: str = DEFAULT_HUMAN_LABEL
    escalate_to: str = DEFAULT_ESCALATE_TO
    agent: str = DEFAULT_AGENT
    ntfy_url: str = DEFAULT_NTFY_URL
    ntfy_topic: str = DEFAULT_NTFY_TOPIC
    max_budget_usd: float | None = None
    timeout_seconds: int | None = None

    @property
    def configured(self) -> bool:
        """Whether the receiver can safely admit a delivery at all.

        Both halves are required: the secret proves a delivery came from our
        Forgejo, and the token is what every follow-up action uses. Missing
        either means the endpoint answers "not configured" rather than acting on
        an unverifiable request.
        """
        return bool(self.webhook_secret) and bool(self.token)

    def issue_url(self, repo: str, number: int) -> str:
        """The human URL for an issue — what the doorbell links to."""
        return f"{self.forgejo_web.rstrip('/')}/{self.owner}/{repo}/issues/{number}"


def from_env(env: Mapping[str, str] | None = None) -> FixerConfig:
    """Load from the environment, falling back to the defaults above."""
    e = env if env is not None else os.environ

    def opt_float(key: str) -> float | None:
        raw = (e.get(key) or "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def opt_int(key: str) -> int | None:
        raw = (e.get(key) or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def text(key: str, fallback: str) -> str:
        raw = (e.get(key) or "").strip()
        return raw or fallback

    return FixerConfig(
        forgejo_api=text(ENV_FORGEJO_API, DEFAULT_FORGEJO_API),
        forgejo_web=text(ENV_FORGEJO_WEB, DEFAULT_FORGEJO_WEB),
        owner=text(ENV_FORGEJO_OWNER, DEFAULT_OWNER),
        token=text(ENV_FORGEJO_TOKEN, ""),
        webhook_secret=text(ENV_WEBHOOK_SECRET, ""),
        bot_actor=text(ENV_BOT_ACTOR, DEFAULT_BOT_ACTOR),
        trigger_label=text(ENV_TRIGGER_LABEL, DEFAULT_TRIGGER_LABEL),
        paused_label=text(ENV_PAUSED_LABEL, DEFAULT_PAUSED_LABEL),
        human_label=text(ENV_HUMAN_LABEL, DEFAULT_HUMAN_LABEL),
        escalate_to=text(ENV_ESCALATE_TO, DEFAULT_ESCALATE_TO),
        agent=text(ENV_AGENT, DEFAULT_AGENT),
        ntfy_url=text(ENV_NTFY_URL, DEFAULT_NTFY_URL),
        ntfy_topic=text(ENV_NTFY_TOPIC, DEFAULT_NTFY_TOPIC),
        max_budget_usd=opt_float(ENV_MAX_BUDGET_USD),
        timeout_seconds=opt_int(ENV_TIMEOUT_SECONDS),
    )
