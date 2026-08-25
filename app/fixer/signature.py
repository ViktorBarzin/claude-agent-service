"""Webhook signature verification.

Forgejo signs a webhook delivery with HMAC-SHA256 over the raw request body,
using the secret configured on the hook, and sends the hex digest in a header.
The header name differs by forge generation, so both spellings are accepted:
``X-Forgejo-Signature`` (Forgejo) and ``X-Gitea-Signature`` (the Gitea-compatible
alias Forgejo also sends). The design doc lists confirming this against a live
delivery as an open question; accepting either spelling is what makes the answer
not matter.

Two properties worth stating, because both are easy to get subtly wrong:

  * **Constant-time comparison.** ``hmac.compare_digest`` throughout, so a
    wrong signature leaks nothing through timing.
  * **The RAW body is what is signed.** Verification must run before any JSON
    parsing and re-serialisation, since ``json.dumps(json.loads(x))`` is not
    ``x``. The receiver reads the body once, verifies those exact bytes, and
    only then parses.
"""
import hmac
from collections.abc import Mapping
from hashlib import sha256

HEADER_FORGEJO = "x-forgejo-signature"
HEADER_GITEA = "x-gitea-signature"
SIGNATURE_HEADERS = (HEADER_FORGEJO, HEADER_GITEA)

EVENT_HEADERS = ("x-forgejo-event", "x-gitea-event")


def expected_signature(secret: str, body: bytes) -> str:
    """The hex HMAC-SHA256 digest Forgejo should have sent for ``body``."""
    return hmac.new(secret.encode(), body, sha256).hexdigest()


def verify(secret: str, body: bytes, headers: Mapping[str, str]) -> bool:
    """Whether ``body`` carries a valid signature for ``secret``.

    Fails closed on every ambiguity: an empty secret, a missing header, or a
    malformed value is not a valid delivery.
    """
    if not secret:
        return False
    want = expected_signature(secret, body)
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in SIGNATURE_HEADERS:
        got = (lowered.get(name) or "").strip()
        if got and hmac.compare_digest(got, want):
            return True
    return False


def event_name(headers: Mapping[str, str]) -> str:
    """The delivery's event name, from whichever header spelling is present."""
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in EVENT_HEADERS:
        value = (lowered.get(name) or "").strip()
        if value:
            return value
    return ""
