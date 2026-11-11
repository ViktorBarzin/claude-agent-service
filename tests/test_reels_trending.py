"""The Reels trending-songs route: ``POST /v1/reels/trending-songs``.

The Reels worker asks for this week's songs trending on Instagram Reels and
TikTok. The worker downloads and parses untrusted media, so it never holds the
``/execute`` token: this route has its own token, runs exactly one agent with
web search only, no settings files, no MCP, and an environment holding nothing
but the Claude credential. These tests pin that down, plus the contract the
worker relies on.
"""
import asyncio
import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import reels_trending
from app.main import app

TOKEN = "reels-token"

AGENT_MD = """\
---
name: reels-trending
description: Trending songs.
model: sonnet
tools: WebSearch, WebFetch
---

You find trending songs.
"""

SONGS = [
    {"title": "Housewife", "artist": "Tate McRae", "why": "4 lists", "source": "https://x"},
    {"title": "Nicole Kidman", "artist": "ADELA", "why": "3 lists", "source": "https://y"},
    {"title": "Dame Hombro", "artist": "Bulin 47", "why": "2 lists", "source": "https://z"},
]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    reels_trending.semaphore = asyncio.Semaphore(1)
    reels_trending.cache.clear()
    agent_file = tmp_path / "reels-trending.md"
    agent_file.write_text(AGENT_MD)
    monkeypatch.setenv(reels_trending.ENV_AGENT_FILE, str(agent_file))
    monkeypatch.setenv(reels_trending.ENV_TOKEN, TOKEN)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cli_output(songs=SONGS, is_error=False) -> str:
    return json.dumps({"type": "result", "is_error": is_error, "result": "",
                       "structured_output": {"songs": songs}})


def test_an_unset_token_refuses_with_503(client, monkeypatch):
    monkeypatch.delenv(reels_trending.ENV_TOKEN)
    r = client.post("/v1/reels/trending-songs", json={"today": "2026-11-11"}, headers=_auth(""))
    assert r.status_code == 503


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}])
def test_a_wrong_or_missing_token_is_401(client, headers):
    with patch.object(reels_trending, "run_agent") as run:
        r = client.post("/v1/reels/trending-songs", json={"today": "2026-11-11"}, headers=headers)
    assert r.status_code == 401
    run.assert_not_called()


def test_the_execute_token_does_not_open_the_route(client):
    r = client.post("/v1/reels/trending-songs", json={"today": "2026-11-11"},
                    headers=_auth(os.environ["API_BEARER_TOKEN"]))
    assert r.status_code == 401


@pytest.mark.parametrize("today", ["2026-13-01", "yesterday", "2026-11-11; rm -rf /", ""])
def test_only_a_real_date_is_accepted(client, today):
    with patch.object(reels_trending, "run_agent") as run:
        r = client.post("/v1/reels/trending-songs", json={"today": today}, headers=_auth())
    assert r.status_code == 400
    run.assert_not_called()


def test_the_songs_come_back_from_the_structured_output(client):
    async def fake(_today):
        return reels_trending.parse_songs(_cli_output())

    with patch.object(reels_trending, "run_agent", side_effect=fake):
        r = client.post("/v1/reels/trending-songs", json={"today": "2026-11-11"}, headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"songs": SONGS}


def test_a_second_call_the_same_day_is_served_from_the_cache(client):
    calls = 0

    async def fake(_today):
        nonlocal calls
        calls += 1
        return SONGS

    with patch.object(reels_trending, "run_agent", side_effect=fake):
        for _ in range(2):
            assert client.post("/v1/reels/trending-songs", json={"today": "2026-11-11"},
                               headers=_auth()).status_code == 200
    assert calls == 1


def test_a_failed_run_is_502_and_not_cached(client):
    async def boom(_today):
        raise RuntimeError("agent failed")

    with patch.object(reels_trending, "run_agent", side_effect=boom):
        r = client.post("/v1/reels/trending-songs", json={"today": "2026-11-11"}, headers=_auth())
    assert r.status_code == 502
    assert reels_trending.cache == {}


def test_parse_songs_rejects_errors_and_too_few_songs():
    with pytest.raises(RuntimeError):
        reels_trending.parse_songs(_cli_output(is_error=True))
    with pytest.raises(RuntimeError):
        reels_trending.parse_songs(_cli_output(songs=SONGS[:2]))
    with pytest.raises(RuntimeError):
        reels_trending.parse_songs("not json")


def test_parse_songs_drops_entries_without_title_or_artist():
    songs = SONGS + [{"title": "", "artist": "x", "why": "", "source": ""}, {"title": "y"}]
    assert reels_trending.parse_songs(_cli_output(songs=songs)) == SONGS


def test_the_run_has_web_search_only_and_no_settings_or_bypass():
    cmd = reels_trending.argv(reels_trending.load_agent(), None)
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    assert schema["required"] == ["songs"]


def test_an_agent_file_with_other_tools_is_refused(monkeypatch, tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text(AGENT_MD.replace("WebSearch, WebFetch", "WebSearch, WebFetch, Bash"))
    monkeypatch.setenv(reels_trending.ENV_AGENT_FILE, str(bad))
    with pytest.raises(ValueError):
        reels_trending.argv(reels_trending.load_agent(), None)


def test_the_environment_holds_only_the_claude_credential():
    env = {"PATH": "/bin", "HOME": "/h", "LANG": "C", "CLAUDE_CODE_OAUTH_TOKEN": "c",
           "API_BEARER_TOKEN": "x", "FIXER_FORGEJO_TOKEN": "y", "VAULT_TOKEN": "z"}
    assert reels_trending.scrubbed_env(env) == {"PATH": "/bin", "HOME": "/h", "LANG": "C",
                                                "CLAUDE_CODE_OAUTH_TOKEN": "c"}


def test_the_prompt_names_the_date_and_only_the_date():
    p = reels_trending.prompt("2026-11-11")
    assert "2026-11-11" in p
    assert "{" not in p.split("2026-11-11")[0][-5:]


def test_parse_songs_reads_json_from_the_text_when_structured_output_is_missing():
    """With --agent, the CLI may not apply --json-schema; the agent then answers in text."""
    text = "Here are the songs:\n```json\n" + json.dumps({"songs": SONGS}) + "\n```\nDone."
    out = json.dumps({"type": "result", "is_error": False, "result": text, "structured_output": None})
    assert reels_trending.parse_songs(out) == SONGS


def test_parse_songs_refuses_prose_without_json():
    out = json.dumps({"type": "result", "is_error": False, "result": "1. Tate McRae - Housewife", "structured_output": None})
    with pytest.raises(RuntimeError):
        reels_trending.parse_songs(out)
