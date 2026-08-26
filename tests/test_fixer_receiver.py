"""Tests for ``POST /hooks/forgejo`` — signature, gates, lock, dispatch.

Driven through the real FastAPI app with a TestClient, so the HTTP contract is
under test too: which refusals answer 200 (the common, expected ones) versus 401
and 503 (real faults). The Forgejo client is stubbed at the module seam, so no
socket opens and the assertions are about what the receiver decided, not about
Forgejo's wire format — that is ``test_forgejo_client.py``'s job.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.fixer import receiver, signature
from app.main import app

SECRET = "hook-secret"


class StubForgejo:
    """Records mutations; answers reads from staged state."""

    def __init__(self):
        self.trusted = frozenset({"viktor", "ebarzin"})
        self.in_progress: list[dict] = []
        self.issue = {"title": "tuya-bridge workers hang"}
        self.labels_added: list[tuple[str, int, str]] = []
        self.comments: list[tuple[str, int, str]] = []

    def trusted_actors(self, repo):
        return self.trusted

    def list_issues(self, repo, label):
        return list(self.in_progress)

    def get_issue(self, repo, number):
        return self.issue

    def add_label(self, repo, number, label):
        self.labels_added.append((repo, number, label))

    def comment(self, repo, number, body):
        self.comments.append((repo, number, body))


@pytest.fixture
def stub(monkeypatch):
    s = StubForgejo()
    monkeypatch.setattr(receiver, "_client", lambda cfg: s)
    return s


@pytest.fixture
def submitted(monkeypatch):
    class Recorder(list):
        """A list of prompts that also remembers the issue label each carried."""
        issues: list[str] = []

    calls = Recorder()
    submitted_issues: list[str] = []

    def submit(prompt: str, issue: str = "") -> str:
        calls.append(prompt)
        submitted_issues.append(issue)
        return "job-77"

    receiver.set_submitter(submit)
    calls.issues = submitted_issues
    yield calls
    receiver.set_submitter(None)


@pytest.fixture
def env(monkeypatch):
    """An armed fixer: secret + token present, infra enrolled, switch off."""
    monkeypatch.setenv("FIXER_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("FIXER_FORGEJO_TOKEN", "forgejo-token")
    monkeypatch.setenv("AFK_KILL_SWITCH", "false")
    monkeypatch.setenv("AFK_ALLOWLIST", "infra")


@pytest.fixture
def client():
    return TestClient(app)


def payload(**kw) -> dict:
    base = {
        "action": "label_updated",
        "issue": {
            "number": 42,
            "state": "open",
            "title": "tuya-bridge workers hang",
            "labels": [{"name": "broken"}],
        },
        "repository": {"name": "infra", "full_name": "viktor/infra"},
        "sender": {"login": "ebarzin"},
    }
    base.update(kw)
    return base


def post(client, body: dict, *, secret: str = SECRET, event: str = "issues", sign: bool = True):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "X-Forgejo-Event": event}
    if sign:
        headers["X-Forgejo-Signature"] = signature.expected_signature(secret, raw)
    return client.post("/hooks/forgejo", content=raw, headers=headers)


# --------------------------------------------------------------------------- #
# The happy path.
# --------------------------------------------------------------------------- #
def test_a_signed_broken_label_dispatches_a_run(client, env, stub, submitted):
    r = post(client, payload())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "reason": "dispatched",
                        "job_id": "job-77", "issue": "infra#42"}
    assert len(submitted) == 1


def test_the_run_is_labelled_with_its_issue(client, env, stub, submitted):
    """The log file is named for the issue, so a transcript is findable by what
    it was about rather than only by an opaque job id."""
    post(client, payload())
    assert submitted.issues == ["infra#42"]


def test_the_prompt_names_the_issue_and_its_url(client, env, stub, submitted):
    post(client, payload())
    prompt = submitted[0]
    assert "viktor/infra#42" in prompt
    assert "forgejo.viktorbarzin.me/viktor/infra/issues/42" in prompt
    assert "tuya-bridge workers hang" in prompt


def test_dispatch_takes_the_lock_and_says_so_on_the_issue(client, env, stub, submitted):
    post(client, payload())
    assert stub.labels_added == [("infra", 42, "agent-in-progress")]
    repo, number, body = stub.comments[0]
    assert (repo, number) == ("infra", 42)
    assert "investigating" in body
    assert "fixer-state:" in body and "job-77" in body


def test_the_label_is_only_applied_after_a_successful_dispatch(client, env, stub, monkeypatch):
    """A submission that raises must not leave a phantom lock on the repo."""
    def boom(prompt, issue=""):
        raise RuntimeError("runner down")

    receiver.set_submitter(boom)
    try:
        with pytest.raises(RuntimeError):
            post(client, payload())
    finally:
        receiver.set_submitter(None)
    assert stub.labels_added == []
    assert stub.comments == []


# --------------------------------------------------------------------------- #
# Signature — the one refusal that is a real fault.
# --------------------------------------------------------------------------- #
def test_a_wrong_signature_is_401_and_dispatches_nothing(client, env, stub, submitted):
    r = post(client, payload(), secret="not-the-secret")
    assert r.status_code == 401
    assert r.json()["reason"] == "bad-signature"
    assert submitted == []


def test_a_missing_signature_is_401(client, env, stub, submitted):
    r = post(client, payload(), sign=False)
    assert r.status_code == 401
    assert submitted == []


def test_a_tampered_body_is_401(client, env, stub, submitted):
    """The signature covers the raw bytes, so any edit invalidates it."""
    raw = json.dumps(payload()).encode()
    good = signature.expected_signature(SECRET, raw)
    tampered = raw.replace(b'"number": 42', b'"number": 43')
    r = client.post("/hooks/forgejo", content=tampered, headers={
        "Content-Type": "application/json",
        "X-Forgejo-Event": "issues",
        "X-Forgejo-Signature": good,
    })
    assert r.status_code == 401
    assert submitted == []


def test_the_gitea_header_spelling_is_accepted(client, env, stub, submitted):
    raw = json.dumps(payload()).encode()
    r = client.post("/hooks/forgejo", content=raw, headers={
        "Content-Type": "application/json",
        "X-Gitea-Event": "issues",
        "X-Gitea-Signature": signature.expected_signature(SECRET, raw),
    })
    assert r.status_code == 200 and r.json()["ok"] is True


# --------------------------------------------------------------------------- #
# Not-configured — fail closed.
# --------------------------------------------------------------------------- #
def test_no_secret_configured_refuses_everything(client, monkeypatch, stub, submitted):
    monkeypatch.delenv("FIXER_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("FIXER_FORGEJO_TOKEN", "t")
    r = post(client, payload())
    assert r.status_code == 503
    assert r.json()["reason"] == "not-configured"
    assert submitted == []


def test_no_token_configured_refuses_everything(client, monkeypatch, stub, submitted):
    monkeypatch.setenv("FIXER_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("FIXER_FORGEJO_TOKEN", raising=False)
    r = post(client, payload())
    assert r.status_code == 503
    assert submitted == []


# --------------------------------------------------------------------------- #
# Gate refusals — all 200, so Forgejo never disables the hook over them.
# --------------------------------------------------------------------------- #
def test_the_bots_own_label_is_ignored(client, env, stub, submitted):
    r = post(client, payload(sender={"login": "infra-agent"}))
    assert r.status_code == 200
    assert r.json()["reason"] == "own-action"
    assert submitted == []


def test_a_change_labelled_issue_is_not_a_trigger(client, env, stub, submitted):
    body = payload()
    body["issue"]["labels"] = [{"name": "change"}]
    assert post(client, body).json()["reason"] == "not-a-trigger"
    assert submitted == []


def test_a_paused_issue_is_not_picked_up(client, env, stub, submitted):
    body = payload()
    body["issue"]["labels"] = [{"name": "broken"}, {"name": "paused"}]
    assert post(client, body).json()["reason"] == "paused"
    assert submitted == []


def test_the_kill_switch_refuses(client, env, stub, submitted, monkeypatch):
    monkeypatch.setenv("AFK_KILL_SWITCH", "true")
    assert post(client, payload()).json()["reason"] == "kill-switch"
    assert submitted == []


def test_an_untrusted_actor_is_refused(client, env, stub, submitted):
    assert post(client, payload(sender={"login": "stranger"})).json()["reason"] == "untrusted-actor"
    assert submitted == []


def test_an_unenrolled_repo_is_refused(client, env, stub, submitted, monkeypatch):
    monkeypatch.setenv("AFK_ALLOWLIST", "some-other-repo")
    assert post(client, payload()).json()["reason"] == "repo-not-enrolled"
    assert submitted == []


def test_a_non_issue_payload_is_a_no_op(client, env, stub, submitted):
    r = post(client, {"ref": "refs/heads/master"}, event="push")
    assert r.status_code == 200
    assert r.json()["reason"] == "not-an-issue-event"
    assert submitted == []


# --------------------------------------------------------------------------- #
# The per-repo lock.
# --------------------------------------------------------------------------- #
def test_a_second_issue_is_deferred_while_a_run_holds_the_repo(
    client, env, stub, submitted
):
    stub.in_progress = [{"number": 41, "labels": [{"name": "agent-in-progress"}]}]
    r = post(client, payload())
    assert r.status_code == 200
    assert r.json()["reason"] == "repo-locked"
    assert submitted == []
    assert stub.labels_added == []
