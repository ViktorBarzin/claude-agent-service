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


def _paged_woodpecker(pages: dict[int, list[dict]], asked: list[int]):
    """A fake ``_get_json`` serving Woodpecker's newest-first, 50-per-page list.

    The server caps ``perPage`` at 50 whatever the client asks for, so "the
    pipeline is on page 4" is an ordinary state for a commit a few days old.
    """
    import urllib.parse

    def get(url, headers):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        page = int(query.get("page", ["1"])[0])
        asked.append(page)
        return pages.get(page, [])
    return get


def _filler(n: int, start: int) -> list[dict]:
    return [{"commit": f"{start + i:040x}", "status": "success"} for i in range(n)]


def test_a_commit_whose_pipeline_is_past_the_first_page_is_still_found(monkeypatch):
    """infra#95, 2026-09-23: the run pushed f69e3dcc on 09-13, its pipeline went
    green a minute later, and the tick first looked five days after that. By
    then the pipeline sat on page 4, only page 1 was read, and "too old to see"
    came back as "no pipeline yet" — a WAIT that could never end, holding the
    per-repo lock over every queued issue for ten days."""
    asked: list[int] = []
    pages = {1: _filler(50, 0), 2: _filler(50, 100), 3: _filler(50, 200),
             4: _filler(10, 300) + [{"commit": "f69e3dcc" + "0" * 32,
                                     "status": "success"}]}
    monkeypatch.setattr(ci, "_get_json", _paged_woodpecker(pages, asked))
    client = ci.WoodpeckerPipelines("http://wp", "tok", "1")
    assert client.deploy_conclusion("infra", "f69e3dcc" + "0" * 32) is StageResult.SUCCESS
    assert asked == [1, 2, 3, 4]


def test_the_search_stops_at_the_end_of_the_history(monkeypatch):
    """A short page is the oldest pipeline there is: nothing past it to read."""
    asked: list[int] = []
    pages = {1: _filler(50, 0), 2: _filler(7, 100)}
    monkeypatch.setattr(ci, "_get_json", _paged_woodpecker(pages, asked))
    client = ci.WoodpeckerPipelines("http://wp", "tok", "1")
    assert client.deploy_conclusion("infra", "abc1234") is StageResult.NONE
    assert asked == [1, 2]


def test_the_search_is_bounded_and_says_when_it_gives_up(monkeypatch, caplog):
    """A commit that never got a pipeline must not cost the whole history on
    every tick. Giving up is logged, because the result is the same NONE a
    just-pushed commit returns, and the two must be told apart in the logs."""
    asked: list[int] = []
    pages = {p: _filler(50, p * 100) for p in range(1, 1000)}
    monkeypatch.setattr(ci, "_get_json", _paged_woodpecker(pages, asked))
    client = ci.WoodpeckerPipelines("http://wp", "tok", "1")
    with caplog.at_level(logging.INFO, logger="app.fixer.ci"):
        assert client.deploy_conclusion("infra", "abc1234") is StageResult.NONE
    assert asked == list(range(1, ci.MAX_PIPELINE_PAGES + 1))
    assert "abc1234" in caplog.text
    assert str(ci.MAX_PIPELINE_PAGES * ci.PIPELINES_PER_PAGE) in caplog.text


def test_the_newest_pipeline_for_a_commit_wins(monkeypatch):
    """A manual re-run on the same commit supersedes the push pipeline under it,
    which is how a killed apply gets finished by hand."""
    asked: list[int] = []
    pages = {1: [{"commit": "abc1234def", "status": "success"},
                 {"commit": "abc1234def", "status": "killed"}]}
    monkeypatch.setattr(ci, "_get_json", _paged_woodpecker(pages, asked))
    client = ci.WoodpeckerPipelines("http://wp", "tok", "1")
    assert client.deploy_conclusion("infra", "abc1234def") is StageResult.SUCCESS
    assert asked == [1]


def test_an_unreachable_page_mid_search_reads_as_none(monkeypatch):
    """We failed to ask, which says nothing about the commit: wait, never guess."""
    def get(url, headers):
        return _filler(50, 0) if "page=1&" in url else None
    monkeypatch.setattr(ci, "_get_json", get)
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


# --------------------------------------------------------------------------- #
# The footer is the run's only followable record, so its write is retried.
#
# drain dispatches, stamps the in-progress label, then writes the footer comment.
# The order is deliberate — a failed dispatch must leave the issue purely ready
# rather than wedged behind a phantom lock — but it leaves a window where the
# label exists and the footer does not. A tick in that window takes the
# no-footer branch: it releases the lock and hands the issue to a human while the
# job is still running and can still push.
#
# If the pod dies in the window the job dies with it (jobs live in-process), so
# escalating is then correct. The case that matters is narrower: the comment POST
# failing while the job survives. One retry covers it.
# --------------------------------------------------------------------------- #
def test_the_footer_write_is_retried_before_the_run_becomes_unfollowable(monkeypatch):
    attempts = {"n": 0}

    class FlakyForgejo(StubForgejo):
        def comment(self, repo, number, body):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("forgejo blipped")
            return super().comment(repo, number, body)

    f = FlakyForgejo()

    class OneReady:
        def list_ready(self, repos):
            from app.afk.types import Issue
            return [Issue(number=11, repo="infra", labels=["broken"],
                          blocked_by=[], labeled_by_trusted=True, priority=1)]

        def list_in_progress(self, repos, label):
            return []

        def add_label(self, repo, issue, label):
            f.label_ops.append(("add", repo, issue, label))

    started = tick.drain(OneReady(), StubDispatcher({}), f, loop_config(), make_cfg())

    assert started == 1
    assert attempts["n"] == 2, "the footer write was not retried"
    record = latest_record([c["body"] for c in f.comments.get(11, [])])
    assert record is not None, "the run ended up with no followable state"


def test_a_footer_that_cannot_be_written_is_logged_loudly(monkeypatch, caplog):
    """When every attempt fails the run is genuinely unfollowable. Nothing can
    recover it from here, so the one useful thing is to say which job it was."""
    class DeadForgejo(StubForgejo):
        def comment(self, repo, number, body):
            raise RuntimeError("forgejo down")

    f = DeadForgejo()

    class OneReady:
        def list_ready(self, repos):
            from app.afk.types import Issue
            return [Issue(number=11, repo="infra", labels=["broken"],
                          blocked_by=[], labeled_by_trusted=True, priority=1)]

        def list_in_progress(self, repos, label):
            return []

        def add_label(self, repo, issue, label):
            pass

    with caplog.at_level(logging.ERROR):
        tick.drain(OneReady(), StubDispatcher({}), f, loop_config(), make_cfg())

    assert "job-new" in caplog.text
    assert "infra#11" in caplog.text


# --------------------------------------------------------------------------- #
# infra#95, replayed: a pushed run whose job vanished, watched late.
#
# The run pushed f69e3dcc on 2026-09-13 and declared it; the pod was replaced a
# few seconds later, so the job vanished; the pipeline went green a minute after
# that. The tick was suspended until 09-18 and then found the run with a commit
# and no verdict, and waited on it until 09-23. These drive the real tick and the
# real CI adapter, faking only the Woodpecker and Forgejo payloads.
# --------------------------------------------------------------------------- #
_SHA_95 = "f69e3dcc820eb1ca36698ca8b71b169a305911a7"


def _iso(epoch: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _issue_95(declared_at: float, started_at: float) -> StubForgejo:
    f = StubForgejo()
    f.issues["agent-in-progress"] = [{"number": 95, "labels": [{"name": "broken"}]}]
    f.comments[95] = [
        {"body": render_comment("Picked this up.\n\n_Fixer run `68a281700261`._",
                                RunRecord("68a281700261", started_at)),
         "created_at": _iso(started_at)},
        {"body": f"**Resolved** — switched the probe.\n\nPushed-Commit: {_SHA_95}",
         "created_at": _iso(declared_at)},
    ]
    return f


def _woodpecker_pages(monkeypatch, pages: dict[int, list[dict]]) -> None:
    monkeypatch.delenv(ci.ENV_FORCE_RED_ONCE, raising=False)
    monkeypatch.setattr(ci, "_get_json", _paged_woodpecker(pages, []))
    monkeypatch.setattr(tick, "_ci_watcher", lambda: ci.build_ci_watcher({}))


def test_infra_95_closes_on_its_green_pipeline_four_pages_back(monkeypatch):
    import time
    ten_days_ago = time.time() - 10 * 86400
    f = _issue_95(declared_at=ten_days_ago + 870, started_at=ten_days_ago)
    _woodpecker_pages(monkeypatch, {
        1: _filler(50, 0), 2: _filler(50, 100), 3: _filler(50, 200),
        4: [{"commit": _SHA_95, "status": "success"}] + _filler(49, 300),
    })
    notifier = StubNotifier()
    lines = tick.watch(f, StubTracker(f), StubDispatcher({"68a281700261": "vanished"}),
                       notifier, loop_config(), make_cfg())
    assert lines == ["infra#95: close_success"]
    assert ("close", "infra", 95, "") in f.label_ops
    assert notifier.sent == ["done"]


def test_a_pushed_run_whose_pipeline_never_appears_is_handed_over(monkeypatch):
    """The lock goes, a human is paged, and the issue says why in words."""
    import time
    ten_days_ago = time.time() - 10 * 86400
    f = _issue_95(declared_at=ten_days_ago + 870, started_at=ten_days_ago)
    _woodpecker_pages(monkeypatch, {1: _filler(12, 0)})
    notifier = StubNotifier()
    lines = tick.watch(f, StubTracker(f), StubDispatcher({"68a281700261": "vanished"}),
                       notifier, loop_config(), make_cfg())
    assert lines == ["infra#95: escalate_no_verdict"]
    assert ("remove", "infra", 95, "agent-in-progress") in f.label_ops
    assert ("add", "infra", 95, "needs-human") in f.label_ops
    assert ("close", "infra", 95, "") not in f.label_ops
    assert notifier.sent == ["needs-human"]
    explained = [b for _, b in f.posted if _SHA_95 in b]
    assert explained, "the hand-over must name the commit on the issue"


def test_the_verdict_clock_starts_when_the_commit_was_declared(monkeypatch):
    """A ten-day-old run that declared its commit five minutes ago is waiting
    on a pipeline that may be about to start, not on one that never will."""
    import time
    now = time.time()
    f = _issue_95(declared_at=now - 300, started_at=now - 10 * 86400)
    _woodpecker_pages(monkeypatch, {1: _filler(12, 0)})
    lines = tick.watch(f, StubTracker(f), StubDispatcher({"68a281700261": "vanished"}),
                       StubNotifier(), loop_config(), make_cfg())
    assert lines == ["infra#95: wait"]
