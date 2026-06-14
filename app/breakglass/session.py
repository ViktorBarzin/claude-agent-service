"""Attachable server-side sessions — the tmux model for the breakglass chat.

Instead of the client owning conversation state, the SERVER owns it and clients
*attach*. A turn runs as a detached task that keeps going if the client
disconnects (you can background the phone / hit a tunnel blip and the agent
keeps working); its output is appended to a per-session event log and broadcast
to every attached subscriber. A client attaches over SSE, gets the log replayed
(or only the part it missed, via Last-Event-ID), then tails live — exactly like
re-attaching to a tmux session. ``EventSource`` reconnects natively, so the
"re-attach" needs zero client logic.

This module owns the lifecycle; ``agent_session`` still provides the claude
argv + the stream-json→UI-event translation (all subprocesses use the no-shell
list-argv form), and ``config`` the knobs.
"""
import asyncio
import json
import os
import uuid
from subprocess import PIPE
from typing import AsyncIterator

from . import agent_session, config


class Session:
    """One conversation. Owns the replay log + live subscribers + the in-flight
    turn. The claude ``session_id`` is reused with ``--resume`` so the agent
    keeps its own context across turns."""

    def __init__(self, session_id: str):
        self.id = session_id
        # The replay log: every UI event, in order. Index in the list IS the
        # SSE event id, so a reconnecting client replays only what it missed.
        self.events: list[dict] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._turn: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._started = False  # has claude opened this session id yet?

    # ── event log + fan-out ────────────────────────────────────────────────
    def add_event(self, event: dict) -> dict:
        """Append an event to the log and broadcast it to attached clients."""
        stored = {**event, "id": len(self.events)}
        self.events.append(stored)
        for q in list(self._subscribers):
            q.put_nowait(stored)
        return stored

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def turn_active(self) -> bool:
        return self._turn is not None and not self._turn.done()

    # ── running a turn (detached from any client) ──────────────────────────
    def start_turn(self, prompt: str, model: str | None = None) -> bool:
        """Kick off a turn as a background task. Returns False if one is already
        running (one turn at a time per session)."""
        if self.turn_active:
            return False
        self.add_event({"kind": "user", "text": prompt})
        self._turn = asyncio.create_task(self._run_turn(prompt, model))
        return True

    async def _run_turn(self, prompt: str, model: str | None) -> None:
        model = model or config.DEFAULT_MODEL
        resume = self._started
        argv = agent_session._turn_argv(self.id, prompt, resume, model)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, cwd=_workspace_for(self.id), stdout=PIPE, stderr=PIPE,
            )
        except Exception as exc:  # noqa: BLE001
            self.add_event({"kind": "error", "error": f"could not start agent: {exc}"})
            self.add_event({"kind": "turn_end"})
            return
        self._started = True
        assert self._proc.stdout is not None and self._proc.stderr is not None

        try:
            async def _pump():
                async for raw in self._proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ev = agent_session.translate_event(obj)
                    if ev is None:
                        continue
                    if ev.get("kind") == "batch":
                        for sub in ev["events"]:
                            self.add_event(sub)
                    else:
                        self.add_event(ev)

            await asyncio.wait_for(_pump(), timeout=config.TURN_TIMEOUT_SECONDS)
            await self._proc.wait()
            if self._proc.returncode not in (0, None):
                err = (await self._proc.stderr.read()).decode(errors="replace")
                self.add_event({"kind": "error", "error": err.strip()[:500] or f"exit {self._proc.returncode}"})
        except asyncio.TimeoutError:
            await self._kill_proc()
            self.add_event({"kind": "error", "error": f"turn timed out after {config.TURN_TIMEOUT_SECONDS}s"})
        except asyncio.CancelledError:
            await self._kill_proc()
            self.add_event({"kind": "cancelled"})
            raise
        finally:
            self._proc = None
            self.add_event({"kind": "turn_end"})

    async def _kill_proc(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass

    async def cancel(self) -> bool:
        """Stop the in-flight turn. Returns True if a turn was cancelled."""
        if not self.turn_active:
            return False
        await self._kill_proc()
        if self._turn:
            self._turn.cancel()
            try:
                await self._turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return True


def _workspace_for(session_id: str) -> str:
    path = os.path.join(config.SESSIONS_DIR, session_id)
    os.makedirs(path, exist_ok=True)
    return path


class SessionManager:
    """Holds all live sessions. The breakglass is single-operator, so callers
    typically reuse one persistent session; multiple are still supported."""

    def __init__(self):
        self.sessions: dict[str, Session] = {}

    def create(self) -> Session:
        sid = str(uuid.uuid4())
        s = Session(sid)
        self.sessions[sid] = s
        return s

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def get_or_create(self, session_id: str | None) -> Session:
        if session_id and session_id in self.sessions:
            return self.sessions[session_id]
        return self.create()


async def attach_stream(session: Session, last_event_id: int | None) -> AsyncIterator[str]:
    """Yield SSE frames for an attached client: first the replay (everything, or
    only events after ``last_event_id`` on a reconnect), then live events as they
    arrive. Each frame carries an ``id:`` so EventSource resumes precisely."""
    q = session.subscribe()
    try:
        start = 0 if last_event_id is None else last_event_id + 1
        backlog = session.events[start:]
        for ev in backlog:
            yield _sse_frame(ev)
        # Tell the client the replay is done and it's now live.
        yield "event: caught-up\ndata: {}\n\n"

        seen = backlog[-1]["id"] if backlog else (last_event_id if last_event_id is not None else -1)
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=config.SSE_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"  # comment frame keeps the connection warm
                continue
            if ev["id"] <= seen:
                continue
            seen = ev["id"]
            yield _sse_frame(ev)
    finally:
        session.unsubscribe(q)


def _sse_frame(event: dict) -> str:
    return f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"
