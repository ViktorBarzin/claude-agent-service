"""The Date Night scout route: ``POST /v1/datenight/scout``.

The Date Night app asks once a week for new date ideas in London. The agent
reads public listing pages, which are untrusted, so the route has its own
token and runs exactly one agent with web search only, no settings files, no
MCP and an environment holding nothing but the Claude credential. These tests
pin that down, plus the contract the app relies on.
"""
import asyncio
import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import datenight_scout
from app.main import app

TOKEN = "scout-token"

AGENT_MD = """\
---
name: date-scout
description: Date ideas.
model: sonnet
tools: WebSearch, WebFetch
---

You find date ideas.
"""

IDEA = {
    "title": "Candlelit flamenco",
    "venue": "Sadler's Wells",
    "address": "Rosebery Ave",
    "area": "Clerkenwell",
    "postcode": "EC1R 4TN",
    "category": "shows",
    "activity_kind": "flamenco show",
    "price_gbp_for_two": 70,
    "url": "https://example.com/a",
    "booking_url": "https://example.com/b",
    "description": "An evening of flamenco.",
    "why": "Live music, close to home.",
    "ends_on": None,
}
IDEAS = [IDEA, {**IDEA, "title": "Pottery night", "venue": "Turning Earth", "category": "classes"},
         {**IDEA, "title": "Supper club", "venue": "Kitchen", "category": "food"}]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    datenight_scout.semaphore = asyncio.Semaphore(1)
    datenight_scout.cache.clear()
    agent_file = tmp_path / "date-scout.md"
    agent_file.write_text(AGENT_MD)
    monkeypatch.setenv(datenight_scout.ENV_AGENT_FILE, str(agent_file))
    monkeypatch.setenv(datenight_scout.ENV_TOKEN, TOKEN)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cli_output(ideas=IDEAS, is_error=False) -> str:
    return json.dumps({"type": "result", "is_error": is_error, "result": "",
                       "structured_output": {"ideas": ideas}})


def test_an_unset_token_refuses_with_503(client, monkeypatch):
    monkeypatch.delenv(datenight_scout.ENV_TOKEN)
    r = client.post("/v1/datenight/scout", json={"today": "2026-11-22"}, headers=_auth(""))
    assert r.status_code == 503


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}])
def test_a_wrong_or_missing_token_is_401(client, headers):
    with patch.object(datenight_scout, "run_agent") as run:
        r = client.post("/v1/datenight/scout", json={"today": "2026-11-22"}, headers=headers)
    assert r.status_code == 401
    run.assert_not_called()


def test_the_execute_token_and_the_reels_token_do_not_open_the_route(client, monkeypatch):
    monkeypatch.setenv("REELS_TRENDING_TOKEN", "reels")
    for token in (os.environ["API_BEARER_TOKEN"], "reels"):
        r = client.post("/v1/datenight/scout", json={"today": "2026-11-22"}, headers=_auth(token))
        assert r.status_code == 401


@pytest.mark.parametrize("today", ["2026-13-01", "tomorrow", "2026-11-22; rm -rf /", ""])
def test_only_a_real_date_is_accepted(client, today):
    with patch.object(datenight_scout, "run_agent") as run:
        r = client.post("/v1/datenight/scout", json={"today": today}, headers=_auth())
    assert r.status_code == 400
    run.assert_not_called()


def test_the_ideas_come_back_from_the_structured_output(client):
    async def fake(_today, *_):
        return datenight_scout.parse_ideas(_cli_output())

    with patch.object(datenight_scout, "run_agent", side_effect=fake):
        r = client.post("/v1/datenight/scout", json={"today": "2026-11-22"}, headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"ideas": IDEAS}


def test_a_second_call_the_same_day_is_served_from_the_cache(client):
    calls = 0

    async def fake(_today, *_):
        nonlocal calls
        calls += 1
        return IDEAS

    with patch.object(datenight_scout, "run_agent", side_effect=fake):
        for _ in range(2):
            assert client.post("/v1/datenight/scout", json={"today": "2026-11-22"},
                               headers=_auth()).status_code == 200
    assert calls == 1


def test_a_failed_run_is_502_and_not_cached(client):
    async def boom(_today, *_):
        raise RuntimeError("agent failed")

    with patch.object(datenight_scout, "run_agent", side_effect=boom):
        r = client.post("/v1/datenight/scout", json={"today": "2026-11-22"}, headers=_auth())
    assert r.status_code == 502
    assert datenight_scout.cache == {}


def test_parse_ideas_rejects_errors_and_too_few_ideas():
    with pytest.raises(RuntimeError):
        datenight_scout.parse_ideas(_cli_output(is_error=True))
    with pytest.raises(RuntimeError):
        datenight_scout.parse_ideas(_cli_output(ideas=IDEAS[:2]))
    with pytest.raises(RuntimeError):
        datenight_scout.parse_ideas("not json")


def test_parse_ideas_drops_entries_without_title_venue_or_category():
    ideas = IDEAS + [{"title": "", "venue": "x", "category": "food"}, {"title": "y", "venue": "z"}, "text"]
    assert datenight_scout.parse_ideas(_cli_output(ideas=ideas)) == IDEAS


def test_parse_ideas_reads_json_from_the_text_when_structured_output_is_missing():
    text = "Here:\n```json\n" + json.dumps({"ideas": IDEAS}) + "\n```"
    out = json.dumps({"type": "result", "is_error": False, "result": text, "structured_output": None})
    assert datenight_scout.parse_ideas(out) == IDEAS


def test_the_run_has_web_search_only_and_no_settings_or_bypass():
    cmd = datenight_scout.argv(datenight_scout.load_agent(), None)
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    assert schema["required"] == ["ideas"]


def test_an_agent_file_with_other_tools_is_refused(monkeypatch, tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text(AGENT_MD.replace("WebSearch, WebFetch", "WebSearch, WebFetch, Bash"))
    monkeypatch.setenv(datenight_scout.ENV_AGENT_FILE, str(bad))
    with pytest.raises(ValueError):
        datenight_scout.argv(datenight_scout.load_agent(), None)


def test_the_environment_holds_only_the_claude_credential():
    env = {"PATH": "/bin", "HOME": "/h", "LANG": "C", "CLAUDE_CODE_OAUTH_TOKEN": "c",
           "API_BEARER_TOKEN": "x", "DATENIGHT_SCOUT_TOKEN": "y", "VAULT_TOKEN": "z"}
    assert datenight_scout.scrubbed_env(env) == {"PATH": "/bin", "HOME": "/h", "LANG": "C",
                                                 "CLAUDE_CODE_OAUTH_TOKEN": "c"}


def test_the_prompt_names_the_date_and_only_the_date():
    p = datenight_scout.prompt("2026-11-22", [], False)
    assert "2026-11-22" in p


def test_the_shipped_agent_file_parses_with_exactly_the_two_tools():
    from app.intake import parse_agent
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "agents", "date-scout.md")) as fh:
        agent = parse_agent(fh.read())
    assert tuple(agent["tools"]) == datenight_scout.TOOLS


def test_the_prompt_lists_the_venues_to_skip_as_data():
    p = datenight_scout.prompt("2026-11-22", ["Kiln", "Sadler's Wells"], False)
    assert "Kiln" in p and "Sadler's Wells" in p
    assert "data" in p.lower()


def test_a_refill_asks_for_other_sources_and_neighbourhoods():
    assert "refill" in datenight_scout.prompt("2026-11-22", [], True).lower()
    assert "refill" not in datenight_scout.prompt("2026-11-22", [], False).lower()


def test_a_refill_on_the_same_day_is_a_new_run_but_cached_itself(client):
    calls = []

    async def fake(today, avoid, refill):
        calls.append((today, refill))
        return IDEAS

    with patch.object(datenight_scout, "run_agent", side_effect=fake):
        client.post("/v1/datenight/scout", json={"today": "2026-11-22"}, headers=_auth())
        client.post("/v1/datenight/scout", json={"today": "2026-11-22", "refill": True}, headers=_auth())
        client.post("/v1/datenight/scout", json={"today": "2026-11-22", "refill": True}, headers=_auth())
    assert calls == [("2026-11-22", False), ("2026-11-22", True)]


def test_the_avoid_list_is_capped_and_cleaned(client):
    seen = {}

    async def fake(today, avoid, refill):
        seen["avoid"] = avoid
        return IDEAS

    avoid = ["  Kiln  ", "", "x" * 500] + [f"Venue {i}" for i in range(500)]
    with patch.object(datenight_scout, "run_agent", side_effect=fake):
        r = client.post("/v1/datenight/scout", json={"today": "2026-11-22", "avoid": avoid}, headers=_auth())
    assert r.status_code == 200
    assert seen["avoid"][0] == "Kiln"
    assert len(seen["avoid"]) <= datenight_scout.MAX_AVOID
    assert all(0 < len(v) <= 120 for v in seen["avoid"])
