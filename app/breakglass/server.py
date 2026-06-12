"""Breakglass FastAPI app — the in-cluster emergency recovery UI.

Routes:
  GET  /health                 — liveness (no auth)
  GET  /                       — the single-page UI (static)
  POST /api/session            — open a chat session, returns {session_id}
  POST /api/chat               — run one turn, streams SSE events (text/tool/result)
  POST /api/pve/{verb}         — LLM-independent PVE power verb (manual buttons)
  GET  /api/pve/verbs          — list allowed verbs + which mutate

Everything under /api requires auth (edge Authentik header or bearer token).
"""
import json
import os
import uuid

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent_session, config, pve
from .auth import require_auth

app = FastAPI(title="Claude Breakglass")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class SessionResponse(BaseModel):
    session_id: str


class ChatRequest(BaseModel):
    session_id: str
    prompt: str = Field(..., min_length=1)
    model: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "claude-breakglass"}


@app.post("/api/session", response_model=SessionResponse)
async def open_session(_identity: str = Depends(require_auth)):
    # Claude wants a UUID for --session-id.
    return SessionResponse(session_id=str(uuid.uuid4()))


@app.post("/api/chat")
async def chat(req: ChatRequest, _identity: str = Depends(require_auth)):
    """Stream one chat turn as Server-Sent Events. The browser reads the
    response body incrementally (fetch + ReadableStream)."""

    async def _sse():
        try:
            async for ev in agent_session.run_turn(req.session_id, req.prompt, req.model):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
            yield f"data: {json.dumps({'kind': 'error', 'error': str(exc)[:500]})}\n\n"
        yield f"data: {json.dumps({'kind': 'done'})}\n\n"

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
