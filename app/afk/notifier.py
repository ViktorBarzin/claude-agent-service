"""Terminal-state doorbell for the AFK loop — Slack / ntfy escalation sink.

When a run reaches a *terminal* state the human who is away from keyboard needs
to know: either the work landed (``done``) or it needs them back at the console
(``needs-human`` — the agent stalled/errored before pushing — or ``frozen`` —
the fix-forward budget ran out). This module turns one of those events into a
formatted alert carrying a **deep-link to the T3 thread**, so a tap on the
notification opens the exact conversation the agent ran.

Design, matching the rest of ``app.afk`` and the breakglass code:

  * ``Notifier`` owns no transport. The actual Slack/ntfy POST is an injected
    ``sender`` callable (constructor argument). Production wires a real HTTP
    sender; tests inject a recording fake and assert the formatted payload
    without touching the network — the same dependency-injection seam breakglass
    uses for the claude subprocess.
  * ``render_notification`` is a pure function that builds the payload; ``notify``
    is just "render, then hand to the sender". Keeping the formatting pure makes
    it unit-testable on its own and guarantees ``notify`` sends exactly what
    ``render_notification`` returns.
  * The kind vocabulary is CLOSED: only the three terminal kinds are sendable.
    An unknown kind raises rather than firing a mystery doorbell — a non-terminal
    kind reaching here is a caller bug, not something to paper over.
  * The notifier never swallows a sender failure. If Slack is down the exception
    propagates; the loop decides whether to retry or give up, not this adapter.

The whole AFK loop ships DISABLED (see ``config.py``); this module is inert
until the loop is deliberately armed and a real sender is wired in.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from .types import Issue

# --------------------------------------------------------------------------- #
# Kind vocabulary — the terminal states a run can reach. One source of truth
# shared by callers (the state machine maps Action -> kind) and tests.
# --------------------------------------------------------------------------- #
KIND_DONE = "done"                  # landed: merged + CI green, issue closeable
KIND_NEEDS_HUMAN = "needs-human"    # stalled/errored before pushing — pre-push escalation
KIND_FROZEN = "frozen"              # fix-forward budget (attempts/wall-clock) exhausted

#: The only kinds ``notify`` will send. Anything else is a caller bug.
TERMINAL_KINDS: frozenset[str] = frozenset({KIND_DONE, KIND_NEEDS_HUMAN, KIND_FROZEN})

# Default T3 web UI. Threads deep-link off this; overridable per-Notifier so the
# host isn't hardcoded into the formatter (re-IP / staging / tests).
DEFAULT_BASE_URL = "https://t3.viktorbarzin.me"

# Per-kind presentation. The leading marker makes the three distinguishable from
# the title alone in a crowded Slack channel without emoji; priority/tags drive
# how the sender routes it (a successful close is quiet; the two escalations are
# loud and tagged so on-call filters can page on them).
_PRESENTATION: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    # kind            -> (marker,     headline,                 priority, tags)
    KIND_DONE:         ("[DONE]",     "landed",                 "low",  ("afk", "done")),
    KIND_NEEDS_HUMAN:  ("[NEEDS-HUMAN]", "needs a human",       "high", ("afk", "escalation", "needs-human")),
    KIND_FROZEN:       ("[FROZEN]",   "frozen — budget exhausted", "high", ("afk", "escalation", "frozen")),
}

#: A sink that delivers a built notification (HTTP POST in prod, recorder in tests).
Sender = Callable[["Notification"], None]


@dataclass
class Notification:
    """The fully-formatted alert handed to the sender.

    A structured payload (not a raw dict) so the sender can map fields onto its
    own schema — ``title``/``body`` for Slack blocks or an ntfy message,
    ``priority``/``tags`` for routing, ``link`` for the click-through. ``link``
    is ``None`` when there is no thread to point at (e.g. dispatch failed before
    a thread existed); the deep-link is also embedded in ``body`` so it survives
    senders that only carry a plain message.
    """

    kind: str
    issue_ref: str            # "<repo>#<number>", e.g. "infra#42"
    title: str
    body: str
    link: str | None
    priority: str             # "low" | "high" — escalation loudness for the sender
    tags: list[str] = field(default_factory=list)


def _deep_link(base_url: str, thread_id: str | None) -> str | None:
    """Build the T3 thread deep-link, or ``None`` when there is no thread."""
    if not thread_id:
        return None
    return f"{base_url.rstrip('/')}/?thread={thread_id}"


def render_notification(
    kind: str,
    issue: Issue,
    thread_id: str | None,
    detail: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> Notification:
    """Build the :class:`Notification` for a terminal event — pure, no I/O.

    Raises ``ValueError`` if ``kind`` is not one of :data:`TERMINAL_KINDS`: only
    terminal states ring the doorbell, and a non-terminal kind reaching here is a
    bug we surface rather than silently send.
    """
    if kind not in TERMINAL_KINDS:
        raise ValueError(
            f"notifier only sends terminal kinds {sorted(TERMINAL_KINDS)}, got {kind!r}"
        )

    marker, headline, priority, tags = _PRESENTATION[kind]
    issue_ref = f"{issue.repo}#{issue.number}"
    link = _deep_link(base_url, thread_id)

    title = f"{marker} {issue_ref} {headline}"

    body_lines = [detail]
    if link is not None:
        body_lines.append(f"Thread: {link}")
    body = "\n".join(body_lines)

    return Notification(
        kind=kind,
        issue_ref=issue_ref,
        title=title,
        body=body,
        link=link,
        priority=priority,
        tags=list(tags),
    )


class Notifier:
    """Sends terminal-state doorbells through an injected ``sender``.

    The ``sender`` is the only egress: ``notify`` formats the payload (via
    :func:`render_notification`) and hands it over. No transport lives here, so a
    test injects a recording fake and asserts the payload without posting.
    """

    def __init__(self, sender: Sender, *, base_url: str = DEFAULT_BASE_URL) -> None:
        self._sender = sender
        self._base_url = base_url

    def notify(self, kind: str, issue: Issue, thread_id: str | None, detail: str) -> None:
        """Format a terminal-state alert and deliver it via the injected sender.

        Raises ``ValueError`` for a non-terminal ``kind`` (before any send), and
        lets a sender failure propagate — see the module docstring.
        """
        notification = render_notification(
            kind, issue, thread_id, detail, base_url=self._base_url
        )
        self._sender(notification)
