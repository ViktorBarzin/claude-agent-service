"""Adapter for the in-cluster T3 Code instance — the AFK executor + cockpit.

The control plane keeps the brain; T3 runs the agent. This module is the thin
wire between them: it turns "implement issue N of repo R with this prompt" into
the TWO HTTP commands T3's orchestration API needs, and reads the fleet
snapshot the watcher polls. It owns no AFK behaviour — the agent's standing
rules ride in as the ``ISSUE_IMPLEMENTER_PREAMBLE`` prepended to the turn
message, because T3's full-access ``claudeAgent`` runtime does NOT honour
``~/.claude/CLAUDE.md`` (see ``issue_implementer_prompt``).

Two operations, both against the dedicated in-cluster T3 pod:

  * ``dispatch(repo, issue, prompt) -> thread_id`` — POSTs ``thread.create``
    then ``thread.turn.start`` to ``/api/orchestration/dispatch``. The create
    command selects the ``claudeAgent`` instance in ``full-access`` runtime mode
    and returns a thread id; the turn command targets that thread and delivers
    ``ISSUE_IMPLEMENTER_PREAMBLE + prompt`` as ``message.text``. One dispatch =
    one worktree-isolated worker.
  * ``snapshot() -> dict`` — GETs ``/api/orchestration/snapshot``, the full fleet
    read-model. T3 has no outbound webhooks, so the watcher polls this for
    per-thread ``running``/``idle``/``error`` status.

The HTTP transport and the bearer provider are **injected** (constructor
args), so the production wiring hands in an ``httpx.Client`` plus a Vault-backed
token reader, while tests hand in an in-memory fake — nothing here ever opens a
socket on its own. The bearer is re-read from the provider on **every** request
because T3's ``orchestration:operate`` token expires hourly and is refreshed out
of band.
"""
from collections.abc import Callable
from typing import Protocol

from .issue_implementer_prompt import ISSUE_IMPLEMENTER_PREAMBLE

# Orchestration API paths, relative to the configured base URL.
_DISPATCH_PATH = "/api/orchestration/dispatch"
_SNAPSHOT_PATH = "/api/orchestration/snapshot"

# Pilot-baked dispatch envelope: which backend instance runs the thread and in
# which runtime mode. Constants (not config) — every AFK thread is identical.
_INSTANCE_ID = "claudeAgent"
_RUNTIME_MODE = "full-access"

# JSON shapes. Command bodies and the snapshot read-model are open string-keyed
# objects; ``object`` values keep us honest without a bare ``Any``.
type Json = dict[str, object]


class HttpResponse(Protocol):
    """The httpx-shaped response surface this adapter relies on.

    Both ``httpx.Response`` and the test fake satisfy it: ``raise_for_status``
    turns a non-2xx into an exception (so a failed ``thread.create`` aborts
    before ``thread.turn.start`` ever fires) and ``json`` parses the body.
    """

    def raise_for_status(self) -> object: ...

    def json(self) -> Json: ...


class HttpClient(Protocol):
    """Minimal injected transport: a JSON ``post`` and a ``get``, both taking
    explicit headers. Deliberately a strict subset of ``httpx.Client`` so the
    real client passes one straight through and tests pass a recorder."""

    def post(self, url: str, json: Json, headers: dict[str, str]) -> HttpResponse: ...

    def get(self, url: str, headers: dict[str, str]) -> HttpResponse: ...


class T3Client:
    """Dispatch/snapshot adapter for one in-cluster T3 instance.

    ``base_url`` is the T3 service root (a trailing slash is tolerated);
    ``http`` is the injected transport; ``bearer_provider`` returns the current
    ``orchestration:operate`` token, re-read per request for hourly rotation.
    """

    def __init__(
        self,
        base_url: str,
        http: HttpClient,
        bearer_provider: Callable[[], str],
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._bearer_provider = bearer_provider

    # ----------------------------------------------------------------- #
    # Public API (the ``t3_client.T3Client`` contract).
    # ----------------------------------------------------------------- #
    def dispatch(self, repo: str, issue: int, prompt: str) -> str:
        """Spawn one worker thread for ``issue`` of ``repo`` and return its id.

        Two POSTs to ``/api/orchestration/dispatch``: ``thread.create`` (selects
        the ``claudeAgent`` instance, ``full-access`` runtime) yields the thread
        id; ``thread.turn.start`` then delivers ``ISSUE_IMPLEMENTER_PREAMBLE +
        prompt`` to that thread. A failed create raises and short-circuits the
        turn (we never fire a turn at a thread that wasn't created).
        """
        create_resp = self._post(
            _DISPATCH_PATH,
            {
                "command": "thread.create",
                "repo": repo,
                "issue": issue,
                "modelSelection": {"instanceId": _INSTANCE_ID},
                "runtimeMode": _RUNTIME_MODE,
            },
        )
        thread_id = self._thread_id_of(create_resp.json())

        self._post(
            _DISPATCH_PATH,
            {
                "command": "thread.turn.start",
                "threadId": thread_id,
                "message": {"text": ISSUE_IMPLEMENTER_PREAMBLE + prompt},
            },
        )
        return thread_id

    def snapshot(self) -> Json:
        """Return the parsed fleet read-model from ``/api/orchestration/snapshot``."""
        return self._get(_SNAPSHOT_PATH).json()

    # ----------------------------------------------------------------- #
    # Internals.
    # ----------------------------------------------------------------- #
    def _post(self, path: str, body: Json) -> HttpResponse:
        resp = self._http.post(self._url(path), json=body, headers=self._headers())
        resp.raise_for_status()
        return resp

    def _get(self, path: str) -> HttpResponse:
        resp = self._http.get(self._url(path), headers=self._headers())
        resp.raise_for_status()
        return resp

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer_provider()}"}

    @staticmethod
    def _thread_id_of(create_response: Json) -> str:
        """Extract the new thread id from a ``thread.create`` reply.

        T3 returns it as ``threadId``; we fail loudly on a malformed reply rather
        than dispatch a turn at an empty/None id.
        """
        thread_id = create_response.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError(
                f"thread.create response missing a usable threadId: {create_response!r}"
            )
        return thread_id
