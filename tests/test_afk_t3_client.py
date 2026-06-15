"""Tests for ``app.afk.t3_client`` — the in-cluster T3 dispatch/snapshot adapter.

Everything here runs against an in-memory FAKE HTTP transport (``FakeHttp``);
no test touches a real T3 server, GitHub/Forgejo, or the cluster. The fake
records every request and replays staged responses, so the assertions pin the
wire contract the control plane depends on:

  * ``dispatch`` issues exactly TWO POSTs to ``/api/orchestration/dispatch`` —
    ``thread.create`` then ``thread.turn.start`` — carrying
    ``modelSelection.instanceId == "claudeAgent"`` and ``runtimeMode ==
    "full-access"``, with ``ISSUE_IMPLEMENTER_PREAMBLE`` PREPENDED to
    ``message.text`` and the thread id from the first response threaded into the
    second.
  * each request carries the ``Authorization: Bearer <token>`` header from the
    injected bearer provider (re-read per call, so token refresh is honoured).
  * ``snapshot`` GETs ``/api/orchestration/snapshot`` and returns the parsed body.
"""
import pytest

from app.afk import t3_client
from app.afk.issue_implementer_prompt import ISSUE_IMPLEMENTER_PREAMBLE


# --------------------------------------------------------------------------- #
# Fake HTTP transport — httpx-shaped (``post``/``get`` → response with
# ``.json()`` + ``.raise_for_status()``), so the real client can hand the
# adapter a plain ``httpx.Client`` while tests hand it this recorder.
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
    """Records each POST/GET and replays queued responses in order.

    ``post`` pops from ``post_responses`` (FIFO); ``get`` pops from
    ``get_responses``. Each recorded call captures the url, json body, and
    headers so tests can assert the two-command dispatch shape and the bearer.
    """

    def __init__(
        self,
        post_responses: list[dict] | None = None,
        get_responses: list[dict] | None = None,
    ) -> None:
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.posts: list[dict] = []
        self.gets: list[dict] = []

    def post(self, url: str, json: dict, headers: dict) -> FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers})
        if not self.post_responses:
            raise AssertionError("unexpected POST — no response staged")
        return FakeResponse(self.post_responses.pop(0))

    def get(self, url: str, headers: dict) -> FakeResponse:
        self.gets.append({"url": url, "headers": headers})
        if not self.get_responses:
            raise AssertionError("unexpected GET — no response staged")
        return FakeResponse(self.get_responses.pop(0))


# Two thread.create / thread.turn.start replies the happy-path dispatch needs.
_CREATE_REPLY = {"threadId": "thread-abc"}
_TURN_REPLY = {"ok": True}


def _client(http: FakeHttp, *, base_url: str = "http://t3-afk:8080", token: str = "tok-1"):
    return t3_client.T3Client(
        base_url=base_url,
        http=http,
        bearer_provider=lambda: token,
    )


def _dispatch(http: FakeHttp, **kw) -> str:
    repo = kw.pop("repo", "infra")
    issue = kw.pop("issue", 42)
    prompt = kw.pop("prompt", "Do the thing.")
    return _client(http, **kw).dispatch(repo=repo, issue=issue, prompt=prompt)


# --------------------------------------------------------------------------- #
# dispatch — the two-POST shape.
# --------------------------------------------------------------------------- #
def test_dispatch_issues_exactly_two_posts_to_dispatch_endpoint():
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    _dispatch(http)
    assert len(http.posts) == 2
    assert http.gets == []
    for call in http.posts:
        assert call["url"] == "http://t3-afk:8080/api/orchestration/dispatch"


def test_dispatch_first_command_is_thread_create():
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    _dispatch(http)
    assert http.posts[0]["json"]["command"] == "thread.create"


def test_dispatch_second_command_is_thread_turn_start():
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    _dispatch(http)
    assert http.posts[1]["json"]["command"] == "thread.turn.start"


def test_dispatch_returns_thread_id_from_create_response():
    http = FakeHttp(post_responses=[{"threadId": "thread-xyz"}, _TURN_REPLY])
    assert _dispatch(http) == "thread-xyz"


def test_dispatch_threads_created_id_into_turn_start():
    http = FakeHttp(post_responses=[{"threadId": "thread-xyz"}, _TURN_REPLY])
    _dispatch(http)
    # The second command must target the thread the first call created.
    assert http.posts[1]["json"]["threadId"] == "thread-xyz"


# --------------------------------------------------------------------------- #
# dispatch — model selection / runtime envelope (the pilot-baked constants).
# --------------------------------------------------------------------------- #
def test_dispatch_uses_claude_agent_instance_and_full_access_runtime():
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    _dispatch(http)
    create_body = http.posts[0]["json"]
    assert create_body["modelSelection"]["instanceId"] == "claudeAgent"
    assert create_body["runtimeMode"] == "full-access"


def test_dispatch_create_carries_repo_and_issue():
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    _dispatch(http, repo="claude-agent-service", issue=7)
    create_body = http.posts[0]["json"]
    assert create_body["repo"] == "claude-agent-service"
    assert create_body["issue"] == 7


# --------------------------------------------------------------------------- #
# dispatch — the preamble PREPEND (behaviour injection).
# --------------------------------------------------------------------------- #
def test_dispatch_prepends_issue_implementer_preamble_to_message_text():
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    _dispatch(http, prompt="Implement issue 42 body here.")
    text = http.posts[1]["json"]["message"]["text"]
    assert text == ISSUE_IMPLEMENTER_PREAMBLE + "Implement issue 42 body here."


def test_dispatch_preamble_comes_strictly_before_the_prompt():
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    _dispatch(http, prompt="UNIQUE-PROMPT-MARKER")
    text = http.posts[1]["json"]["message"]["text"]
    assert text.startswith(ISSUE_IMPLEMENTER_PREAMBLE)
    assert text.index(ISSUE_IMPLEMENTER_PREAMBLE) < text.index("UNIQUE-PROMPT-MARKER")
    # The raw prompt is preserved verbatim after the preamble.
    assert text.endswith("UNIQUE-PROMPT-MARKER")


def test_dispatch_does_not_prepend_preamble_to_create_command():
    # The preamble belongs only on the turn message, not the thread.create call.
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    _dispatch(http)
    assert "message" not in http.posts[0]["json"]


# --------------------------------------------------------------------------- #
# Auth — bearer header, read from the injected provider each call.
# --------------------------------------------------------------------------- #
def test_dispatch_sends_bearer_on_both_posts():
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    _dispatch(http, token="secret-token")
    for call in http.posts:
        assert call["headers"]["Authorization"] == "Bearer secret-token"


def test_bearer_provider_is_called_per_request_so_refresh_is_honoured():
    # A rotating provider proves the token isn't captured once at construction
    # (T3's orchestration token expires hourly and must be re-read).
    tokens = iter(["tok-A", "tok-B", "tok-C"])
    http = FakeHttp(post_responses=[_CREATE_REPLY, _TURN_REPLY])
    client = t3_client.T3Client(
        base_url="http://t3-afk:8080",
        http=http,
        bearer_provider=lambda: next(tokens),
    )
    client.dispatch(repo="infra", issue=1, prompt="x")
    assert http.posts[0]["headers"]["Authorization"] == "Bearer tok-A"
    assert http.posts[1]["headers"]["Authorization"] == "Bearer tok-B"


# --------------------------------------------------------------------------- #
# snapshot — GET + parse.
# --------------------------------------------------------------------------- #
def test_snapshot_gets_snapshot_endpoint_and_returns_parsed_body():
    fleet = {"threads": [{"id": "thread-abc", "status": "running"}]}
    http = FakeHttp(get_responses=[fleet])
    result = _client(http).snapshot()
    assert result == fleet
    assert len(http.gets) == 1
    assert http.gets[0]["url"] == "http://t3-afk:8080/api/orchestration/snapshot"
    assert http.posts == []


def test_snapshot_sends_bearer():
    http = FakeHttp(get_responses=[{"threads": []}])
    _client(http, token="snap-token").snapshot()
    assert http.gets[0]["headers"]["Authorization"] == "Bearer snap-token"


# --------------------------------------------------------------------------- #
# base_url handling — a trailing slash must not produce a double slash.
# --------------------------------------------------------------------------- #
def test_trailing_slash_in_base_url_is_normalised():
    http = FakeHttp(
        post_responses=[_CREATE_REPLY, _TURN_REPLY],
        get_responses=[{"threads": []}],
    )
    client = _client(http, base_url="http://t3-afk:8080/")
    client.dispatch(repo="infra", issue=1, prompt="x")
    client.snapshot()
    assert http.posts[0]["url"] == "http://t3-afk:8080/api/orchestration/dispatch"
    assert http.gets[0]["url"] == "http://t3-afk:8080/api/orchestration/snapshot"


# --------------------------------------------------------------------------- #
# Error surfacing — a non-2xx response must raise, not be swallowed.
# --------------------------------------------------------------------------- #
def test_dispatch_raises_when_a_post_returns_an_error_status():
    class ErroringHttp(FakeHttp):
        def post(self, url: str, json: dict, headers: dict) -> FakeResponse:
            self.posts.append({"url": url, "json": json, "headers": headers})
            return FakeResponse({}, status_code=500)

    http = ErroringHttp()
    with pytest.raises(RuntimeError):
        _dispatch(http)
    # It failed on the FIRST call — never blindly fired thread.turn.start after
    # a failed thread.create.
    assert len(http.posts) == 1
