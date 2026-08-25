"""Tests for the fixer's CI sources and its tick loop.

Two things are pinned here because getting either wrong is silent in production:
an unrecognised pipeline status must never read as SUCCESS (it would close an
issue that never landed), and a tick must never leave an in-flight run with
nothing driving it.
"""
import pytest

from app.afk.ci_watcher import StageResult
from app.afk.types import Action, CIStatus, Config
from app.fixer import ci, tick
from app.fixer.runstate import RunRecord, latest_record, render_comment


# --------------------------------------------------------------------------- #
# Woodpecker — the decisive stage for infra.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,expected", [
    ("success", StageResult.SUCCESS),
    ("skipped", StageResult.SUCCESS),
    ("failure", StageResult.FAILURE),
    ("error", StageResult.FAILURE),
    ("killed", StageResult.FAILURE),
    ("declined", StageResult.FAILURE),
    ("running", StageResult.PENDING),
    ("pending", StageResult.PENDING),
    ("blocked", StageResult.PENDING),
])
def test_pipeline_status_maps_to_a_stage_result(status, expected, monkeypatch):
    monkeypatch.setattr(ci, "_get_json",
                        lambda url, headers: [{"commit": "abc1234def", "status": status}])
    client = ci.WoodpeckerPipelines("http://wp", "tok", "1")
    assert client.deploy_conclusion("infra", "abc1234def") is expected


def test_an_unknown_pipeline_status_is_pending_never_success(monkeypatch):
    """An unrecognised status must not be able to close an issue."""
    monkeypatch.setattr(ci, "_get_json",
                        lambda url, headers: [{"commit": "abc1234", "status": "brand-new"}])
    client = ci.WoodpeckerPipelines("http://wp", "tok", "1")
    assert client.deploy_conclusion("infra", "abc1234") is StageResult.PENDING


def test_no_pipeline_for_the_commit_reads_as_none(monkeypatch):
    monkeypatch.setattr(ci, "_get_json",
                        lambda url, headers: [{"commit": "999999", "status": "success"}])
    client = ci.WoodpeckerPipelines("http://wp", "tok", "1")
    assert client.deploy_conclusion("infra", "abc1234") is StageResult.NONE


def test_an_unreachable_woodpecker_reads_as_none(monkeypatch):
    monkeypatch.setattr(ci, "_get_json", lambda url, headers: None)
    client = ci.WoodpeckerPipelines("http://wp", "tok", "1")
    assert client.deploy_conclusion("infra", "abc1234") is StageResult.NONE


# --------------------------------------------------------------------------- #
# The unobserved build stage.
# --------------------------------------------------------------------------- #
def test_the_unobserved_stage_reports_success_and_says_so_once(caplog):
    stage = ci.UnobservedStage("GitHub Actions build")
    with caplog.at_level("INFO"):
        assert stage.run_conclusion("infra", "abc") is StageResult.SUCCESS
        assert stage.run_conclusion("infra", "def") is StageResult.SUCCESS
    assert sum("unobserved" in r.message for r in caplog.records) == 1


def test_the_watcher_uses_github_checks_when_a_token_is_present():
    watcher = ci.build_ci_watcher({"FIXER_GITHUB_TOKEN": "ghp_x"})
    assert isinstance(watcher._github, ci.GitHubChecks)


def test_the_watcher_falls_back_to_the_unobserved_stage():
    watcher = ci.build_ci_watcher({})
    assert isinstance(watcher._github, ci.UnobservedStage)


def test_a_green_deploy_is_terminal_without_a_rollout_client(monkeypatch):
    monkeypatch.setattr(ci, "_get_json",
                        lambda url, headers: [{"commit": "abc1234", "status": "success"}])
    watcher = ci.build_ci_watcher({})
    assert watcher.status("infra", "abc1234") is CIStatus.GREEN


# --------------------------------------------------------------------------- #
# The tick loop.
# --------------------------------------------------------------------------- #
class StubForgejo:
    def __init__(self):
        self.issues: dict[str, list[dict]] = {}
        self.comments: dict[int, list[dict]] = {}
        self.label_ops: list[tuple[str, str, int, str]] = []
        self.posted: list[tuple[int, str]] = []

    def list_issues(self, repo, label):
        return list(self.issues.get(label, []))

    def list_comments(self, repo, number):
        return list(self.comments.get(number, []))

    def get_issue(self, repo, number):
        return {"title": "t", "number": number}

    def comment(self, repo, number, body):
        self.posted.append((number, body))
        self.comments.setdefault(number, []).append({"body": body})

    def add_label(self, repo, number, label):
        self.label_ops.append(("add", repo, number, label))

    def remove_label(self, repo, number, label):
        self.label_ops.append(("remove", repo, number, label))


class StubTracker:
    def __init__(self, forgejo):
        self._f = forgejo

    def _to_issue(self, repo, raw):
        from app.afk.types import Issue
        return Issue(
            number=int(raw["number"]), repo=repo,
            labels=[lbl["name"] for lbl in raw.get("labels", [])],
            blocked_by=[], labeled_by_trusted=True, priority=1,
        )

    def add_label(self, repo, issue, label):
        self._f.add_label(repo, issue, label)

    def remove_label(self, repo, issue, label):
        self._f.remove_label(repo, issue, label)

    def comment(self, repo, issue, body):
        self._f.comment(repo, issue, body)

    def close(self, repo, issue):
        self._f.label_ops.append(("close", repo, issue, ""))


class StubDispatcher:
    def __init__(self, states: dict[str, str]):
        self.states = states
        self.dispatched: list[tuple[str, int]] = []
        self.next_id = "job-new"

    def dispatch(self, repo, issue, prompt):
        self.dispatched.append((repo, issue))
        return self.next_id

    def snapshot(self):
        return {"threads": [{"id": jid, "latestTurn": {"state": st}}
                            for jid, st in self.states.items()]}

    def track(self, job_id):
        self.states.setdefault(job_id, "running")


class StubNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, kind, issue, thread_id, detail):
        self.sent.append(kind)


def make_cfg():
    from app.fixer.config import FixerConfig
    return FixerConfig(token="t", webhook_secret="s")


def loop_config():
    """As the fixer runs it: no fix-forward ceiling (decision 14)."""
    from app.fixer.config import UNBOUNDED
    return Config(allowlist=["infra"], kill_switch=False,
                  fix_forward_max_attempts=UNBOUNDED,
                  fix_forward_max_seconds=UNBOUNDED)


def test_an_in_progress_issue_with_no_run_state_is_handed_over(monkeypatch):
    """An orphaned lock must be released, not left parked forever."""
    f = StubForgejo()
    f.issues["agent-in-progress"] = [{"number": 9, "labels": [{"name": "broken"}]}]
    lines = tick.watch(f, StubTracker(f), StubDispatcher({}), StubNotifier(),
                       loop_config(), make_cfg())
    assert lines == ["infra#9: orphaned, escalated"]
    assert ("remove", "infra", 9, "agent-in-progress") in f.label_ops
    assert ("add", "infra", 9, "needs-human") in f.label_ops


def test_a_running_job_is_left_alone(monkeypatch):
    f = StubForgejo()
    f.issues["agent-in-progress"] = [{"number": 9, "labels": [{"name": "broken"}]}]
    f.comments[9] = [{"body": render_comment("working", RunRecord("job-1", 1.0))}]
    monkeypatch.setattr(tick, "_ci_watcher", lambda: ci.build_ci_watcher({}))
    lines = tick.watch(f, StubTracker(f), StubDispatcher({"job-1": "running"}),
                       StubNotifier(), loop_config(), make_cfg())
    assert lines == ["infra#9: wait"]
    # A WAIT refreshes the progress checklist and nothing else: no findings,
    # no escalation, no new run-state footer.
    assert [b for _, b in f.posted if "fixer-state:" in b] == []


def test_a_red_pipeline_dispatches_a_corrective_turn_and_records_it(monkeypatch):
    f = StubForgejo()
    f.issues["agent-in-progress"] = [{"number": 9, "labels": [{"name": "broken"}]}]
    f.comments[9] = [{"body": render_comment(
        "pushed abc1234def", RunRecord("job-1", 1.0, commit="abc1234def"))}]

    class RedCI:
        def status(self, repo, commit):
            return CIStatus.RED

    monkeypatch.setattr(tick, "_ci_watcher", lambda: RedCI())
    dispatcher = StubDispatcher({"job-1": "completed"})
    lines = tick.watch(f, StubTracker(f), dispatcher, StubNotifier(),
                       loop_config(), make_cfg())
    assert lines == ["infra#9: fix_forward"]
    assert dispatcher.dispatched == [("infra", 9)]

    # The new state must be readable by the NEXT tick, or the loop forgets.
    record = latest_record([c["body"] for c in f.comments[9]])
    assert record is not None
    assert record.job_id == "job-new"
    assert record.fix_forward_attempts == 1
    assert record.commit == "abc1234def"


def test_a_green_pipeline_closes_the_issue(monkeypatch):
    f = StubForgejo()
    f.issues["agent-in-progress"] = [{"number": 9, "labels": [{"name": "broken"}]}]
    f.comments[9] = [{"body": render_comment(
        "pushed abc1234def", RunRecord("job-1", 1.0, commit="abc1234def"))}]

    class GreenCI:
        def status(self, repo, commit):
            return CIStatus.GREEN

    monkeypatch.setattr(tick, "_ci_watcher", lambda: GreenCI())
    notifier = StubNotifier()
    lines = tick.watch(f, StubTracker(f), StubDispatcher({"job-1": "completed"}),
                       notifier, loop_config(), make_cfg())
    assert lines == ["infra#9: close_success"]
    assert ("close", "infra", 9, "") in f.label_ops
    assert notifier.sent == ["done"]


def test_the_commit_is_read_from_the_runs_prose_not_only_the_footer(monkeypatch):
    """A run states its sha in a comment; that is what the watcher follows."""
    f = StubForgejo()
    f.issues["agent-in-progress"] = [{"number": 9, "labels": [{"name": "broken"}]}]
    f.comments[9] = [
        {"body": render_comment("investigating", RunRecord("job-1", 1.0))},
        {"body": "Resolved: increased the memory limit. Commit `9f8e7d6c5b4a`."},
    ]
    seen: list[str] = []

    class RecordingCI:
        def status(self, repo, commit):
            seen.append(commit)
            return CIStatus.PENDING

    monkeypatch.setattr(tick, "_ci_watcher", lambda: RecordingCI())
    tick.watch(f, StubTracker(f), StubDispatcher({"job-1": "completed"}),
               StubNotifier(), loop_config(), make_cfg())
    assert seen == ["9f8e7d6c5b4a"]


def test_drain_comments_the_run_state_on_what_it_starts():
    f = StubForgejo()

    class OnePoller:
        def __init__(self, *a, **k):
            pass

        def run_once(self, config):
            from app.afk.poller import Dispatched, PollResult
            from app.afk.types import Issue
            issue = Issue(number=12, repo="infra", labels=["broken"], blocked_by=[],
                          labeled_by_trusted=True, priority=1)
            return PollResult(dispatched=[
                Dispatched(issue=issue, thread_id="job-x", reason="ready")
            ])

    import app.fixer.tick as tick_mod
    original = tick_mod.Poller
    tick_mod.Poller = OnePoller
    try:
        started = tick.drain(StubTracker(f), StubDispatcher({}), f,
                             loop_config(), make_cfg())
    finally:
        tick_mod.Poller = original
    assert started == 1
    record = latest_record([b for _, b in f.posted])
    assert record is not None and record.job_id == "job-x"
