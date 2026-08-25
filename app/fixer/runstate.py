"""Run state, carried in the issue itself.

Fixer runs are one-shot ``/execute`` jobs, so nothing in the process survives
between a dispatch and the tick that judges it. The bookkeeping the watcher needs
— which job, which pushed commit, how many fix-forward attempts, when the run
started, which issue it descends from — therefore lives in the issue, written as
a hidden footer on a normal ``infra-agent`` comment.

Two properties that made this the choice over a datastore:

  * **No new state to operate.** The tracker is already the source of truth for
    what is in flight (the ``agent-in-progress`` label), and a pod restart or a
    CronJob rescheduling loses nothing.
  * **The trail stays readable.** The visible half of the comment is what a human
    reads; the footer is an HTML comment, so it renders as nothing and travels
    with the thing it describes.

The footer is written on every state change, and ``parse`` takes the LAST one
found — so a run's history is append-only and the current state is the most
recent footer, never a mutated one.
"""
import json
import re
from dataclasses import asdict, dataclass, field

# An HTML comment renders as nothing in Forgejo's markdown, so the footer is
# invisible to a reader while remaining plain text in the API payload.
_FOOTER_RE = re.compile(r"<!--\s*fixer-state:\s*(\{.*?\})\s*-->", re.DOTALL)
_FOOTER_TEMPLATE = "<!-- fixer-state: {payload} -->"


@dataclass
class RunRecord:
    """One fixer run's bookkeeping, as stored in the issue.

    ``job_id`` is the ``/execute`` job the last dispatch created — the watcher
    polls it for liveness. ``commit`` is the sha the run pushed, or ``None``
    while nothing is pushed; ``pushed`` in the state machine is derived from it.
    ``started_at`` is a unix epoch seconds stamp, so elapsed time survives a
    process that did not.
    """

    job_id: str
    started_at: float
    commit: str | None = None
    fix_forward_attempts: int = 0
    chain_parent: int | None = None
    # Free-form notes a run leaves for its successor. Bounded by convention, not
    # by code: this is a hint channel, not a log.
    notes: list[str] = field(default_factory=list)

    def elapsed_seconds(self, now: float) -> float:
        """Wall-clock seconds since the run started, never negative."""
        return max(0.0, now - self.started_at)


def render_footer(record: RunRecord) -> str:
    """The hidden footer line for ``record``.

    Compact separators keep it to one line, which makes a diff of two footers
    readable when someone is debugging a chain by eye.
    """
    payload = json.dumps(asdict(record), sort_keys=True, separators=(",", ":"))
    return _FOOTER_TEMPLATE.format(payload=payload)


def render_comment(visible: str, record: RunRecord) -> str:
    """A complete comment body: what a person reads, then the hidden footer."""
    return f"{visible.rstrip()}\n\n{render_footer(record)}"


def parse_footer(body: str) -> RunRecord | None:
    """The record in one comment body, or ``None`` if it carries no footer.

    A malformed footer is treated as absent rather than raising: a run must not
    be wedged by one unparseable comment, and the next footer supersedes it.
    """
    matches = _FOOTER_RE.findall(body or "")
    if not matches:
        return None
    for raw in reversed(matches):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict) or "job_id" not in data:
            continue
        return RunRecord(
            job_id=str(data.get("job_id") or ""),
            started_at=float(data.get("started_at") or 0.0),
            commit=(str(data["commit"]) if data.get("commit") else None),
            fix_forward_attempts=int(data.get("fix_forward_attempts") or 0),
            chain_parent=(int(data["chain_parent"]) if data.get("chain_parent") else None),
            notes=[str(n) for n in (data.get("notes") or [])],
        )
    return None


def latest_record(comment_bodies: list[str]) -> RunRecord | None:
    """The current state of a run: the last parseable footer across its comments.

    Comments are expected oldest-first, which is the order Forgejo returns them.
    """
    for body in reversed(comment_bodies):
        record = parse_footer(body)
        if record is not None:
            return record
    return None


#: A git sha as an agent writes it in prose — 7 to 40 hex chars, on a word
#: boundary so ordinary words never match. Deliberately not anchored to a
#: sentence shape: runs phrase it differently every time ("pushed abc1234",
#: "commit `abc1234`", "landed as abc1234").
_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")

# Words that are all hex characters and would otherwise read as a sha. Short
# enough to be plausible in prose, so they are excluded by name rather than by
# a cleverer pattern.
_SHA_FALSE_FRIENDS = frozenset({
    "deadbeef", "cafebabe", "accede", "decade", "defaced", "effaced", "facade",
})


def find_pushed_commit(comment_bodies: list[str]) -> str | None:
    """The most recent commit sha mentioned across ``comment_bodies``.

    A fixer run states the sha it pushed in a comment, and this is what turns
    that prose into the ``commit`` the state machine needs. Later mentions win,
    so a fix-forward turn's sha supersedes the one before it.

    Returns ``None`` when nothing looks like a sha, which is the correct reading
    of "the run has not pushed yet".
    """
    for body in reversed(comment_bodies or []):
        # Skip the hidden footer: it carries a sha of its own, and reading it
        # back here would make the extraction circular.
        visible = _FOOTER_RE.sub("", body or "")
        matches = [m for m in _SHA_RE.findall(visible) if m not in _SHA_FALSE_FRIENDS]
        if matches:
            return matches[-1]
    return None
