"""Tests for ``app.afk.tracker`` — the GitHub issues adapter.

The ``Tracker`` is the loop's read/write port onto the issue tracker. It wraps
an injected GitHub client (the real one shells out to ``gh``; here we inject a
FAKE that records calls and replays staged data) and holds all the *business*
logic the loop depends on: turning raw issues into ``Issue`` records with
``blocked_by`` parsed, ``labeled_by_trusted`` decided fail-closed from the label
event actor, and ``priority`` read off a priority label. No test here reaches a
real ``gh``, GitHub/Forgejo, or the network.
"""
import pytest

from app.afk.tracker import (
    DEFAULT_TRUSTED_ASSOCIATIONS,
    GitHubClient,
    Tracker,
)
from app.afk.types import Issue


# --------------------------------------------------------------------------- #
# Fake GitHub client — the injected port. Records every mutating call and
# replays issues / label-events staged per repo. Implements the GitHubClient
# Protocol the Tracker depends on.
# --------------------------------------------------------------------------- #
class FakeGitHub:
    def __init__(self) -> None:
        # repo -> list of raw issue dicts (gh issue list --json shape)
        self._issues: dict[str, list[dict]] = {}
        # (repo, number) -> list of label-event dicts (who added which label)
        self._events: dict[tuple[str, int], list[dict]] = {}
        # recorded mutations
        self.labels_added: list[tuple[str, int, str]] = []
        self.labels_removed: list[tuple[str, int, str]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.closed: list[tuple[str, int]] = []

    # --- staging helpers (test-only) --- #
    def seed_issues(self, repo: str, issues: list[dict]) -> None:
        self._issues[repo] = issues

    def seed_label_events(self, repo: str, number: int, events: list[dict]) -> None:
        self._events[(repo, number)] = events

    # --- GitHubClient surface --- #
    def list_issues(self, repo: str, label: str) -> list[dict]:
        return [
            issue
            for issue in self._issues.get(repo, [])
            if label in [lbl["name"] for lbl in issue.get("labels", [])]
        ]

    def label_events(self, repo: str, number: int) -> list[dict]:
        return list(self._events.get((repo, number), []))

    def add_label(self, repo: str, number: int, label: str) -> None:
        self.labels_added.append((repo, number, label))

    def remove_label(self, repo: str, number: int, label: str) -> None:
        self.labels_removed.append((repo, number, label))

    def comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append((repo, number, body))

    def close(self, repo: str, number: int) -> None:
        self.closed.append((repo, number))


# --------------------------------------------------------------------------- #
# Raw-issue / event builders matching the gh JSON shapes the real client emits.
# --------------------------------------------------------------------------- #
def _raw_issue(
    number: int = 1,
    labels: list[str] | None = None,
    body: str = "",
) -> dict:
    return {
        "number": number,
        "labels": [{"name": name} for name in (labels or ["ready-for-agent"])],
        "body": body,
    }


def _label_event(label: str, association: str = "OWNER", actor: str = "viktorbarzin") -> dict:
    # Mirrors the `gh api .../timeline` "labeled" event shape we care about.
    return {
        "event": "labeled",
        "label": {"name": label},
        "actor": {"login": actor},
        "author_association": association,
    }


@pytest.fixture
def gh() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def tracker(gh: FakeGitHub) -> Tracker:
    return Tracker(gh)


# --------------------------------------------------------------------------- #
# Construction / contract.
# --------------------------------------------------------------------------- #
def test_tracker_wraps_injected_client(gh: FakeGitHub):
    t = Tracker(gh)
    assert t.client is gh


def test_fake_satisfies_protocol(gh: FakeGitHub):
    # The fake must be usable where a GitHubClient is expected (structural typing).
    assert isinstance(gh, GitHubClient)


def test_default_trusted_associations_are_collaborator_or_above():
    assert DEFAULT_TRUSTED_ASSOCIATIONS == frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


# --------------------------------------------------------------------------- #
# list_ready — the read path that builds Issue records.
# --------------------------------------------------------------------------- #
def test_list_ready_returns_issue_objects(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=7)])
    gh.seed_label_events("infra", 7, [_label_event("ready-for-agent")])

    issues = tracker.list_ready(["infra"])

    assert len(issues) == 1
    issue = issues[0]
    assert isinstance(issue, Issue)
    assert issue.number == 7
    assert issue.repo == "infra"
    assert issue.labels == ["ready-for-agent"]


def test_list_ready_spans_multiple_repos(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=1)])
    gh.seed_issues("crawler", [_raw_issue(number=2)])
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent")])
    gh.seed_label_events("crawler", 2, [_label_event("ready-for-agent")])

    issues = tracker.list_ready(["infra", "crawler"])

    assert {(i.repo, i.number) for i in issues} == {("infra", 1), ("crawler", 2)}


def test_list_ready_empty_when_no_ready_issues(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=1, labels=["bug"])])
    assert tracker.list_ready(["infra"]) == []


def test_list_ready_queries_with_configured_ready_label(gh: FakeGitHub):
    # A Tracker built with a custom ready label must query the client for *that*
    # label, not the default.
    seen: dict[str, str] = {}

    class _RecordingGitHub(FakeGitHub):
        def list_issues(self, repo: str, label: str) -> list[dict]:
            seen["label"] = label
            return super().list_issues(repo, label)

    rec = _RecordingGitHub()
    rec.seed_issues("infra", [_raw_issue(number=1, labels=["queue-me"])])
    rec.seed_label_events("infra", 1, [_label_event("queue-me")])
    t = Tracker(rec, ready_label="queue-me")

    issues = t.list_ready(["infra"])

    assert seen["label"] == "queue-me"
    assert len(issues) == 1


# --------------------------------------------------------------------------- #
# Trust gate — labeled_by_trusted is decided from the label-event actor,
# fail-closed.
# --------------------------------------------------------------------------- #
def test_owner_labeled_issue_is_trusted(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=1)])
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent", association="OWNER")])

    assert tracker.list_ready(["infra"])[0].labeled_by_trusted is True


@pytest.mark.parametrize("association", ["MEMBER", "COLLABORATOR"])
def test_collaborator_and_member_are_trusted(gh: FakeGitHub, tracker: Tracker, association: str):
    gh.seed_issues("infra", [_raw_issue(number=1)])
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent", association=association)])

    assert tracker.list_ready(["infra"])[0].labeled_by_trusted is True


@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", ""])
def test_untrusted_association_is_not_trusted(gh: FakeGitHub, tracker: Tracker, association: str):
    gh.seed_issues("infra", [_raw_issue(number=1)])
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent", association=association)])

    assert tracker.list_ready(["infra"])[0].labeled_by_trusted is False


def test_missing_label_event_is_not_trusted(gh: FakeGitHub, tracker: Tracker):
    # The issue carries the ready label, but no event records WHO applied it —
    # fail closed: an unattributable label is never trusted.
    gh.seed_issues("infra", [_raw_issue(number=1)])
    gh.seed_label_events("infra", 1, [])

    assert tracker.list_ready(["infra"])[0].labeled_by_trusted is False


def test_trust_uses_latest_application_of_ready_label(gh: FakeGitHub, tracker: Tracker):
    # If the ready label was removed and re-added, the MOST RECENT application
    # decides trust — a trusted re-label after an untrusted one is trusted.
    gh.seed_issues("infra", [_raw_issue(number=1)])
    gh.seed_label_events(
        "infra",
        1,
        [
            _label_event("ready-for-agent", association="NONE", actor="drive-by"),
            _label_event("ready-for-agent", association="OWNER", actor="viktorbarzin"),
        ],
    )

    assert tracker.list_ready(["infra"])[0].labeled_by_trusted is True


def test_trust_ignores_events_for_other_labels(gh: FakeGitHub, tracker: Tracker):
    # A trusted actor labeling something else must not make the ready label trusted.
    gh.seed_issues("infra", [_raw_issue(number=1)])
    gh.seed_label_events(
        "infra",
        1,
        [
            _label_event("priority:high", association="OWNER"),
            _label_event("ready-for-agent", association="NONE", actor="drive-by"),
        ],
    )

    assert tracker.list_ready(["infra"])[0].labeled_by_trusted is False


def test_custom_trusted_associations_override_default(gh: FakeGitHub):
    # Tighten the trust set to OWNER only: a COLLABORATOR label is no longer trusted.
    t = Tracker(gh, trusted_associations=frozenset({"OWNER"}))
    gh.seed_issues("infra", [_raw_issue(number=1)])
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent", association="COLLABORATOR")])

    assert t.list_ready(["infra"])[0].labeled_by_trusted is False


# --------------------------------------------------------------------------- #
# blocked_by — parsed from the issue body's "Blocked by" references.
# --------------------------------------------------------------------------- #
def test_blocked_by_empty_when_body_has_no_references(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=1, body="just implement the thing")])
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].blocked_by == []


def test_blocked_by_parses_single_reference(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=5, body="Blocked by #3")])
    gh.seed_label_events("infra", 5, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].blocked_by == [3]


def test_blocked_by_parses_multiple_references(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=9, body="Blocked by #3, #4 and #10")])
    gh.seed_label_events("infra", 9, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].blocked_by == [3, 4, 10]


def test_blocked_by_is_case_insensitive_and_dedupes(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=9, body="blocked BY #3 and Blocked by #3, #4")])
    gh.seed_label_events("infra", 9, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].blocked_by == [3, 4]


def test_blocked_by_ignores_plain_issue_mentions(gh: FakeGitHub, tracker: Tracker):
    # A bare "#7" that is not part of a "Blocked by" clause is NOT a blocker.
    gh.seed_issues("infra", [_raw_issue(number=9, body="See #7 for context. Blocked by #3")])
    gh.seed_label_events("infra", 9, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].blocked_by == [3]


def test_blocked_by_tolerates_missing_body(gh: FakeGitHub, tracker: Tracker):
    issue = _raw_issue(number=1)
    issue["body"] = None  # gh returns null for an empty body
    gh.seed_issues("infra", [issue])
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].blocked_by == []


# --------------------------------------------------------------------------- #
# priority — read off a priority label (lower number runs first).
# --------------------------------------------------------------------------- #
def test_priority_defaults_to_zero_without_priority_label(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=1, labels=["ready-for-agent"])])
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].priority == 0


def test_priority_read_from_priority_label(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues("infra", [_raw_issue(number=1, labels=["ready-for-agent", "priority:2"])])
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].priority == 2


def test_priority_lowest_label_wins_when_several(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues(
        "infra", [_raw_issue(number=1, labels=["ready-for-agent", "priority:5", "priority:1"])]
    )
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].priority == 1


def test_priority_ignores_non_numeric_priority_label(gh: FakeGitHub, tracker: Tracker):
    gh.seed_issues(
        "infra", [_raw_issue(number=1, labels=["ready-for-agent", "priority:high"])]
    )
    gh.seed_label_events("infra", 1, [_label_event("ready-for-agent")])

    assert tracker.list_ready(["infra"])[0].priority == 0


# --------------------------------------------------------------------------- #
# Mutations delegate to the injected client.
# --------------------------------------------------------------------------- #
def test_add_label_delegates(gh: FakeGitHub, tracker: Tracker):
    tracker.add_label("infra", 7, "agent-in-progress")
    assert gh.labels_added == [("infra", 7, "agent-in-progress")]


def test_remove_label_delegates(gh: FakeGitHub, tracker: Tracker):
    tracker.remove_label("infra", 7, "agent-in-progress")
    assert gh.labels_removed == [("infra", 7, "agent-in-progress")]


def test_comment_delegates(gh: FakeGitHub, tracker: Tracker):
    tracker.comment("infra", 7, "phase: tests-red done")
    assert gh.comments == [("infra", 7, "phase: tests-red done")]


def test_close_delegates(gh: FakeGitHub, tracker: Tracker):
    tracker.close("infra", 7)
    assert gh.closed == [("infra", 7)]


# --------------------------------------------------------------------------- #
# The concrete gh-CLI-backed client builds no-shell argv and parses JSON; we
# inject a fake runner so no real `gh` is ever spawned.
# --------------------------------------------------------------------------- #
from app.afk.tracker import GhCliClient  # noqa: E402


class _FakeRunner:
    """Stand-in for the subprocess runner GhCliClient shells out through.

    Records every argv and returns staged stdout per command, so we can pin the
    exact `gh` invocations without spawning a process.
    """

    def __init__(self, responses: dict[tuple[str, ...], str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses = responses or {}

    def __call__(self, argv: list[str]) -> str:
        key = tuple(argv)
        self.calls.append(key)
        return self._responses.get(key, "")


def test_gh_cli_list_issues_builds_no_shell_argv_and_parses_json():
    argv = (
        "gh", "issue", "list", "--repo", "owner/infra",
        "--label", "ready-for-agent", "--state", "open",
        "--json", "number,labels,body", "--limit", "100",
    )
    runner = _FakeRunner({argv: '[{"number": 4, "labels": [{"name": "ready-for-agent"}], "body": "x"}]'})
    client = GhCliClient(repo_owner="owner", run=runner)

    issues = client.list_issues("infra", "ready-for-agent")

    assert runner.calls == [argv]
    assert issues == [{"number": 4, "labels": [{"name": "ready-for-agent"}], "body": "x"}]


def test_gh_cli_list_issues_empty_output_is_empty_list():
    runner = _FakeRunner()  # returns "" for everything
    client = GhCliClient(repo_owner="owner", run=runner)
    assert client.list_issues("infra", "ready-for-agent") == []


def test_gh_cli_label_events_filters_labeled_events():
    timeline = (
        '[{"event": "commented"},'
        ' {"event": "labeled", "label": {"name": "ready-for-agent"},'
        '  "actor": {"login": "viktorbarzin"}, "author_association": "OWNER"}]'
    )
    argv = (
        "gh", "api",
        "repos/owner/infra/issues/4/timeline",
        "--paginate",
        "-H", "Accept: application/vnd.github+json",
    )
    runner = _FakeRunner({argv: timeline})
    client = GhCliClient(repo_owner="owner", run=runner)

    events = client.label_events("infra", 4)

    assert runner.calls == [argv]
    assert [e["event"] for e in events] == ["labeled"]
    assert events[0]["label"]["name"] == "ready-for-agent"


def test_gh_cli_add_label_builds_argv():
    runner = _FakeRunner()
    client = GhCliClient(repo_owner="owner", run=runner)
    client.add_label("infra", 4, "agent-in-progress")
    assert runner.calls == [
        ("gh", "issue", "edit", "4", "--repo", "owner/infra", "--add-label", "agent-in-progress")
    ]


def test_gh_cli_remove_label_builds_argv():
    runner = _FakeRunner()
    client = GhCliClient(repo_owner="owner", run=runner)
    client.remove_label("infra", 4, "agent-in-progress")
    assert runner.calls == [
        ("gh", "issue", "edit", "4", "--repo", "owner/infra", "--remove-label", "agent-in-progress")
    ]


def test_gh_cli_comment_builds_argv():
    runner = _FakeRunner()
    client = GhCliClient(repo_owner="owner", run=runner)
    client.comment("infra", 4, "phase update")
    assert runner.calls == [
        ("gh", "issue", "comment", "4", "--repo", "owner/infra", "--body", "phase update")
    ]


def test_gh_cli_close_builds_argv():
    runner = _FakeRunner()
    client = GhCliClient(repo_owner="owner", run=runner)
    client.close("infra", 4)
    assert runner.calls == [
        ("gh", "issue", "close", "4", "--repo", "owner/infra")
    ]


def test_gh_cli_end_to_end_through_tracker():
    # Wire the gh-CLI client (fake runner) behind a real Tracker and confirm a
    # full read produces a correctly-decoded, trusted, blocked Issue.
    list_argv = (
        "gh", "issue", "list", "--repo", "owner/infra",
        "--label", "ready-for-agent", "--state", "open",
        "--json", "number,labels,body", "--limit", "100",
    )
    timeline_argv = (
        "gh", "api",
        "repos/owner/infra/issues/12/timeline",
        "--paginate",
        "-H", "Accept: application/vnd.github+json",
    )
    runner = _FakeRunner({
        list_argv: (
            '[{"number": 12,'
            '  "labels": [{"name": "ready-for-agent"}, {"name": "priority:3"}],'
            '  "body": "Blocked by #11"}]'
        ),
        timeline_argv: (
            '[{"event": "labeled", "label": {"name": "ready-for-agent"},'
            '  "actor": {"login": "viktorbarzin"}, "author_association": "OWNER"}]'
        ),
    })
    tracker = Tracker(GhCliClient(repo_owner="owner", run=runner))

    issue = tracker.list_ready(["infra"])[0]

    assert issue.number == 12
    assert issue.repo == "infra"
    assert issue.blocked_by == [11]
    assert issue.priority == 3
    assert issue.labeled_by_trusted is True
