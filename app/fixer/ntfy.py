"""The doorbell's transport: ntfy.

``app.afk.notifier`` builds the notification and hands it to an injected sender;
this is that sender, posting to the self-hosted ntfy instance. It maps the
notification's fields onto ntfy's header protocol — title, priority, tags, and a
click-through — so an escalation arrives on a phone looking like an alert rather
than a wall of text.

Deliberately thin: no retries, no swallowing. The notifier's contract is that a
sender failure propagates so the caller decides, and a doorbell that silently
fails is worse than one that raises.
"""
import logging
import urllib.error
import urllib.request
from collections.abc import Callable

from app.afk.notifier import Notification
from app.afk.types import Issue

log = logging.getLogger(__name__)

# ntfy priorities are 1..5; the notifier speaks "low" / "high".
_PRIORITY = {"low": "2", "high": "5"}

Poster = Callable[[str, bytes, dict[str, str]], int]


def _urllib_poster(url: str, body: bytes, headers: dict[str, str]) -> int:
    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def forgejo_link(base_url: str, issue: Issue, thread_id: str | None) -> str | None:
    """Link builder for the notifier: point at the issue, not at a thread.

    ``base_url`` is the Forgejo web root and the owner is folded in by the
    caller's config, so this reads ``<web>/<owner>/<repo>/issues/<n>``. The
    owner is taken from the base url to keep the notifier free of fixer config;
    pass a base that already includes it.
    """
    return f"{base_url.rstrip('/')}/{issue.repo}/issues/{issue.number}"


def make_sender(
    ntfy_url: str, topic: str, token: str = "", poster: Poster | None = None
):
    """Build a notifier sender that posts to ``topic`` on ``ntfy_url``.

    ``token`` is required in practice: this ntfy runs
    ``NTFY_AUTH_DEFAULT_ACCESS=deny-all``, so an unauthenticated publish is a
    403 and the doorbell never rings. The fixer publishes as a write-only user
    scoped to this one topic.
    """
    send: Poster = poster or _urllib_poster
    endpoint = f"{ntfy_url.rstrip('/')}/{topic}"

    def sender(notification: Notification) -> None:
        headers = {
            "Title": notification.title,
            "Priority": _PRIORITY.get(notification.priority, "3"),
            "Tags": ",".join(notification.tags),
        }
        if notification.link:
            headers["Click"] = notification.link
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status = send(endpoint, notification.body.encode("utf-8"), headers)
        if status >= 400:
            raise RuntimeError(f"ntfy {endpoint} -> HTTP {status}")
        log.info("doorbell: %s %s -> ntfy %s",
                 notification.kind, notification.issue_ref, topic)

    return sender
