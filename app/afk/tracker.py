"""Issue-tracker adapter — the loop's read/write port onto GitHub issues.

``Tracker`` is the only place the AFK loop touches the issue tracker. It wraps an
injected ``GitHubClient`` (the port) so the policy/state-machine code — and the
tests — never depend on a real ``gh`` or the network: production injects
``GhCliClient`` (shells out to ``gh`` with no-shell argv); tests inject a fake.

The split is deliberate. The ``GitHubClient`` port speaks only in *primitives*
(list raw issues for a label, fetch a single issue's label events, and the four
mutations). All the loop-specific *decisions* live on ``Tracker``:

  * ``labeled_by_trusted`` — decided **fail-closed** from the actor who made the
    most-recent application of the ready label. On private repos only
    collaborators can label, so the label *is* the authorization (design doc,
    "Trigger & dispatch predicate"); an unattributable label is never trusted.
  * ``blocked_by`` — the issue numbers in the body's "Blocked by #N" clauses
    (the per-issue dependency the design doc gates dispatch on).
  * ``priority`` — read off a ``priority:<n>`` label, lowest wins (lower runs
    first, matching ``Issue.priority`` semantics in ``types``).

Keeping the decisions here, not in the client, is what lets the whole read path
be tested against a thin fake. Mutations (``add_label`` / ``remove_label`` /
``comment`` / ``close``) are pass-throughs the loop drives during a run.
"""
import json
import re
from collections.abc import Callable
from subprocess import PIPE, run
from typing import Protocol, runtime_checkable

from .types import Issue

# Trusted author associations: GitHub tags each issue event actor with their
# association to the repo. Only these may arm an issue for the AFK loop — the
# trust gate from the design doc. Overridable per Tracker for a tighter policy.
DEFAULT_TRUSTED_ASSOCIATIONS: frozenset[str] = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# Default gating label; mirrors Config.ready_label so a Tracker built without an
# explicit override matches the production default.
DEFAULT_READY_LABEL = "ready-for-agent"

# "Blocked by #3, #4 and #10" → [3, 4, 10]. We match a "blocked by" lead-in
# (case-insensitive) and then harvest every "#<n>" in the clause that follows,
# up to the next line break — so a bare "#7 for context" elsewhere is ignored.
_BLOCKED_BY_CLAUSE = re.compile(r"blocked\s+by\b([^\n\r]*)", re.IGNORECASE)
_ISSUE_REF = re.compile(r"#(\d+)")

# "priority:2" → 2. Anything non-numeric (e.g. "priority:high") is not a numeric
# priority and is skipped.
_PRIORITY_LABEL = re.compile(r"^priority:(\d+)$")


@runtime_checkable
class GitHubClient(Protocol):
    """The primitive surface ``Tracker`` depends on — one issue tracker, faked
    in tests. Implementations must not embed loop policy; they only fetch raw
    data and perform the four mutations.

    ``list_issues`` returns the ``gh issue list --json number,labels,body`` shape
    (``labels`` is a list of ``{"name": ...}``; ``body`` may be ``None``).
    ``label_events`` returns the ``labeled`` timeline events for one issue, each
    with ``label.name``, ``actor.login`` and ``author_association``.
    """

    def list_issues(self, repo: str, label: str) -> list[dict]: ...
    def label_events(self, repo: str, number: int) -> list[dict]: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...
    def remove_label(self, repo: str, number: int, label: str) -> None: ...
    def comment(self, repo: str, number: int, body: str) -> None: ...
    def close(self, repo: str, number: int) -> None: ...


class Tracker:
    """Adapter that turns raw issue-tracker data into ``Issue`` records and
    relays mutations, over an injected :class:`GitHubClient`."""

    def __init__(
        self,
        client: GitHubClient,
        ready_label: str = DEFAULT_READY_LABEL,
        trusted_associations: frozenset[str] = DEFAULT_TRUSTED_ASSOCIATIONS,
    ) -> None:
        self.client = client
        self.ready_label = ready_label
        self.trusted_associations = trusted_associations

    # ----------------------------------------------------------------- reads #
    def list_ready(self, repos: list[str]) -> list[Issue]:
        """Every ready-labeled open issue across ``repos``, as ``Issue`` records.

        Ordering follows the client's per-repo order; dispatch ordering by
        priority is the dispatch policy's job, not the tracker's.
        """
        issues: list[Issue] = []
        for repo in repos:
            for raw in self.client.list_issues(repo, self.ready_label):
                issues.append(self._to_issue(repo, raw))
        return issues

    def _to_issue(self, repo: str, raw: dict) -> Issue:
        number = int(raw["number"])
        labels = [lbl["name"] for lbl in raw.get("labels", [])]
        return Issue(
            number=number,
            repo=repo,
            labels=labels,
            blocked_by=_parse_blocked_by(raw.get("body")),
            labeled_by_trusted=self._is_labeled_by_trusted(repo, number),
            priority=_parse_priority(labels),
        )

    def _is_labeled_by_trusted(self, repo: str, number: int) -> bool:
        """True iff the MOST RECENT application of the ready label was made by a
        trusted actor. Fail-closed: no attributable application → not trusted."""
        last_association: str | None = None
        for event in self.client.label_events(repo, number):
            if event.get("event") != "labeled":
                continue
            if (event.get("label") or {}).get("name") != self.ready_label:
                continue
            last_association = event.get("author_association")
        return last_association in self.trusted_associations

    # ------------------------------------------------------------- mutations #
    def add_label(self, repo: str, issue: int, label: str) -> None:
        self.client.add_label(repo, issue, label)

    def remove_label(self, repo: str, issue: int, label: str) -> None:
        self.client.remove_label(repo, issue, label)

    def comment(self, repo: str, issue: int, body: str) -> None:
        self.client.comment(repo, issue, body)

    def close(self, repo: str, issue: int) -> None:
        self.client.close(repo, issue)


# --------------------------------------------------------------------------- #
# Parsing helpers — pure functions, no I/O.
# --------------------------------------------------------------------------- #
def _parse_blocked_by(body: str | None) -> list[int]:
    """Issue numbers referenced in the body's "Blocked by #N" clauses.

    Order-preserving and de-duplicated; bare "#N" mentions outside a "blocked by"
    clause are ignored. A missing/empty body yields ``[]``.
    """
    if not body:
        return []
    seen: dict[int, None] = {}  # insertion-ordered set
    for clause in _BLOCKED_BY_CLAUSE.findall(body):
        for ref in _ISSUE_REF.findall(clause):
            seen.setdefault(int(ref), None)
    return list(seen)


def _parse_priority(labels: list[str]) -> int:
    """Numeric priority from a ``priority:<n>`` label, lowest wins; 0 if none."""
    priorities = [
        int(match.group(1))
        for label in labels
        if (match := _PRIORITY_LABEL.match(label))
    ]
    return min(priorities) if priorities else 0


# --------------------------------------------------------------------------- #
# Concrete client — shells out to `gh`. Injected `run` keeps it testable.
# --------------------------------------------------------------------------- #
def _default_run(argv: list[str]) -> str:
    """Run ``argv`` with no shell and return stdout (text). Raises on non-zero.

    List argv (never a shell string), matching the no-injection-surface pattern
    the breakglass/main subprocess helpers use — the repo/label/body values are
    never interpreted by a shell.
    """
    proc = run(argv, stdout=PIPE, stderr=PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed ({proc.returncode}): {proc.stderr[:200]}")
    return proc.stdout


class GhCliClient:
    """:class:`GitHubClient` backed by the ``gh`` CLI.

    ``repo_owner`` is the GitHub owner/org the sub-project repos live under, so a
    bare repo name (``"infra"``) becomes the ``--repo owner/infra`` slug ``gh``
    wants. ``run`` is the subprocess runner (defaults to the real no-shell one);
    tests inject a fake to capture argv without spawning ``gh``.
    """

    def __init__(self, repo_owner: str, run: Callable[[list[str]], str] = _default_run) -> None:
        self.repo_owner = repo_owner
        self._run = run

    def _slug(self, repo: str) -> str:
        return f"{self.repo_owner}/{repo}"

    def list_issues(self, repo: str, label: str) -> list[dict]:
        out = self._run([
            "gh", "issue", "list", "--repo", self._slug(repo),
            "--label", label, "--state", "open",
            "--json", "number,labels,body", "--limit", "100",
        ])
        return _loads_list(out)

    def label_events(self, repo: str, number: int) -> list[dict]:
        out = self._run([
            "gh", "api",
            f"repos/{self._slug(repo)}/issues/{number}/timeline",
            "--paginate",
            "-H", "Accept: application/vnd.github+json",
        ])
        events = _loads_list(out)
        return [e for e in events if e.get("event") == "labeled"]

    def add_label(self, repo: str, number: int, label: str) -> None:
        self._run([
            "gh", "issue", "edit", str(number), "--repo", self._slug(repo),
            "--add-label", label,
        ])

    def remove_label(self, repo: str, number: int, label: str) -> None:
        self._run([
            "gh", "issue", "edit", str(number), "--repo", self._slug(repo),
            "--remove-label", label,
        ])

    def comment(self, repo: str, number: int, body: str) -> None:
        self._run([
            "gh", "issue", "comment", str(number), "--repo", self._slug(repo),
            "--body", body,
        ])

    def close(self, repo: str, number: int) -> None:
        self._run(["gh", "issue", "close", str(number), "--repo", self._slug(repo)])


def _loads_list(out: str) -> list[dict]:
    """Parse ``gh`` JSON stdout into a list of dicts. Empty stdout → ``[]``."""
    text = out.strip()
    if not text:
        return []
    return json.loads(text)
