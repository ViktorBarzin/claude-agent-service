"""Tests for the breakglass app: verb whitelist, SSE translation, auth, routes."""
import os

os.environ.setdefault("API_BEARER_TOKEN", "test-token")

import pytest
from fastapi.testclient import TestClient

from app.breakglass import agent_session, pve
from app.breakglass.server import app


# --------------------------------------------------------------------------- #
# PVE verb whitelist — the security boundary mirrored client-side.
# --------------------------------------------------------------------------- #

def test_allowed_verbs_match_host_script():
    assert pve.ALLOWED_VERBS == {
        "status", "forensics", "reset", "stop", "start", "cycle"
    }
    assert pve.MUTATING_VERBS == {"reset", "stop", "start", "cycle"}
    assert pve.MUTATING_VERBS < pve.ALLOWED_VERBS


@pytest.mark.parametrize("bad", [
    "rm -rf /", "status; rm -rf /", "status 103", "shutdown", "", "STATUS",
    "cycle 999", "$(reboot)", "../start",
])
@pytest.mark.asyncio
async def test_run_verb_rejects_non_whitelisted_without_ssh(bad, monkeypatch):
    """A bad verb must be rejected locally — never spawning a subprocess."""
    called = False

    async def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("ssh must not run for a rejected verb")

    monkeypatch.setattr(pve.asyncio, "create_subprocess_exec", _boom)
    result = await pve.run_verb(bad)
    assert result["rejected"] is True
    assert result["exit_code"] is None
    assert called is False


@pytest.mark.asyncio
async def test_run_verb_allowed_invokes_ssh_with_bare_verb(monkeypatch):
    captured = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"status: running\n", b"")

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(pve.asyncio, "create_subprocess_exec", _fake_exec)
    result = await pve.run_verb("status")
    assert result["rejected"] is False
    assert result["exit_code"] == 0
    assert "running" in result["stdout"]
    # The verb is the LAST argv element, passed as a single token (no shell).
    assert captured["argv"][-1] == "status"
    assert captured["argv"][0] == "ssh"


# --------------------------------------------------------------------------- #
# stream-json -> UI event translation (pure function).
# --------------------------------------------------------------------------- #

def test_translate_init_to_session():
    ev = agent_session.translate_event(
        {"type": "system", "subtype": "init", "session_id": "abc"}
    )
    assert ev == {"kind": "session", "session_id": "abc"}


@pytest.mark.parametrize("noise", [
    {"type": "system", "subtype": "hook_started"},
    {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 5},
    {"type": "user", "message": {"content": []}},
    {"type": "unknown"},
])
def test_translate_drops_noise(noise):
    assert agent_session.translate_event(noise) is None


def test_translate_assistant_text():
    ev = agent_session.translate_event({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "checking disk"}]},
    })
    assert ev == {"kind": "text", "text": "checking disk"}


def test_translate_assistant_tool_use():
    ev = agent_session.translate_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "df -h"}}
        ]},
    })
    assert ev["kind"] == "tool"
    assert ev["name"] == "Bash"
    assert ev["input"]["command"] == "df -h"


def test_translate_result():
    ev = agent_session.translate_event({
        "type": "result", "is_error": False, "result": "done", "duration_ms": 1234,
    })
    assert ev == {"kind": "result", "is_error": False, "result": "done", "duration_ms": 1234}


# --------------------------------------------------------------------------- #
# Routes + auth.
# --------------------------------------------------------------------------- #

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}


def test_health_no_auth():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "claude-breakglass"


def test_api_requires_auth():
    assert client.post("/api/session").status_code == 401
    assert client.get("/api/pve/verbs").status_code == 401


def test_api_accepts_bearer():
    r = client.post("/api/session", headers=AUTH)
    assert r.status_code == 200
    assert "session_id" in r.json()


def test_api_accepts_authentik_header():
    r = client.post("/api/session", headers={"X-authentik-username": "me@viktorbarzin.me"})
    assert r.status_code == 200


def test_pve_verb_route_rejects_unknown():
    r = client.post("/api/pve/destroy", headers=AUTH)
    assert r.status_code == 400


def test_pve_verbs_listing():
    r = client.get("/api/pve/verbs", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert set(body["verbs"]) == pve.ALLOWED_VERBS
    assert set(body["mutating"]) == pve.MUTATING_VERBS


def test_chat_streams_sse(monkeypatch):
    async def _fake_turn(session_id, prompt, model=None):
        yield {"kind": "session", "session_id": session_id}
        yield {"kind": "text", "text": "hello"}
        yield {"kind": "result", "is_error": False, "result": "ok"}

    monkeypatch.setattr(agent_session, "run_turn", _fake_turn)
    r = client.post("/api/chat", headers=AUTH,
                    json={"session_id": "s1", "prompt": "diagnose"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    body = r.text
    assert "hello" in body
    assert '"kind": "done"' in body  # terminal frame always emitted
