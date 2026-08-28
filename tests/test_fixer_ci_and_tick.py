"""Tests for the fixer's CI sources and its tick loop.

Two things are pinned here because getting either wrong is silent in production:
an unrecognised pipeline status must never read as SUCCESS (it would close an
issue that never landed), and a tick must never leave an in-flight run with
nothing driving it.
"""
import logging
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


def test_the_commit_comes_from_the_runs_explicit_marker(monkeypatch):
    """A run DECLARES what it pushed; that declaration is what the watcher
    follows. Prose is not read, because image tags and job ids are hex too."""
    f = StubForgejo()
    f.issues["agent-in-progress"] = [{"number": 9, "labels": [{"name": "broken"}]}]
    f.comments[9] = [
        {"body": render_comment("investigating", RunRecord("job-1", 1.0))},
        {"body": "Resolved: increased the memory limit.\n\nPushed-Commit: 9f8e7d6c5b4a"},
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


# --------------------------------------------------------------------------- #
# The doorbell — this ntfy is deny-all, so an unauthenticated publish is a 403.
# --------------------------------------------------------------------------- #
def test_the_doorbell_authenticates_when_a_token_is_configured():
    from app.afk.notifier import Notification
    from app.fixer import ntfy as ntfy_mod
    seen = {}

    def poster(url, body, headers):
        seen.update({"url": url, "headers": headers, "body": body})
        return 200

    send = ntfy_mod.make_sender("https://ntfy.example", "fixer", "tk_secret", poster)
    send(Notification(kind="done", issue_ref="infra#7", title="[DONE] infra#7 landed",
                      body="all good", link="https://forgejo/x", priority="low",
                      tags=["afk", "done"]))
    assert seen["url"] == "https://ntfy.example/fixer"
    assert seen["headers"]["Authorization"] == "Bearer tk_secret"
    assert seen["headers"]["Priority"] == "2"
    assert seen["headers"]["Click"] == "https://forgejo/x"


def test_the_doorbell_omits_the_header_when_no_token_is_set():
    from app.afk.notifier import Notification
    from app.fixer import ntfy as ntfy_mod
    seen = {}

    def poster(url, body, headers):
        seen.update(headers)
        return 200

    ntfy_mod.make_sender("https://ntfy.example", "fixer", "", poster)(
        Notification(kind="frozen", issue_ref="infra#7", title="t", body="b",
                     link=None, priority="high", tags=[]))
    assert "Authorization" not in seen
    assert seen["Priority"] == "5"


def test_a_rejected_publish_raises_rather_than_failing_quietly():
    from app.afk.notifier import Notification
    from app.fixer import ntfy as ntfy_mod
    send = ntfy_mod.make_sender("https://ntfy.example", "fixer", "", lambda u, b, h: 403)
    with pytest.raises(RuntimeError, match="403"):
        send(Notification(kind="done", issue_ref="infra#7", title="t", body="b",
                          link=None, priority="low", tags=[]))


def test_the_fixer_checklist_describes_a_repair_not_a_tdd_build():
    """The AFK wording ("Failing test written (TDD red)") misdescribes an
    incident fix on an issue a human reads."""
    from app.afk.phase_checklist import FIXER_LABELS, render
    from app.afk.types import Phase
    body = render(Phase.GREEN, {"repo": "infra", "issue": 30, "thread_id": "j"}, FIXER_LABELS)
    assert "Symptom verified" in body and "Cause found and repaired" in body
    assert "TDD" not in body


def test_the_afk_wording_is_unchanged_when_no_labels_are_passed():
    from app.afk.phase_checklist import render
    from app.afk.types import Phase
    body = render(Phase.GREEN, {"repo": "x", "issue": 1, "thread_id": "j"})
    assert "TDD red" in body


# --------------------------------------------------------------------------- #
# A tick during a deployment roll: /execute is briefly unreachable.
# --------------------------------------------------------------------------- #
def test_a_failing_drain_does_not_skip_driving_in_flight_runs(monkeypatch):
    """Observed live: a tick died on the drain while the pod was rolling, so
    every in-flight run went undriven for that interval."""
    watched = []
    monkeypatch.setenv("AFK_KILL_SWITCH", "false")
    monkeypatch.setenv("AFK_ALLOWLIST", "infra")
    monkeypatch.setenv("FIXER_FORGEJO_TOKEN", "t")
    monkeypatch.setattr(tick, "build", lambda *a, **k: (None, None, None, None))

    def boom(*a, **k):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(tick, "drain", boom)
    monkeypatch.setattr(tick, "watch", lambda *a, **k: watched.append(1) or ["infra#9: wait"])
    assert tick.main([]) == 0
    assert watched == [1]


def test_a_tick_fails_only_when_both_phases_fail(monkeypatch):
    monkeypatch.setenv("AFK_KILL_SWITCH", "false")
    monkeypatch.setenv("AFK_ALLOWLIST", "infra")
    monkeypatch.setenv("FIXER_FORGEJO_TOKEN", "t")
    monkeypatch.setattr(tick, "build", lambda *a, **k: (None, None, None, None))

    def boom(*a, **k):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(tick, "drain", boom)
    monkeypatch.setattr(tick, "watch", boom)
    assert tick.main([]) == 1


def test_the_kill_switch_makes_a_tick_do_nothing(monkeypatch):
    monkeypatch.setenv("AFK_KILL_SWITCH", "true")
    called = []
    monkeypatch.setattr(tick, "build", lambda *a, **k: called.append(1))
    assert tick.main([]) == 0
    assert called == []


# --------------------------------------------------------------------------- #
# The drill affordance for the otherwise-unreachable fix-forward path.
# --------------------------------------------------------------------------- #
def test_force_red_is_off_unless_armed(monkeypatch, tmp_path):
    monkeypatch.delenv("FIXER_CI_FORCE_RED_ONCE", raising=False)
    monkeypatch.setattr(ci, "FORCE_RED_STATE", str(tmp_path / ".forced"))
    assert ci._force_red_once("abc1234") is False


def test_force_red_fires_once_then_never_again(monkeypatch, tmp_path):
    """Once per commit, and the seen-set is on disk: each tick is a fresh pod, so
    in-memory state would fire every tick and never leave fix-forward."""
    monkeypatch.setenv("FIXER_CI_FORCE_RED_ONCE", "1")
    monkeypatch.setattr(ci, "FORCE_RED_STATE", str(tmp_path / ".forced"))
    assert ci._force_red_once("abc1234") is True
    assert ci._force_red_once("abc1234") is False
    assert ci._force_red_once("abc1234") is False


def test_force_red_says_so_when_it_cannot_keep_its_marker(monkeypatch, tmp_path, caplog):
    """Armed but unable to persist -> still False, but no longer in silence.

    The other tests here point FORCE_RED_STATE at a writable tmp_path, so none
    of them saw the case that actually happened in the cluster: the tick pod did
    not mount the volume the real path lives on, the container runs as uid 1000,
    and the write raised PermissionError. It was caught and read as "not armed",
    so the affordance looked active for three days of ticks without ever firing.
    """
    unwritable = tmp_path / "ro"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    monkeypatch.setenv("FIXER_CI_FORCE_RED_ONCE", "1")
    monkeypatch.setattr(ci, "FORCE_RED_STATE", str(unwritable / "sub" / ".forced"))

    with caplog.at_level(logging.WARNING):
        assert ci._force_red_once("abc1234") is False

    assert "NOT forcing red" in caplog.text
    assert "FIXER_CI_FORCE_RED_ONCE" in caplog.text


def test_force_red_is_per_commit(monkeypatch, tmp_path):
    monkeypatch.setenv("FIXER_CI_FORCE_RED_ONCE", "1")
    monkeypatch.setattr(ci, "FORCE_RED_STATE", str(tmp_path / ".forced"))
    assert ci._force_red_once("aaa1111") is True
    assert ci._force_red_once("bbb2222") is True
    assert ci._force_red_once("aaa1111") is False


def test_an_armed_verdict_reports_failure_without_asking_woodpecker(monkeypatch, tmp_path):
    monkeypatch.setenv("FIXER_CI_FORCE_RED_ONCE", "1")
    monkeypatch.setattr(ci, "FORCE_RED_STATE", str(tmp_path / ".forced"))
    called = []
    monkeypatch.setattr(ci, "_get_json", lambda u, h: called.append(u))
    client = ci.WoodpeckerPipelines("http://wp", "tok", "1")
    assert client.deploy_conclusion("infra", "abc1234") is StageResult.FAILURE
    assert called == []          # no request made
    monkeypatch.setattr(ci, "_get_json",
                        lambda u, h: [{"commit": "abc1234", "status": "success"}])
    assert client.deploy_conclusion("infra", "abc1234") is StageResult.SUCCESS


# --------------------------------------------------------------------------- #
# The defer ceiling must NOT follow the fix-forward budgets into unbounded.
# --------------------------------------------------------------------------- #
def test_the_defer_ceiling_stays_bounded_even_though_fix_forward_is_not():
    """The no-caps decision is about not truncating the agent's work: the fixer
    dispatches with no budget and no timeout on purpose. This ceiling bounds only
    how long the WATCHER waits before acting on a verdict it already has, and
    because there is no job timeout to fall back on, making it unbounded too
    would let a turn that wedged after pushing hold the in-progress lock — and
    every other ready issue behind it — indefinitely."""
    from app.fixer import config as fixer_config

    cfg = fixer_config.loop_config(
        {"AFK_ALLOWLIST": "infra", "AFK_KILL_SWITCH": "false"}
    )
    assert cfg.fix_forward_max_attempts == fixer_config.UNBOUNDED
    assert cfg.fix_forward_max_seconds == fixer_config.UNBOUNDED
    assert cfg.close_defer_max_seconds == fixer_config.DEFAULT_CLOSE_DEFER_SECONDS
    assert cfg.close_defer_max_seconds < fixer_config.UNBOUNDED


@pytest.mark.parametrize(
    "raw,expected",
    [("600", 600), ("", None), ("not-a-number", None), ("-5", None), ("0", None)],
)
def test_the_defer_ceiling_is_tunable_and_refuses_nonsense(raw, expected):
    """A ceiling of zero or below would close every green run instantly, which is
    the bug this exists to prevent, so it falls back rather than obeying."""
    from app.fixer import config as fixer_config

    cfg = fixer_config.loop_config({
        "AFK_ALLOWLIST": "infra", "AFK_KILL_SWITCH": "false",
        "FIXER_CLOSE_DEFER_SECONDS": raw,
    })
    want = fixer_config.DEFAULT_CLOSE_DEFER_SECONDS if expected is None else expected
    assert cfg.close_defer_max_seconds == want


# --------------------------------------------------------------------------- #
# A close that races a live turn leaves a trail.
# --------------------------------------------------------------------------- #
def test_a_green_run_defers_its_close_while_the_turn_is_running(monkeypatch):
    """The normal protection: no close, no comment churn, run stays in flight.

    ``started_at`` has to be NOW, not the 1.0 the other tests here use: 1.0 is
    epoch, so such a run is decades old and already past the defer ceiling.
    """
    import time

    f = StubForgejo()
    f.issues["agent-in-progress"] = [{"number": 9, "labels": [{"name": "broken"}]}]
    f.comments[9] = [{"body": render_comment(
        "pushed abc1234def",
        RunRecord("job-1", time.time(), commit="abc1234def"))}]

    class GreenCI:
        def status(self, repo, commit):
            return CIStatus.GREEN

    monkeypatch.setattr(tick, "_ci_watcher", lambda: GreenCI())
    lines = tick.watch(f, StubTracker(f), StubDispatcher({"job-1": "running"}),
                       StubNotifier(), loop_config(), make_cfg())
    assert lines == ["infra#9: wait"]
    assert ("close", "infra", 9, "") not in f.label_ops


def test_a_close_past_the_defer_ceiling_records_that_it_raced_a_live_turn(monkeypatch):
    """Past the ceiling the close proceeds, because the commit landed and CI is
    green. It must say so: the last footer still names the job, and without a
    note there is nothing to connect a later stray push to this run."""
    import time

    f = StubForgejo()
    f.issues["agent-in-progress"] = [{"number": 9, "labels": [{"name": "broken"}]}]
    # Started long enough ago to be past the ceiling.
    started = time.time() - 999_999
    f.comments[9] = [{"body": render_comment(
        "pushed abc1234def", RunRecord("job-1", started, commit="abc1234def"))}]

    class GreenCI:
        def status(self, repo, commit):
            return CIStatus.GREEN

    monkeypatch.setattr(tick, "_ci_watcher", lambda: GreenCI())
    lines = tick.watch(f, StubTracker(f), StubDispatcher({"job-1": "running"}),
                       StubNotifier(), loop_config(), make_cfg())

    assert lines == ["infra#9: close_success"]
    assert ("close", "infra", 9, "") in f.label_ops
    bodies = "\n".join(c["body"] for c in f.comments[9])
    assert "job-1" in bodies
    assert "still running" in bodies
