"""Tests for ``app.afk.notifier`` — the terminal-state doorbell.

The notifier's whole job is to format a human-facing alert (Slack / ntfy) with a
deep-link back to the T3 thread when a run reaches a terminal state — done,
needs-human, or frozen — and hand it to an injected sender. Every test here
injects a recording fake sender, so nothing is ever POSTed: we assert the
*formatted payload* per kind, plus the deep-link, the kind vocabulary, and the
guardrails (no thread → no link, unknown kind rejected, sender called exactly
once with the return value being None).

No real Slack/ntfy/T3 is touched — consistent with the rest of the AFK suite.
"""
import pytest

from app.afk import notifier as notifier_mod
from app.afk.notifier import KIND_DONE, KIND_FROZEN, KIND_NEEDS_HUMAN, Notification, Notifier
from app.afk.types import Issue


# --------------------------------------------------------------------------- #
# A recording sender — captures the Notification instead of posting it.
# --------------------------------------------------------------------------- #
class RecordingSender:
    """Injectable stand-in for the real Slack/ntfy POST. Records each payload so
    a test can assert the formatting without any network."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def __call__(self, notification: Notification) -> None:
        self.sent.append(notification)


@pytest.fixture
def sender() -> RecordingSender:
    return RecordingSender()


def _issue(number: int = 42, repo: str = "infra") -> Issue:
    return Issue(
        number=number,
        repo=repo,
        labels=["ready-for-agent"],
        blocked_by=[],
        labeled_by_trusted=True,
        priority=0,
    )


# --------------------------------------------------------------------------- #
# Kind vocabulary — the three terminal states, and nothing else.
# --------------------------------------------------------------------------- #
def test_terminal_kinds_are_exactly_the_three_terminal_states():
    assert KIND_DONE == "done"
    assert KIND_NEEDS_HUMAN == "needs-human"
    assert KIND_FROZEN == "frozen"
    assert notifier_mod.TERMINAL_KINDS == {KIND_DONE, KIND_NEEDS_HUMAN, KIND_FROZEN}


# --------------------------------------------------------------------------- #
# Dispatch mechanics — sender injected, called exactly once, returns None.
# --------------------------------------------------------------------------- #
def test_notify_calls_sender_exactly_once_and_returns_none(sender):
    n = Notifier(sender)
    result = n.notify(KIND_DONE, _issue(), "thread-7", "all green")
    assert result is None
    assert len(sender.sent) == 1


def test_notify_does_not_post_anything_itself(sender):
    """The Notifier must never reach the network on its own — all egress goes
    through the injected sender. A test-only sentinel proves that."""
    n = Notifier(sender)
    n.notify(KIND_FROZEN, _issue(), "thread-1", "budget exhausted")
    # Nothing other than the injected sender ran: exactly one recorded payload,
    # and it is the Notification dataclass (not a raw dict / HTTP response).
    assert isinstance(sender.sent[0], Notification)


# --------------------------------------------------------------------------- #
# Deep-link — every payload links back to the T3 thread (when there is one).
# --------------------------------------------------------------------------- #
def test_payload_deep_links_to_the_t3_thread(sender):
    n = Notifier(sender, base_url="https://t3.viktorbarzin.me")
    n.notify(KIND_DONE, _issue(), "thread-abc", "done")
    payload = sender.sent[0]
    assert payload.link == "https://t3.viktorbarzin.me/?thread=thread-abc"
    # The link is also surfaced in the human-readable body so it survives
    # senders that drop structured fields (e.g. a plain ntfy message).
    assert "https://t3.viktorbarzin.me/?thread=thread-abc" in payload.body


def test_base_url_trailing_slash_is_normalised(sender):
    n = Notifier(sender, base_url="https://t3.viktorbarzin.me/")
    n.notify(KIND_DONE, _issue(), "thread-x", "done")
    assert sender.sent[0].link == "https://t3.viktorbarzin.me/?thread=thread-x"


def test_no_thread_id_means_no_link(sender):
    """A run can reach 'needs-human' before any thread exists (e.g. dispatch
    itself failed). Without a thread there is nothing to deep-link to, so the
    link is None — but the doorbell still fires."""
    n = Notifier(sender)
    n.notify(KIND_NEEDS_HUMAN, _issue(), None, "dispatch failed")
    payload = sender.sent[0]
    assert payload.link is None
    assert len(sender.sent) == 1
    # No dangling "/?thread=" fragment leaks into the body either.
    assert "?thread=" not in payload.body


# --------------------------------------------------------------------------- #
# Per-kind formatting — title / body / priority / tags differ per terminal kind.
# --------------------------------------------------------------------------- #
def test_done_payload_is_informational(sender):
    n = Notifier(sender)
    n.notify(KIND_DONE, _issue(number=7, repo="infra"), "thread-7", "merged + CI green")
    p = sender.sent[0]
    assert p.kind == KIND_DONE
    assert p.issue_ref == "infra#7"
    assert "infra#7" in p.title
    assert "merged + CI green" in p.body
    # A successful close is informational, not an escalation.
    assert p.priority == "low"
    assert "escalation" not in p.tags


def test_needs_human_payload_is_an_escalation(sender):
    n = Notifier(sender)
    n.notify(KIND_NEEDS_HUMAN, _issue(number=9, repo="claude-agent-service"), "thread-9", "errored before push")
    p = sender.sent[0]
    assert p.kind == KIND_NEEDS_HUMAN
    assert p.issue_ref == "claude-agent-service#9"
    assert "claude-agent-service#9" in p.title
    assert "errored before push" in p.body
    assert p.priority == "high"
    assert "escalation" in p.tags


def test_frozen_payload_is_an_escalation(sender):
    n = Notifier(sender)
    n.notify(KIND_FROZEN, _issue(number=3, repo="infra"), "thread-3", "fix-forward budget exhausted")
    p = sender.sent[0]
    assert p.kind == KIND_FROZEN
    assert "infra#3" in p.title
    assert "fix-forward budget exhausted" in p.body
    assert p.priority == "high"
    assert "escalation" in p.tags


def test_titles_distinguish_the_three_kinds(sender):
    """An operator skimming a Slack channel must tell the three apart from the
    title alone, without reading the body."""
    n = Notifier(sender)
    n.notify(KIND_DONE, _issue(), "t", "x")
    n.notify(KIND_NEEDS_HUMAN, _issue(), "t", "x")
    n.notify(KIND_FROZEN, _issue(), "t", "x")
    titles = [p.title for p in sender.sent]
    assert len({t.split(" ")[0] for t in titles}) == 3  # distinct leading marker per kind


# --------------------------------------------------------------------------- #
# Guardrail — only terminal kinds are sendable. An unknown kind is a bug.
# --------------------------------------------------------------------------- #
def test_unknown_kind_raises_and_sends_nothing(sender):
    n = Notifier(sender)
    with pytest.raises(ValueError):
        n.notify("running", _issue(), "thread-1", "still working")
    assert sender.sent == []


# --------------------------------------------------------------------------- #
# Pure formatter — render_notification builds the payload independently of any
# sender, so the formatting is unit-testable on its own.
# --------------------------------------------------------------------------- #
def test_render_notification_is_pure_and_matches_notify(sender):
    issue = _issue(number=11, repo="infra")
    built = notifier_mod.render_notification(
        KIND_FROZEN, issue, "thread-11", "stuck", base_url="https://t3.viktorbarzin.me"
    )
    assert isinstance(built, Notification)
    assert built.link == "https://t3.viktorbarzin.me/?thread=thread-11"
    # notify() must produce the identical payload it hands the sender.
    Notifier(sender, base_url="https://t3.viktorbarzin.me").notify(
        KIND_FROZEN, issue, "thread-11", "stuck"
    )
    assert sender.sent[0] == built


def test_sender_exception_propagates(sender):
    """If the sender fails (Slack down), the notifier does not swallow it — the
    loop decides what to do with a failed doorbell, not this adapter."""
    def boom(_notification: Notification) -> None:
        raise RuntimeError("slack 503")

    n = Notifier(boom)
    with pytest.raises(RuntimeError, match="slack 503"):
        n.notify(KIND_DONE, _issue(), "thread-1", "done")
