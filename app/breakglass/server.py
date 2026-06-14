"""Breakglass FastAPI app — the in-cluster emergency recovery UI.

The chat uses the tmux/attach model (see session.py): the server owns the
conversation; clients attach over SSE and the turn keeps running if they
disconnect.

Routes:
  GET  /health                        — liveness (no auth)
  GET  /                              — the single-page UI (static)
  POST /api/session                   — create a session, returns {session_id}
  GET  /api/session/{id}/stream       — ATTACH (SSE): replay + live tail
  POST /api/session/{id}/prompt       — run a turn (detached; survives disconnect)
  POST /api/session/{id}/cancel       — stop the in-flight turn
  GET  /api/pve/verbs                 — list allowed verbs + which mutate
  POST /api/pve/{verb}                — LLM-independent PVE power verb (buttons)

Everything under /api requires auth (edge Authentik header or bearer token).
"""
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, pve
from .auth import require_auth
from .session import SessionManager, attach_stream

app = FastAPI(title="Claude Breakglass")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

manager = SessionManager()


class SessionResponse(BaseModel):
    session_id: str


class PromptRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "claude-breakglass"}


@app.post("/api/session", response_model=SessionResponse)
async def open_session(_identity: str = Depends(require_auth)):
    return SessionResponse(session_id=manager.create().id)


@app.get("/api/session/{session_id}/stream")
async def attach(
    session_id: str,
    _identity: str = Depends(require_auth),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Attach to a session (SSE). Replays the conversation so far, then tails
    live. On an EventSource auto-reconnect the browser sends Last-Event-ID, so we
    replay only what was missed."""
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        leid = int(last_event_id) if last_event_id is not None else None
    except ValueError:
        leid = None
    return StreamingResponse(
        attach_stream(session, leid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.post("/api/session/{session_id}/prompt")
async def prompt(session_id: str, req: PromptRequest, _identity: str = Depends(require_auth)):
    """Start a turn. It runs DETACHED (keeps going if the client disconnects);
    output is delivered via the attach stream, not this response."""
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.start_turn(req.prompt, req.model):
        raise HTTPException(status_code=409, detail="a turn is already running")
    return {"status": "started"}


@app.post("/api/session/{session_id}/cancel")
async def cancel(session_id: str, _identity: str = Depends(require_auth)):
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    cancelled = await session.cancel()
    return {"cancelled": cancelled}


@app.get("/api/pve/verbs")
async def pve_verbs(_identity: str = Depends(require_auth)):
    return {
        "verbs": sorted(pve.ALLOWED_VERBS),
        "mutating": sorted(pve.MUTATING_VERBS),
    }


@app.post("/api/pve/{verb}")
async def pve_verb(verb: str, _identity: str = Depends(require_auth)):
    """Run a PVE power verb directly (no LLM in the path). Mutating verbs
    capture forensics first on the host, unconditionally."""
    if not pve.is_allowed(verb):
        raise HTTPException(status_code=400, detail=f"unknown verb '{verb}'")
    result = await pve.run_verb(verb)
    status = 200 if result.get("exit_code") == 0 else 502
    return JSONResponse(status_code=status, content=result)


# Serve the SPA. Mounted last so it doesn't shadow /api or /health.
if os.path.isdir(_STATIC_DIR):
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
