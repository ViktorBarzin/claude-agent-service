"""The fixer's per-repo profile, and f1-stream's settings in it.

infra keeps every value it had before profiles existed: ``broken``, any
collaborator, ``issue-responder``, the infra prompts, Woodpecker follow-through,
``infra-agent`` ignored, comments never trigger. f1-stream takes the build
contract's table (f1-stream ``docs/design/2026-10-16-user-reports-build-contract.md``):
``approved``, Viktor only, ``f1-feedback-fixer``, its own prompts, no CI
follow-through in the service, ``infra-agent`` and ``tv-feedback-bot`` ignored,
and a reply comment from Viktor starts a continue turn.
"""
import datetime
import json

import pytest
from fastapi.testclient import TestClient

from app.afk.types import Config
from app.fixer import profiles, receiver, signature, tick
from app.fixer.config import FixerConfig
from app.fixer.execute_client import ExecuteClient
from app.fixer.gates import Delivery, Verdict, decide, parse_delivery
from app.fixer.runstate import RunRecord, latest_record, render_comment
from app.main import app

SECRET = "hook-secret"
F1 = "f1-stream"


def cfg(**kw) -> FixerConfig:
    return FixerConfig(token="t", webhook_secret="s", **kw)


# --------------------------------------------------------------------------- #
# The profiles.
# --------------------------------------------------------------------------- #
def test_infra_keeps_todays_values():
    p = profiles.profile_for("infra", cfg())
    assert p.trigger_label == "broken"
    assert p.trusted_actors is None          # the repo's collaborators, as before
    assert p.agent is None                   # FIXER_AGENT, as before
    assert p.ignored_actors == frozenset({"infra-agent"})
    assert p.ci_mode == profiles.CI_WOODPECKER
    assert p.comment_trigger is False
    assert p.playbook == profiles.PLAYBOOK_INFRA
    assert p.cleared_on_approval == frozenset()


def test_infra_follows_the_environment_overrides_it_always_had():
    p = profiles.profile_for("infra", cfg(trigger_label="fixme", bot_actor="bot2"))
    assert p.trigger_label == "fixme"
    assert p.ignored_actors == frozenset({"bot2"})


def test_f1_stream_takes_the_contracts_values():
    p = profiles.profile_for(F1, cfg())
    assert p.trigger_label == "approved"
    assert p.trusted_actors == frozenset({"viktor"})
    assert p.agent == "f1-feedback-fixer"
    assert p.ignored_actors == frozenset({"infra-agent", "tv-feedback-bot"})
    assert p.ci_mode == profiles.CI_NONE
    assert p.comment_trigger is True
    assert p.playbook == profiles.PLAYBOOK_F1
    assert p.cleared_on_approval == frozenset({"needs-approval"})


def test_an_unknown_repo_gets_the_infra_style_default():
    assert profiles.profile_for("other", cfg()) == profiles.profile_for("infra", cfg())


# --------------------------------------------------------------------------- #
# The gates, one row each, with f1-stream's profile.
# --------------------------------------------------------------------------- #
ARMED = Config(allowlist=["infra", F1], kill_switch=False)


def f1_delivery(**kw) -> Delivery:
    base = dict(event="issue_comment", action="created", repo=F1, number=5,
                actor="viktor", labels=frozenset({"approved", "problem"}),
                body="Try the other feed first.")
    base.update(kw)
    return Delivery(**base)


def f1_verdict(delivery: Delivery, config: Config = ARMED) -> Verdict:
    p = profiles.profile_for(F1, cfg())
    return decide(delivery, config, trigger_label=p.trigger_label,
                  bot_actor=p.ignored_actors, trusted_actors=p.trusted_actors or frozenset(),
                  comment_trigger=p.comment_trigger)


def test_a_viktor_reply_on_an_approved_issue_continues_the_run():
    assert f1_verdict(f1_delivery()) is Verdict.CONTINUE


@pytest.mark.parametrize("action", ["edited", "deleted", ""])
def test_only_a_created_comment_triggers(action):
    assert f1_verdict(f1_delivery(action=action)) is Verdict.NOT_A_TRIGGER


def test_a_comment_on_a_closed_issue_does_not_trigger():
    assert f1_verdict(f1_delivery(state="closed")) is Verdict.NOT_A_TRIGGER


def test_a_comment_on_an_unapproved_issue_does_not_trigger():
    d = f1_delivery(labels=frozenset({"needs-approval", "problem"}))
    assert f1_verdict(d) is Verdict.NOT_A_TRIGGER


@pytest.mark.parametrize("body", ["To reporter: which channel?", "  To reporter: hi"])
def test_a_relay_to_the_reporter_does_not_trigger(body):
    assert f1_verdict(f1_delivery(body=body)) is Verdict.NOT_A_TRIGGER


@pytest.mark.parametrize("actor", ["infra-agent", "tv-feedback-bot"])
def test_the_bots_comments_are_ignored(actor):
    assert f1_verdict(f1_delivery(actor=actor)) is Verdict.OWN_ACTION


def test_a_comment_while_a_run_is_in_progress_does_not_trigger():
    d = f1_delivery(labels=frozenset({"approved", "agent-in-progress"}))
    assert f1_verdict(d) is Verdict.RUN_IN_PROGRESS


def test_a_paused_issue_does_not_continue():
    d = f1_delivery(labels=frozenset({"approved", "paused"}))
    assert f1_verdict(d) is Verdict.PAUSED


def test_the_kill_switch_stops_a_continue():
    assert f1_verdict(f1_delivery(), Config(allowlist=[F1], kill_switch=True)) \
        is Verdict.KILL_SWITCH


@pytest.mark.parametrize("actor", ["ebarzin", "someone", ""])
def test_only_viktor_may_continue(actor):
    assert f1_verdict(f1_delivery(actor=actor)) is Verdict.UNTRUSTED_ACTOR


def test_an_unenrolled_f1_stream_does_not_continue():
    assert f1_verdict(f1_delivery(), Config(allowlist=["infra"], kill_switch=False)) \
        is Verdict.REPO_NOT_ENROLLED


def test_the_approved_label_from_viktor_dispatches():
    d = f1_delivery(event="issues", action="label_updated", body="")
    assert f1_verdict(d) is Verdict.DISPATCH


@pytest.mark.parametrize("actor", ["ebarzin", "tv-feedback-bot", "infra-agent"])
def test_the_approved_label_from_anyone_else_does_not(actor):
    d = f1_delivery(event="issues", action="label_updated", body="", actor=actor)
    assert f1_verdict(d) in (Verdict.UNTRUSTED_ACTOR, Verdict.OWN_ACTION)


def test_infra_comments_still_never_trigger():
    p = profiles.profile_for("infra", cfg())
    d = Delivery(event="issue_comment", action="created", repo="infra", number=1,
                 actor="viktor", labels=frozenset({"broken"}), body="please")
    assert decide(d, ARMED, trigger_label=p.trigger_label, bot_actor=p.ignored_actors,
                  trusted_actors=frozenset({"viktor"}),
                  comment_trigger=p.comment_trigger) is Verdict.NOT_A_TRIGGER


def test_parse_delivery_reads_the_comment_body():
    d = parse_delivery("issue_comment", {
        "action": "created",
        "issue": {"number": 5, "state": "open", "labels": [{"name": "approved"}]},
        "comment": {"body": "go on"},
        "repository": {"name": F1},
        "sender": {"login": "viktor"},
    })
    assert d is not None and d.body == "go on" and d.repo == F1


# --------------------------------------------------------------------------- #
# The receiver.
# --------------------------------------------------------------------------- #
class StubForgejo:
    def __init__(self):
        self.in_progress: list[dict] = []
        self.ops: list[tuple] = []
        self.comments: list[tuple[str, int, str]] = []
        self.trusted_asked = False

    def trusted_actors(self, repo):
        self.trusted_asked = True
        return frozenset({"viktor", "ebarzin"})

    def list_issues(self, repo, label):
        return list(self.in_progress)

    def get_issue(self, repo, number):
        return {"title": "Bahrain replay stops"}

    def add_label(self, repo, number, label):
        self.ops.append(("add", repo, number, label))

    def remove_label(self, repo, number, label):
        self.ops.append(("remove", repo, number, label))

    def comment(self, repo, number, body):
        self.comments.append((repo, number, body))


@pytest.fixture
def stub(monkeypatch):
    s = StubForgejo()
    monkeypatch.setattr(receiver, "_client", lambda c: s)
    return s


@pytest.fixture
def submitted():
    calls: list[dict] = []

    def submit(prompt: str, issue: str = "", agent: str | None = None) -> str:
        calls.append({"prompt": prompt, "issue": issue, "agent": agent})
        return "job-f1"

    receiver.set_submitter(submit)
    yield calls
    receiver.set_submitter(None)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("FIXER_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("FIXER_FORGEJO_TOKEN", "forgejo-token")
    monkeypatch.setenv("AFK_KILL_SWITCH", "false")
    monkeypatch.setenv("AFK_ALLOWLIST", "infra,f1-stream")


def post(body: dict, event: str):
    raw = json.dumps(body).encode()
    return TestClient(app).post("/hooks/forgejo", content=raw, headers={
        "Content-Type": "application/json", "X-Forgejo-Event": event,
        "X-Forgejo-Signature": signature.expected_signature(SECRET, raw)})


def comment_payload(body="Use the backup feed.", labels=("approved", "needs-human"),
                    sender="viktor") -> dict:
    return {
        "action": "created",
        "issue": {"number": 5, "state": "open", "title": "Bahrain replay stops",
                  "labels": [{"name": n} for n in labels]},
        "comment": {"body": body},
        "repository": {"name": F1, "full_name": "viktor/f1-stream"},
        "sender": {"login": sender},
    }


def label_payload(sender="viktor", labels=("approved", "problem")) -> dict:
    return {
        "action": "label_updated",
        "issue": {"number": 5, "state": "open", "title": "Bahrain replay stops",
                  "labels": [{"name": n} for n in labels]},
        "repository": {"name": F1, "full_name": "viktor/f1-stream"},
        "sender": {"login": sender},
    }


def test_viktors_approval_dispatches_the_f1_fixer(env, stub, submitted):
    r = post(label_payload(), "issues")
    assert r.json()["reason"] == "dispatched"
    [call] = submitted
    assert call["agent"] == "f1-feedback-fixer"
    assert call["issue"] == "f1-stream#5"
    assert "docs/issue-bar.md" in call["prompt"]
    assert ("add", F1, 5, "agent-in-progress") in stub.ops
    # Trust is fixed for f1-stream: the collaborator list is not consulted.
    assert stub.trusted_asked is False


def test_viktors_approval_takes_needs_approval_off(env, stub, submitted):
    post(label_payload(labels=("approved", "needs-approval", "idea")), "issues")
    assert ("remove", F1, 5, "needs-approval") in stub.ops


def test_an_approval_queued_behind_a_busy_repo_still_takes_needs_approval_off(
        env, stub, submitted):
    stub.in_progress = [{"number": 9, "labels": [{"name": "agent-in-progress"}]}]
    r = post(label_payload(labels=("approved", "needs-approval")), "issues")
    assert r.json()["reason"] == "repo-locked"
    assert stub.ops == [("remove", F1, 5, "needs-approval")]


def test_an_approval_without_needs_approval_removes_nothing(env, stub, submitted):
    post(label_payload(), "issues")
    assert not [op for op in stub.ops if op[0] == "remove"]


def test_someone_elses_approval_leaves_needs_approval_on(env, stub, submitted):
    post(label_payload(sender="ebarzin", labels=("approved", "needs-approval")), "issues")
    assert stub.ops == []


def test_a_collaborators_approval_does_not_dispatch(env, stub, submitted):
    assert post(label_payload(sender="ebarzin"), "issues").json()["reason"] \
        == "untrusted-actor"
    assert submitted == []


def test_the_feedback_bots_labels_are_ignored(env, stub, submitted):
    assert post(label_payload(sender="tv-feedback-bot"), "issues").json()["reason"] \
        == "own-action"


def test_a_viktor_reply_continues_with_his_words(env, stub, submitted):
    r = post(comment_payload(), "issue_comment")
    assert r.json()["reason"] == "continued"
    [call] = submitted
    assert call["agent"] == "f1-feedback-fixer"
    assert "Use the backup feed." in call["prompt"]
    assert ("remove", F1, 5, "needs-human") in stub.ops
    assert ("add", F1, 5, "agent-in-progress") in stub.ops
    [(_, _, body)] = stub.comments
    record = latest_record([body])
    assert record is not None and record.job_id == "job-f1"


def test_a_reply_without_needs_human_removes_nothing(env, stub, submitted):
    post(comment_payload(labels=("approved",)), "issue_comment")
    assert not [op for op in stub.ops if op[0] == "remove"]


def test_a_relay_comment_does_not_continue(env, stub, submitted):
    r = post(comment_payload(body="To reporter: which channel?"), "issue_comment")
    assert r.json()["reason"] == "not-a-trigger"
    assert submitted == [] and stub.ops == []


def test_a_reply_while_the_repo_is_busy_queues_the_issue_for_the_drain(env, stub, submitted):
    """The drain skips issues with needs-human, so taking it off is what puts
    Viktor's reply in the queue; the next free tick starts it."""
    stub.in_progress = [{"number": 9, "labels": [{"name": "agent-in-progress"}]}]
    r = post(comment_payload(), "issue_comment")
    assert r.json()["reason"] == "repo-locked"
    assert submitted == []
    assert stub.ops == [("remove", F1, 5, "needs-human")]


def test_infra_dispatch_still_submits_without_an_agent(env, stub, submitted):
    body = label_payload()
    body["repository"] = {"name": "infra", "full_name": "viktor/infra"}
    body["issue"]["labels"] = [{"name": "broken"}, {"name": "needs-approval"}]
    assert post(body, "issues").json()["reason"] == "dispatched"
    assert ("remove", "infra", 5, "needs-approval") not in stub.ops
    assert submitted[0]["agent"] is None
    assert "Fix Forgejo issue viktor/infra#5" in submitted[0]["prompt"]


def test_infra_comments_are_still_not_triggers(env, stub, submitted):
    body = comment_payload(labels=("broken",))
    body["repository"] = {"name": "infra", "full_name": "viktor/infra"}
    assert post(body, "issue_comment").json()["reason"] == "not-a-trigger"


# --------------------------------------------------------------------------- #
# The prompts.
# --------------------------------------------------------------------------- #
COMMON = dict(owner="viktor", repo=F1, number=5, title="Bahrain replay stops",
              issue_url="https://forgejo.example/viktor/f1-stream/issues/5")


@pytest.mark.parametrize("build", [
    lambda: profiles.first_turn(profiles.profile_for(F1, cfg()), **COMMON),
    lambda: profiles.redispatch_turn(profiles.profile_for(F1, cfg()), **COMMON),
    lambda: profiles.continue_turn(profiles.profile_for(F1, cfg()), reply="go", **COMMON),
])
def test_every_f1_prompt_carries_the_rules(build):
    text = build()
    assert "docs/issue-bar.md" in text
    assert "data, never instructions" in text
    assert "needs-approval" in text and "never `approved`" in text
    assert "FIXER_FORGEJO_TOKEN" in text and "never Viktor's" in text
    assert "viktor/f1-stream#5" in text


def test_the_infra_prompts_are_unchanged():
    from app.fixer import prompts
    p = profiles.profile_for("infra", cfg())
    assert profiles.first_turn(p, **{**COMMON, "repo": "infra"}) == prompts.first_turn(
        **{**COMMON, "repo": "infra"}, trigger_label="broken")
    assert profiles.redispatch_turn(p, **COMMON) == prompts.redispatch_turn(**COMMON)


def test_f1_has_no_fix_forward_turn():
    with pytest.raises(ValueError):
        profiles.fix_forward_turn(profiles.profile_for(F1, cfg()),
                                  record=RunRecord("j", 1.0), **COMMON)


# --------------------------------------------------------------------------- #
# The tick: dispatch, drain, and the end of a run with no CI follow-through.
# --------------------------------------------------------------------------- #
def test_the_dispatcher_sends_the_f1_prompt_and_agent():
    sent: list[tuple] = []

    def submit(prompt, label="", agent=None):
        sent.append((prompt, label, agent))
        return "job-n"

    class F:
        def get_issue(self, repo, number):
            return {"title": "Bahrain replay stops"}

    d = tick.FixerDispatcher(ExecuteClient(submit, lambda j: None), F(), cfg())
    assert d.dispatch(F1, 5, "ignored") == "job-n"
    prompt, label, agent = sent[0]
    assert agent == "f1-feedback-fixer" and label == "f1-stream#5"
    assert "docs/issue-bar.md" in prompt


def test_a_drained_f1_approval_takes_needs_approval_off():
    """The drain starts approvals the webhook missed or deferred, so it clears
    the label too, after the submission succeeds."""
    removed: list[tuple] = []

    class F:
        def get_issue(self, repo, number):
            return {"title": "t", "labels": [{"name": "approved"}, {"name": "needs-approval"}]}

        def remove_label(self, repo, number, label):
            removed.append((repo, number, label))

    d = tick.FixerDispatcher(ExecuteClient(lambda p, label="", agent=None: "job-n",
                                           lambda j: None), F(), cfg())
    d.dispatch(F1, 5, "ignored")
    assert removed == [(F1, 5, "needs-approval")]


def test_a_failed_drain_submission_leaves_needs_approval_on():
    removed: list[tuple] = []

    class F:
        def get_issue(self, repo, number):
            return {"title": "t", "labels": [{"name": "needs-approval"}]}

        def remove_label(self, repo, number, label):
            removed.append((repo, number, label))

    def submit(prompt, label="", agent=None):
        raise RuntimeError("service down")

    d = tick.FixerDispatcher(ExecuteClient(submit, lambda j: None), F(), cfg())
    with pytest.raises(RuntimeError):
        d.dispatch(F1, 5, "ignored")
    assert removed == []


def test_the_service_jobs_client_posts_the_agent_it_is_given(monkeypatch):
    calls = []
    jobs = tick.ServiceJobs("http://svc", "tok", "issue-responder", cfg())
    monkeypatch.setattr(jobs, "_call", lambda m, p, b=None: calls.append(b) or {"job_id": "x"})
    jobs.submit("p", "f1-stream#5", "f1-feedback-fixer")
    jobs.submit("p", "infra#1")
    assert [c["agent"] for c in calls] == ["f1-feedback-fixer", "issue-responder"]


class LabelForgejo:
    """Timeline label events and issue lists, as the drain's tracker reads them."""

    def __init__(self, issues, actor):
        self._issues = issues
        self._actor = actor

    def list_issues(self, repo, label):
        return [i for i in self._issues.get(repo, [])
                if label in [x["name"] for x in i["labels"]]]

    def label_events(self, repo, number):
        assoc = "OWNER" if self._actor == "viktor" else "COLLABORATOR"
        return [{"event": "labeled", "label": {"name": "approved"},
                 "author_association": assoc}]


@pytest.mark.parametrize("actor,trusted", [("viktor", True), ("ebarzin", False)])
def test_the_drain_reads_f1_issues_by_approved_and_trusts_only_viktor(actor, trusted):
    f = LabelForgejo({F1: [{"number": 5, "labels": [{"name": "approved"}], "body": ""}],
                      "infra": [{"number": 8, "labels": [{"name": "broken"}], "body": ""}]},
                     actor)
    t = profiles.RepoTrackers(f, cfg())
    ready = {(i.repo, i.number): i.labeled_by_trusted for i in t.list_ready(["infra", F1])}
    assert ready[(F1, 5)] is trusted
    assert (("infra", 8) in ready) is True


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


class F1Forgejo:
    def __init__(self, labels=("approved", "agent-in-progress"), comments=(), closed=()):
        self.open = [{"number": 5, "labels": [{"name": n} for n in labels]}]
        self.closed = list(closed)
        self.comments_by = {5: list(comments)}
        self.ops: list[tuple] = []
        self.posted: list[tuple[int, str]] = []

    def list_issues(self, repo, label, state="open"):
        pool = self.open if state == "open" else self.closed
        return [i for i in pool if label in [x["name"] for x in i["labels"]]]

    def list_comments(self, repo, number):
        return list(self.comments_by.get(number, []))

    def get_issue(self, repo, number):
        return {"title": "t"}

    def add_label(self, repo, number, label):
        self.ops.append(("add", number, label))

    def remove_label(self, repo, number, label):
        self.ops.append(("remove", number, label))

    def comment(self, repo, number, body):
        self.posted.append((number, body))
        self.comments_by.setdefault(number, []).append(
            {"body": body, "user": {"login": "infra-agent"}, "created_at": _iso(10_000.0)})


class StubDispatcher:
    def __init__(self, states):
        self.states = states
        self.dispatched: list[tuple] = []

    def dispatch(self, repo, issue, prompt, *, kind="first"):
        self.dispatched.append((repo, issue, kind))
        return "job-again"

    def snapshot(self):
        return {"threads": [{"id": j, "latestTurn": {"state": s}}
                            for j, s in self.states.items()]}

    def track(self, job_id):
        self.states.setdefault(job_id, "running")


class Notifier:
    def __init__(self):
        self.sent = []

    def notify(self, kind, issue, thread_id, detail):
        self.sent.append(kind)


LOOP = Config(allowlist=[F1], kill_switch=False, human_label="needs-human")


def _footer(started_at=1000.0, job="job-1"):
    return {"body": render_comment("Picked this up.", RunRecord(job, started_at)),
            "user": {"login": "infra-agent"}, "created_at": _iso(started_at)}


def _comment(body, at, who="infra-agent"):
    return {"body": body, "user": {"login": who}, "created_at": _iso(at)}


def _watch(f, states):
    notifier = Notifier()
    lines = tick.watch(f, None, StubDispatcher(states), notifier, LOOP, cfg())
    return lines, notifier


def test_a_running_f1_job_is_left_alone():
    f = F1Forgejo(comments=[_footer()])
    lines, notifier = _watch(f, {"job-1": "running"})
    assert lines == ["f1-stream#5: wait"]
    assert f.ops == [] and f.posted == [] and notifier.sent == []


@pytest.mark.parametrize("state", ["completed", "errored"])
def test_a_run_that_ends_without_a_fix_shipped_goes_to_viktor(state):
    f = F1Forgejo(comments=[_footer(), _comment("Looked around, nothing yet.", 1500)])
    lines, notifier = _watch(f, {"job-1": state})
    assert lines == ["f1-stream#5: ended without a fix, handed over"]
    assert f.ops == [("remove", 5, "agent-in-progress"), ("add", 5, "needs-human")]
    assert f.posted[-1][1].startswith(
        "The run ended without shipping a fix or asking a question; over to Viktor.")
    assert notifier.sent == ["needs-human"]


def test_a_fix_shipped_before_this_run_started_does_not_count():
    f = F1Forgejo(comments=[_comment("Fix shipped: server", 500), _footer(started_at=1000)])
    lines, _ = _watch(f, {"job-1": "completed"})
    assert ("add", 5, "needs-human") in f.ops


def test_a_fix_shipped_by_someone_else_does_not_count():
    f = F1Forgejo(comments=[_footer(), _comment("Fix shipped: server", 1500, who="ebarzin")])
    _watch(f, {"job-1": "completed"})
    assert ("add", 5, "needs-human") in f.ops


def test_a_run_that_asked_for_help_only_releases_the_lock():
    f = F1Forgejo(labels=("approved", "agent-in-progress", "needs-human"),
                  comments=[_footer(), _comment("Need a TV check.", 1500)])
    lines, notifier = _watch(f, {"job-1": "completed"})
    assert f.ops == [("remove", 5, "agent-in-progress")]
    assert f.posted == [] and notifier.sent == []
    assert lines == ["f1-stream#5: ended, with a human"]


def test_a_run_that_shipped_but_left_the_issue_open_also_goes_to_viktor():
    """Without needs-human the drain would start the same issue again on the
    next tick, and again after that, so an open issue at the end of a run is
    always handed over; the comment says the fix shipped."""
    f = F1Forgejo(comments=[_footer(), _comment("Fix shipped: TV version 2.45.0+1201\n\nwatching",
                                                1500)])
    lines, notifier = _watch(f, {"job-1": "completed"})
    assert lines == ["f1-stream#5: ended after a fix shipped, handed over"]
    assert f.ops == [("remove", 5, "agent-in-progress"), ("add", 5, "needs-human")]
    assert "shipped" in f.posted[-1][1]
    assert notifier.sent == ["needs-human"]


def test_a_vanished_job_with_nothing_shipped_restarts_once():
    f = F1Forgejo(comments=[_footer()])
    states = {"job-1": "vanished"}
    dispatcher = StubDispatcher(states)
    lines = tick.watch(f, None, dispatcher, Notifier(), LOOP, cfg())
    assert lines == ["f1-stream#5: redispatch"]
    assert dispatcher.dispatched == [(F1, 5, "redispatch")]
    record = latest_record([c["body"] for c in f.comments_by[5]])
    assert record.job_id == "job-again" and record.redispatch_attempts == 1
    assert f.ops == []


def test_a_closed_issue_still_holding_the_lock_is_released_once_its_job_ends():
    closed = {"number": 6, "labels": [{"name": "approved"}, {"name": "agent-in-progress"}]}
    f = F1Forgejo(labels=("approved",), closed=[closed])
    f.comments_by[6] = [_footer(job="job-6")]
    lines, notifier = _watch(f, {"job-6": "completed"})
    assert ("remove", 6, "agent-in-progress") in f.ops
    assert ("add", 6, "needs-human") not in f.ops
    assert lines == ["f1-stream#6: closed, lock released"]


def test_a_closed_issue_whose_job_still_runs_keeps_the_lock():
    closed = {"number": 6, "labels": [{"name": "agent-in-progress"}]}
    f = F1Forgejo(labels=("approved",), closed=[closed])
    f.comments_by[6] = [_footer(job="job-6")]
    _watch(f, {"job-6": "running"})
    assert f.ops == []


def test_an_f1_issue_with_no_run_state_is_handed_over_like_infra():
    f = F1Forgejo(comments=[])
    lines, _ = _watch(f, {})
    assert lines == ["f1-stream#5: orphaned, escalated"]
    assert ("remove", 5, "agent-in-progress") in f.ops


def test_the_infra_ci_watcher_is_never_asked_about_f1(monkeypatch):
    class NoCI:
        def status(self, repo, commit):
            raise AssertionError("CI consulted for f1-stream")

    monkeypatch.setattr(tick, "_ci_watcher", lambda: NoCI())
    f = F1Forgejo(comments=[_footer(), _comment("Pushed-Commit: abcdef1234", 1500)])
    _watch(f, {"job-1": "completed"})
