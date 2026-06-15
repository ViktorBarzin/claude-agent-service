"""Adapter for the in-cluster T3 Code instance — the AFK executor + cockpit.

The control plane keeps the brain; T3 runs the agent. This module is the thin
wire between them, written against T3's **real** orchestration contract
(reverse-engineered from the v0.0.27 binary and verified live against t3-afk on
2026-06-15 — an earlier version of this adapter was written against a guessed
shape that a fake test accepted but the real server 400s).

The contract, in three facts that shape everything here:

  1. **Bare command envelope.** ``POST /api/orchestration/dispatch`` takes a
     single command object whose discriminator is ``type`` (NOT a ``command``
     string, NOT a wrapper). The body *is* the command.
  2. **Client-authoritative IDs.** The CLIENT mints ``threadId`` / ``commandId``
     / ``messageId`` (UUIDs) and stamps ``createdAt`` (ISO-8601); the server
     replies ``{"sequence": N}`` and does NOT echo the thread id. So ``dispatch``
     returns the id it generated, never one parsed from the response.
  3. **Threads live in a project.** A project's ``workspaceRoot`` is the repo
     checkout the agent runs in (it ``cd``s there and commits there). So a repo
     maps to a project; ``dispatch`` ensures that project exists before creating
     the thread.

Operations (the methods ``poller`` / ``watcher`` call, plus a multi-turn helper):

  * ``dispatch(repo, issue, prompt) -> thread_id`` — ensure the repo's project,
    then ``thread.create`` + ``thread.turn.start`` (``ISSUE_IMPLEMENTER_PREAMBLE
    + prompt`` as the user message). Returns the client-minted thread id.
  * ``send_turn(thread_id, prompt) -> None`` — a follow-up user turn on an
    existing thread. Multi-turn context is retained (verified live), so this is
    how a conversation continues without spawning a fresh thread.
  * ``snapshot() -> dict`` — the fleet read-model (``GET``); the watcher reads
    per-thread ``latestTurn.state`` from it.

The HTTP transport, the bearer provider, the id factory, and the clock are all
**injected**, so production hands in an ``httpx.Client`` + a Vault-backed token
reader + ``uuid4`` + a UTC clock, while tests hand in deterministic fakes. The
bearer is re-read from the provider on **every** request because T3's
``orchestration:operate`` token rotates.
"""
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .issue_implementer_prompt import ISSUE_IMPLEMENTER_PREAMBLE

# Orchestration API paths, relative to the configured base URL.
_DISPATCH_PATH = "/api/orchestration/dispatch"
_SNAPSHOT_PATH = "/api/orchestration/snapshot"

# Pilot-baked execution envelope. ``claudeAgent`` is the embedded Claude Agent
# SDK instance; ``full-access`` is the unattended runtime (bypass-permissions);
# ``default`` interaction mode is normal turns (vs ``plan``). The model is the
# one the pilot validated — tunable via the constructor.
_INSTANCE_ID = "claudeAgent"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_RUNTIME_MODE = "full-access"
_INTERACTION_MODE = "default"

# JSON shapes. Command bodies and the snapshot read-model are open string-keyed
# objects; ``object`` values keep us honest without a bare ``Any``.
type Json = dict[str, object]


def _uuid() -> str:
    """Default id factory: a fresh random UUID string (thread/command/message ids)."""
    return str(uuid.uuid4())


def _now_iso() -> str:
    """Default clock: the current instant as an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProjectRef:
    """Where a repo's agent runs. ``project_id`` is the stable T3 project id (the
    client mints it, deterministically per repo); ``workspace_root`` is the repo
    checkout directory the project points at (the agent's cwd); ``title`` is the
    human label shown in the cockpit."""

    project_id: str
    workspace_root: str
    title: str


def default_project_resolver(workspace_base: str = "/data") -> "Callable[[str], ProjectRef]":
    """A repo -> :class:`ProjectRef` resolver with stable, deterministic ids.

    ``project_id`` is a UUID5 of the repo (so the same repo always resolves to the
    same project across ticks and restarts — ``dispatch``'s ensure-project step
    is therefore idempotent); ``workspace_root`` is ``<workspace_base>/<slug>``
    where the slug flattens ``owner/name`` to a single path segment. The checkout
    itself (cloning the repo into ``workspace_root``) is an enrollment concern,
    not this adapter's — the agent or a provisioning step populates it.
    """

    def resolve(repo: str) -> ProjectRef:
        slug = repo.replace("/", "__")
        return ProjectRef(
            project_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"afk-project:{repo}")),
            workspace_root=f"{workspace_base.rstrip('/')}/{slug}",
            title=repo,
        )

    return resolve


class HttpResponse(Protocol):
    """The httpx-shaped response surface this adapter relies on: ``raise_for_status``
    turns a non-2xx into an exception (so a failed command aborts the sequence)
    and ``json`` parses the body."""

    def raise_for_status(self) -> object: ...

    def json(self) -> Json: ...


class HttpClient(Protocol):
    """Minimal injected transport: a JSON ``post`` and a ``get``, both taking
    explicit headers. A strict subset of ``httpx.Client`` so the real client
    passes straight through and tests pass a recorder."""

    def post(self, url: str, json: Json, headers: dict[str, str]) -> HttpResponse: ...

    def get(self, url: str, headers: dict[str, str]) -> HttpResponse: ...


class T3Client:
    """Dispatch/snapshot adapter for one in-cluster T3 instance.

    ``base_url`` is the T3 service root (a trailing slash is tolerated); ``http``
    is the injected transport; ``bearer_provider`` returns the current
    ``orchestration:operate`` token, re-read per request; ``project_resolver``
    maps a repo to its :class:`ProjectRef`; ``id_factory`` / ``clock`` are
    injected for deterministic tests (defaulting to ``uuid4`` / UTC now).
    """

    def __init__(
        self,
        base_url: str,
        http: HttpClient,
        bearer_provider: Callable[[], str],
        project_resolver: Callable[[str], ProjectRef] | None = None,
        *,
        id_factory: Callable[[], str] = _uuid,
        clock: Callable[[], str] = _now_iso,
        model: str = _DEFAULT_MODEL,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._bearer_provider = bearer_provider
        self._project_for = project_resolver or default_project_resolver()
        self._id = id_factory
        self._now = clock
        self._model = model

    # ----------------------------------------------------------------- #
    # Public API (the ``t3_client.T3Client`` contract the poller/watcher use).
    # ----------------------------------------------------------------- #
    def dispatch(self, repo: str, issue: int, prompt: str) -> str:
        """Spawn one worker thread for ``issue`` of ``repo`` and return its id.

        Ensures the repo's project exists, generates the thread id locally, then
        POSTs ``thread.create`` followed by ``thread.turn.start`` (delivering
        ``ISSUE_IMPLEMENTER_PREAMBLE + prompt``). Any failed POST raises and
        short-circuits the rest of the sequence. The returned id is the one this
        method minted — the server never sends it back.
        """
        project = self._ensure_project(repo)
        thread_id = self._id()

        self._post(self._thread_create_command(thread_id, project))
        self._post(self._turn_command(thread_id, ISSUE_IMPLEMENTER_PREAMBLE + prompt))
        return thread_id

    def send_turn(self, thread_id: str, prompt: str) -> None:
        """Deliver a follow-up user turn to an existing thread (multi-turn).

        Used to continue a conversation — the agent retains the thread's prior
        context across turns. No preamble: the standing rules were already
        delivered on the opening turn.
        """
        self._post(self._turn_command(thread_id, prompt))

    def snapshot(self) -> Json:
        """Return the parsed fleet read-model from ``/api/orchestration/snapshot``."""
        return self._get(_SNAPSHOT_PATH).json()

    # ----------------------------------------------------------------- #
    # Command builders (the real wire shapes).
    # ----------------------------------------------------------------- #
    def _ensure_project(self, repo: str) -> ProjectRef:
        """Make sure the repo's project exists, creating it if absent. Idempotent:
        the resolver's project id is stable per repo, so a project already in the
        snapshot is left untouched (no duplicate, no error)."""
        project = self._project_for(repo)
        existing = {
            p.get("id") for p in self._get(_SNAPSHOT_PATH).json().get("projects", [])
        }
        if project.project_id not in existing:
            self._post(
                {
                    "type": "project.create",
                    "commandId": self._id(),
                    "projectId": project.project_id,
                    "title": project.title,
                    "workspaceRoot": project.workspace_root,
                    "createWorkspaceRootIfMissing": True,
                    "createdAt": self._now(),
                }
            )
        return project

    def _thread_create_command(self, thread_id: str, project: ProjectRef) -> Json:
        return {
            "type": "thread.create",
            "commandId": self._id(),
            "threadId": thread_id,
            "projectId": project.project_id,
            "title": project.title,
            "modelSelection": {"instanceId": _INSTANCE_ID, "model": self._model},
            "runtimeMode": _RUNTIME_MODE,
            "interactionMode": _INTERACTION_MODE,
            "branch": None,
            "worktreePath": None,
            "createdAt": self._now(),
        }

    def _turn_command(self, thread_id: str, text: str) -> Json:
        return {
            "type": "thread.turn.start",
            "commandId": self._id(),
            "threadId": thread_id,
            "message": {
                "messageId": self._id(),
                "role": "user",
                "text": text,
                "attachments": [],
            },
            "runtimeMode": _RUNTIME_MODE,
            "interactionMode": _INTERACTION_MODE,
            "createdAt": self._now(),
        }

    # ----------------------------------------------------------------- #
    # Transport internals.
    # ----------------------------------------------------------------- #
    def _post(self, command: Json) -> HttpResponse:
        resp = self._http.post(self._url(_DISPATCH_PATH), json=command, headers=self._headers())
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
