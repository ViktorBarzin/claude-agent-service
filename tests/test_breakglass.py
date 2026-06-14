"""Tests for the breakglass app: session manager (attach model), verb whitelist,
SSE translation, auth, routes."""
import os

os.environ.setdefault("API_BEARER_TOKEN", "test-token")
# Turns chdir into a per-session workspace; point it somewhere writable for tests
# (prod uses the /workspace emptyDir). Must be set before the app imports config.
os.environ.setdefault("BREAKGLASS_SESSIONS_DIR", "/tmp/bg-test-sessions")

import pytest
from fastapi.testclient import TestClient

from app.breakglass import agent_session, pve, session as sessionmod
from app.breakglass.server import app


# --------------------------------------------------------------------------- #
# Fakes for the claude subprocess a turn spawns.
# --------------------------------------------------------------------------- #
class _FakeStdout:
    def __init__(self, lines):
        self._lines = [(l + "\n").encode() for l in lines]
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._i]
        self._i += 1
        return line


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()
        self.returncode = None
        self._rc = rc

    async def wait(self):
        self.returncode = self._rc
        return self._rc

    def kill(self):
        self.returncode = -9


def _patch_proc(monkeypatch, lines, rc=0):
    async def _fake_spawn(*argv, **kwargs):
        return _FakeProc(lines, rc)
    monkeypatch.setattr(sessionmod.asyncio, "create_subprocess_exec", _fake_spawn)


_TURN_LINES = [
    '{"type":"system","subtype":"init","session_id":"s"}',
    '{"type":"system","subtype":"thinking_tokens","estimated_tokens":5}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"checking disk"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"df -h"}}]}}',
    '{"type":"result","is_error":false,"result":"done","duration_ms":12}',
]


# --------------------------------------------------------------------------- #
# Session: event log + broadcast + replay/Last-Event-ID.
# --------------------------------------------------------------------------- #
def test_add_event_assigns_sequential_ids():
    s = sessionmod.Session("s1")
    a = s.add_event({"kind": "user", "text": "hi"})
    b = s.add_event({"kind": "text", "text": "yo"})
    assert a["id"] == 0 and b["id"] == 1
    assert [e["kind"] for e in s.events] == ["user", "text"]


def test_subscribe_receives_broadcast():
    s = sessionmod.Session("s1")
    q = s.subscribe()
    s.add_event({"kind": "text", "text": "live"})
    assert q.get_nowait()["text"] == "live"
    s.unsubscribe(q)
    s.add_event({"kind": "text", "text": "after"})
    assert q.empty()


@pytest.mark.asyncio
async def test_attach_replays_then_signals_caught_up():
    s = sessionmod.Session("s1")
    s.add_event({"kind": "user", "text": "diagnose"})
    s.add_event({"kind": "text", "text": "looking"})
    frames = []
    async for frame in sessionmod.attach_stream(s, last_event_id=None):
        frames.append(frame)
        if "caught-up" in frame:
            break
    body = "".join(frames)
    assert "diagnose" in body and "looking" in body
    assert "id: 0" in body and "id: 1" in body
    assert "event: caught-up" in frames[-1]


@pytest.mark.asyncio
async def test_attach_reconnect_replays_only_missed():
    s = sessionmod.Session("s1")
    for i in range(3):
        s.add_event({"kind": "text", "text": f"e{i}"})  # ids 0,1,2
    frames = []
    async for frame in sessionmod.attach_stream(s, last_event_id=0):  # already saw id 0
        frames.append(frame)
        if "caught-up" in frame:
            break
    body = "".join(frames)
    assert "e0" not in body  # not re-sent
    assert "e1" in body and "e2" in body


# --------------------------------------------------------------------------- #
# Session: running a detached turn (mocked subprocess).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_turn_streams_events_into_log(monkeypatch):
    _patch_proc(monkeypatch, _TURN_LINES)
    s = sessionmod.Session("s1")
    assert s.start_turn("diagnose the devvm") is True
    await s._turn  # wait for the detached turn to finish
    kinds = [e["kind"] for e in s.events]
    assert kinds[0] == "user"
    assert "session" in kinds and "text" in kinds and "tool" in kinds
    assert "result" in kinds and kinds[-1] == "turn_end"
    assert "thinking_tokens" not in kinds


@pytest.mark.asyncio
async def test_one_turn_at_a_time(monkeypatch):
    _patch_proc(monkeypatch, _TURN_LINES)
    s = sessionmod.Session("s1")
    assert s.start_turn("first") is True
    assert s.start_turn("second") is False  # task not done yet
    await s._turn


@pytest.mark.asyncio
async def test_resume_after_first_turn(monkeypatch):
    captured = {"argvs": []}

    async def _fake_spawn(*argv, **kwargs):
        captured["argvs"].append(argv)
        return _FakeProc(_TURN_LINES)

    monkeypatch.setattr(sessionmod.asyncio, "create_subprocess_exec", _fake_spawn)
    s = sessionmod.Session("s1")
    s.start_turn("first"); await s._turn
    s.start_turn("second"); await s._turn
    assert "--session-id" in captured["argvs"][0]
    assert "--resume" in captured["argvs"][1]


# --------------------------------------------------------------------------- #
# SessionManager.
# --------------------------------------------------------------------------- #
def test_manager_create_get():
    m = sessionmod.SessionManager()
    s = m.create()
    assert m.get(s.id) is s
    assert m.get("nope") is None
    assert m.get_or_create(s.id) is s
    assert m.get_or_create(None).id != s.id


# --------------------------------------------------------------------------- #
# PVE verb whitelist (unchanged security boundary).
# --------------------------------------------------------------------------- #
def test_allowed_verbs_match_host_script():
    assert pve.ALLOWED_VERBS == {"status", "forensics", "reset", "stop", "start", "cycle"}
    assert pve.MUTATING_VERBS == {"reset", "stop", "start", "cycle"}


@pytest.mark.parametrize("bad", ["rm -rf /", "status; reboot", "status 103", "", "STATUS"])
@pytest.mark.asyncio
async def test_run_verb_rejects_non_whitelisted_without_ssh(bad, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("ssh must not run for a rejected verb")
    monkeypatch.setattr(pve.asyncio, "create_subprocess_exec", _boom)
    result = await pve.run_verb(bad)
    assert result["rejected"] is True


# --------------------------------------------------------------------------- #
# translate_event (pure).
# --------------------------------------------------------------------------- #
def test_translate_init_and_noise_and_blocks():
    assert agent_session.translate_event(
        {"type": "system", "subtype": "init", "session_id": "abc"}
    ) == {"kind": "session", "session_id": "abc"}
    assert agent_session.translate_event({"type": "system", "subtype": "hook_started"}) is None
    assert agent_session.translate_event(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
    ) == {"kind": "text", "text": "hi"}
    tool = agent_session.translate_event(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "df -h"}}]}}
    )
    assert tool["kind"] == "tool" and tool["input"]["command"] == "df -h"


# --------------------------------------------------------------------------- #
# Routes + auth.
# --------------------------------------------------------------------------- #
client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}


def test_health_no_auth():
    assert client.get("/health").json()["service"] == "claude-breakglass"


def test_api_requires_auth():
    assert client.post("/api/session").status_code == 401
    assert client.get("/api/pve/verbs").status_code == 401
    assert client.post("/api/session/x/prompt", json={"prompt": "hi"}).status_code == 401


def test_session_create_and_unknown_session_404():
    r = client.post("/api/session", headers=AUTH)
    assert r.status_code == 200 and "session_id" in r.json()
    assert client.post("/api/session/nope/prompt", headers=AUTH, json={"prompt": "x"}).status_code == 404
    assert client.post("/api/session/nope/cancel", headers=AUTH).status_code == 404


def test_prompt_starts_turn(monkeypatch):
    monkeypatch.setattr(sessionmod.Session, "start_turn", lambda self, *a, **k: True)
    sid = client.post("/api/session", headers=AUTH).json()["session_id"]
    r = client.post(f"/api/session/{sid}/prompt", headers=AUTH, json={"prompt": "diagnose"})
    assert r.status_code == 200 and r.json()["status"] == "started"


def test_prompt_409_when_turn_active(monkeypatch):
    monkeypatch.setattr(sessionmod.Session, "start_turn", lambda self, *a, **k: False)
    sid = client.post("/api/session", headers=AUTH).json()["session_id"]
    r = client.post(f"/api/session/{sid}/prompt", headers=AUTH, json={"prompt": "x"})
    assert r.status_code == 409


def test_pve_verbs_listing_and_unknown_rejected():
    assert set(client.get("/api/pve/verbs", headers=AUTH).json()["verbs"]) == pve.ALLOWED_VERBS
    assert client.post("/api/pve/destroy", headers=AUTH).status_code == 400
