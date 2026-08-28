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
    #: Restarts spent after the runner lost the job (a replaced process, not a
    #: failed turn). Its own budget, separate from ``fix_forward_attempts``.
    redispatch_attempts: int = 0
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
            # Absent in footers written before this field existed, which a run
            # in flight across the upgrade will have; 0 is the right reading.
            redispatch_attempts=int(data.get("redispatch_attempts") or 0),
            chain_parent=(int(data["chain_parent"]) if data.get("chain_parent") else None),
            notes=[str(n) for n in (data.get("notes") or [])],
        )
    return None


def all_job_ids(comment_bodies: list[str]) -> set[str]:
    """Every job id this thread has recorded, across all footers.

    A fix-forward chain accumulates one job id per turn, and each appears in the
    prose of its own comment ("Fixer run `abc123def456`"). They are hex, so every
    one of them can be mistaken for a pushed commit — not just the current run's.
    Excluding the whole set is what makes :func:`find_pushed_commit` correct on a
    thread with history rather than only on a fresh one.
    """
    out: set[str] = set()
    for body in comment_bodies or []:
        record = parse_footer(body)
        if record is not None and record.job_id:
            out.add(record.job_id)
    return out


def latest_record(comment_bodies: list[str]) -> RunRecord | None:
    """The current state of a run: the last parseable footer across its comments.

    Comments are expected oldest-first, which is the order Forgejo returns them.
    """
    for body in reversed(comment_bodies):
        record = parse_footer(body)
        if record is not None:
            return record
    return None


#: The explicit marker a run uses to declare what it pushed. Required, and the
#: ONLY thing read as a commit — see :func:`find_pushed_commit`.
_MARKER_RE = re.compile(r"^\s*Pushed-Commit:\s*`?([0-9a-f]{7,40})`?\s*$",
                        re.IGNORECASE | re.MULTILINE)


def find_pushed_commit(
    comment_bodies: list[str], exclude: frozenset[str] | set[str] = frozenset()
) -> str | None:
    """The commit a run declared it pushed, or ``None`` if it declared none.

    Only an explicit ``Pushed-Commit: <sha>`` line counts. Inferring a sha from
    prose was tried first and is not viable: hex strings of commit length are
    everywhere in a real report — a run's own 12-character job id, a container
    image tag (``b0ef3eca``), a digest fragment — and every false positive makes
    a run that pushed nothing look pushed, after which the state machine waits
    forever on CI for a commit that does not exist.

    A run that pushed but omitted the marker therefore reads as not-pushed, which
    escalates to a human rather than hanging. That is the safe direction: a
    missing marker costs one notification, a phantom commit costs a stuck run.

    Later declarations win, so a fix-forward turn's sha supersedes the one before
    it. ``exclude`` remains available for a value that must never be treated as a
    commit even if declared.
    """
    skip = {str(x) for x in exclude}
    for body in reversed(comment_bodies or []):
        # Skip the hidden footer: it carries the job id and any recorded commit,
        # and reading those back here would make the extraction circular.
        visible = _FOOTER_RE.sub("", body or "")
        matches = [m for m in _MARKER_RE.findall(visible) if m not in skip]
        if matches:
            return matches[-1].lower()
    return None
