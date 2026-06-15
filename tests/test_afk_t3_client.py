"""Tests for ``app.afk.t3_client`` — the in-cluster T3 dispatch/snapshot adapter.

Everything runs against an in-memory FAKE HTTP transport; no test touches a real
T3 server. These assertions pin the **real** orchestration wire contract
(reverse-engineered from T3 v0.0.27 and verified live against t3-afk on
2026-06-15) — deliberately strict, because the previous version of this adapter
passed a laxer fake while 400-ing the real server. The fake therefore *rejects*
a command without a ``type`` discriminator, so a regression to the old
``{"command": "..."}` shape fails loudly here.

Pinned facts:
  * the dispatch body is a BARE command keyed by ``type`` (not ``command``);
  * the CLIENT mints ``threadId``/``commandId``/``messageId`` + ``createdAt``;
    ``dispatch`` returns the id it generated (the server replies ``{sequence}``);
  * a thread lives in a project, so ``dispatch`` ensures the repo's project
    (snapshot GET → ``project.create`` iff absent) before ``thread.create``;
  * ``ISSUE_IMPLEMENTER_PREAMBLE`` is prepended to the opening turn's text;
  * ``send_turn`` posts a follow-up turn (no preamble) on an existing thread;
  * every request carries ``Authorization: Bearer <token>``, re-read per call.
"""
import pytest

from app.afk import t3_client
from app.afk.issue_implementer_prompt import ISSUE_IMPLEMENTER_PREAMBLE

_MODEL = "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# Fake HTTP transport — httpx-shaped, but it ENFORCES the command envelope so a
# malformed command (the old bug) raises instead of silently passing.
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Records each POST/GET; GETs replay staged snapshots (default: no projects,
    so ``dispatch`` creates one). POST bodies are validated as real commands."""

    def __init__(self, get_responses: list[dict] | None = None) -> None:
        self.get_responses = list(get_responses or [])
        self.posts: list[dict] = []
        self.gets: list[dict] = []

    def post(self, url: str, json: dict, headers: dict) -> FakeResponse:
        assert isinstance(json.get("type"), str) and json["type"], (
            f"command must carry a non-empty `type` discriminator, got {json!r}"
        )
        self.posts.append({"url": url, "json": json, "headers": headers})
        return FakeResponse({"sequence": len(self.posts)})  # the real server reply

    def get(self, url: str, headers: dict) -> FakeResponse:
        self.gets.append({"url": url, "headers": headers})
        body = self.get_responses.pop(0) if self.get_responses else {"projects": []}
        return FakeResponse(body)

    # Convenience views over recorded POSTs, keyed by command type.
    def commands(self, type_: str) -> list[dict]:
        return [c["json"] for c in self.posts if c["json"]["type"] == type_]


def _ids():
    """Deterministic id factory: id-1, id-2, … so tests can reason about minting."""
    n = {"i": 0}

    def f() -> str:
        n["i"] += 1
        return f"id-{n['i']}"

    return f


def _resolver(repo: str) -> t3_client.ProjectRef:
    """Predictable repo -> project mapping for assertions."""
    return t3_client.ProjectRef(f"proj-{repo}", f"/data/{repo}", repo)


def _client(http: FakeHttp, *, base_url="http://t3-afk:8080", token="tok-1", **kw):
    return t3_client.T3Client(
        base_url=base_url,
        http=http,
        bearer_provider=lambda: token,
        project_resolver=_resolver,
        id_factory=kw.pop("id_factory", _ids()),
        clock=kw.pop("clock", lambda: "2026-06-15T00:00:00+00:00"),
        model=_MODEL,
    )


def _dispatch(http: FakeHttp, *, repo="infra", issue=42, prompt="Do the thing.", **kw):
    return _client(http, **kw).dispatch(repo=repo, issue=issue, prompt=prompt)


# --------------------------------------------------------------------------- #
# dispatch — ensure-project, then create, then turn.
# --------------------------------------------------------------------------- #
def test_dispatch_ensures_project_then_creates_thread_then_turn_when_project_absent():
    http = FakeHttp(get_responses=[{"projects": []}])
    _dispatch(http)
    # one snapshot GET (the existence check) + three POSTs in order.
    assert len(http.gets) == 1
    types = [c["json"]["type"] for c in http.posts]
    assert types == ["project.create", "thread.create", "thread.turn.start"]
    for call in http.posts:
        assert call["url"] == "http://t3-afk:8080/api/orchestration/dispatch"


def test_dispatch_skips_project_create_when_project_already_exists():
    http = FakeHttp(get_responses=[{"projects": [{"id": "proj-infra"}]}])
    _dispatch(http, repo="infra")
    types = [c["json"]["type"] for c in http.posts]
    assert types == ["thread.create", "thread.turn.start"]  # idempotent: no re-create


def test_dispatch_uses_type_discriminator_not_command_string():
    # Regression guard for the original bug: discriminator is `type`, and there is
    # no legacy top-level `command` string key on any command.
    http = FakeHttp()
    _dispatch(http)
    for c in http.posts:
        assert "type" in c["json"]
        assert not isinstance(c["json"].get("command"), str)


# --------------------------------------------------------------------------- #
# dispatch — thread.create real field set.
# --------------------------------------------------------------------------- #
def test_thread_create_carries_real_required_fields():
    http = FakeHttp()
    _dispatch(http, repo="infra")
    create = http.commands("thread.create")[0]
    assert create["projectId"] == "proj-infra"
    assert create["modelSelection"] == {"instanceId": "claudeAgent", "model": _MODEL}
    assert create["runtimeMode"] == "full-access"
    assert create["interactionMode"] == "default"
    # NullOr fields are present (not omitted) — the schema requires the keys.
    assert create["branch"] is None
    assert create["worktreePath"] is None
    # client-minted identity + timestamp.
    assert isinstance(create["commandId"], str) and create["commandId"]
    assert isinstance(create["threadId"], str) and create["threadId"]
    assert create["createdAt"] == "2026-06-15T00:00:00+00:00"


def test_dispatch_returns_client_minted_thread_id_not_a_server_value():
    http = FakeHttp()
    returned = _dispatch(http)
    create = http.commands("thread.create")[0]
    turn = http.commands("thread.turn.start")[0]
    # The returned id is the one WE put on thread.create (server only sends {sequence}).
    assert returned == create["threadId"] == turn["threadId"]


# --------------------------------------------------------------------------- #
# dispatch — thread.turn.start real message shape + preamble.
# --------------------------------------------------------------------------- #
def test_turn_message_has_real_shape_and_prepends_preamble():
    http = FakeHttp()
    _dispatch(http, prompt="Implement issue 42 body here.")
    turn = http.commands("thread.turn.start")[0]
    msg = turn["message"]
    assert msg["role"] == "user"
    assert isinstance(msg["messageId"], str) and msg["messageId"]
    assert msg["attachments"] == []
    assert msg["text"] == ISSUE_IMPLEMENTER_PREAMBLE + "Implement issue 42 body here."
    assert turn["runtimeMode"] == "full-access"
    assert turn["interactionMode"] == "default"


def test_preamble_only_on_turn_not_on_create():
    http = FakeHttp()
    _dispatch(http)
    assert "message" not in http.commands("thread.create")[0]


# --------------------------------------------------------------------------- #
# send_turn — follow-up turn on an existing thread (multi-turn), no preamble.
# --------------------------------------------------------------------------- #
def test_send_turn_posts_single_turn_to_existing_thread_without_preamble():
    http = FakeHttp()
    _client(http).send_turn("thread-xyz", "Just this follow-up.")
    assert [c["json"]["type"] for c in http.posts] == ["thread.turn.start"]
    turn = http.commands("thread.turn.start")[0]
    assert turn["threadId"] == "thread-xyz"
    assert turn["message"]["text"] == "Just this follow-up."  # verbatim, no preamble
    assert http.gets == []  # no project work for a follow-up


# --------------------------------------------------------------------------- #
# Auth — bearer on every request, re-read per call.
# --------------------------------------------------------------------------- #
def test_every_request_sends_bearer():
    http = FakeHttp()
    _dispatch(http, token="secret-token")
    for call in http.posts:
        assert call["headers"]["Authorization"] == "Bearer secret-token"
    for call in http.gets:
        assert call["headers"]["Authorization"] == "Bearer secret-token"


def test_bearer_is_reread_per_request_so_rotation_is_honoured():
    tokens = iter(["tok-A", "tok-B", "tok-C", "tok-D", "tok-E"])
    http = FakeHttp()
    client = t3_client.T3Client(
        base_url="http://t3-afk:8080",
        http=http,
        bearer_provider=lambda: next(tokens),
        project_resolver=_resolver,
        id_factory=_ids(),
        clock=lambda: "t",
    )
    client.dispatch(repo="infra", issue=1, prompt="x")
    # GET(ensure) then POST(project.create) then POST(create) then POST(turn) —
    # each pulled a fresh token in call order.
    assert http.gets[0]["headers"]["Authorization"] == "Bearer tok-A"
    assert http.posts[0]["headers"]["Authorization"] == "Bearer tok-B"
    assert http.posts[1]["headers"]["Authorization"] == "Bearer tok-C"
    assert http.posts[2]["headers"]["Authorization"] == "Bearer tok-D"


# --------------------------------------------------------------------------- #
# snapshot — GET + parse.
# --------------------------------------------------------------------------- #
def test_snapshot_gets_endpoint_and_returns_parsed_body():
    fleet = {"threads": [{"id": "t1", "latestTurn": {"state": "running"}}], "projects": []}
    http = FakeHttp(get_responses=[fleet])
    result = _client(http).snapshot()
    assert result == fleet
    assert http.gets[0]["url"] == "http://t3-afk:8080/api/orchestration/snapshot"
    assert http.posts == []


# --------------------------------------------------------------------------- #
# base_url normalisation + error surfacing.
# --------------------------------------------------------------------------- #
def test_trailing_slash_in_base_url_is_normalised():
    http = FakeHttp()
    client = _client(http, base_url="http://t3-afk:8080/")
    client.dispatch(repo="infra", issue=1, prompt="x")
    assert http.posts[0]["url"] == "http://t3-afk:8080/api/orchestration/dispatch"
    assert http.gets[0]["url"] == "http://t3-afk:8080/api/orchestration/snapshot"


def test_dispatch_raises_and_short_circuits_when_a_post_errors():
    class ErroringHttp(FakeHttp):
        def post(self, url: str, json: dict, headers: dict) -> FakeResponse:
            super().post(url, json, headers)  # validates + records
            return FakeResponse({}, status_code=500)

    http = ErroringHttp(get_responses=[{"projects": [{"id": "proj-infra"}]}])
    with pytest.raises(RuntimeError):
        _dispatch(http, repo="infra")
    # Project already existed, so the FIRST post is thread.create — and it failed,
    # so thread.turn.start never fired.
    assert [c["json"]["type"] for c in http.posts] == ["thread.create"]
