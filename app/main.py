import asyncio
import hmac
import os
import uuid
from datetime import datetime, timezone
from subprocess import PIPE

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

app = FastAPI(title="Claude Agent Service")

API_TOKEN = os.environ.get("API_BEARER_TOKEN", "")
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/workspace/infra")

jobs: dict[str, dict] = {}
execution_lock = asyncio.Lock()


class ExecuteRequest(BaseModel):
    prompt: str
    agent: str
    max_budget_usd: float = 5.0
    timeout_seconds: int = 2700
    metadata: dict | None = None


def verify_token(authorization: str | None):
    # Reject everything when the service is unconfigured. compare_digest("", "")
    # returns True, so without this guard an empty API_TOKEN would happily
    # accept an empty header.
    if not API_TOKEN:
        raise HTTPException(status_code=401, detail="Service unauthenticated")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(token, API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")


async def run_git_sync():
    proc = await asyncio.create_subprocess_exec(
        "git", "pull", "--rebase",
        cwd=WORKSPACE_DIR,
        stdout=PIPE, stderr=PIPE,
    )
    await proc.wait()


async def run_agent(job_id: str, request: ExecuteRequest):
    try:
        await run_git_sync()

        cmd = [
            "claude", "-p",
            "--agent", request.agent,
            "--dangerously-skip-permissions",
            "--max-budget-usd", str(request.max_budget_usd),
            "--output-format", "json",
            request.prompt,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=WORKSPACE_DIR,
            stdout=PIPE,
            stderr=PIPE,
        )

        output_lines = []
        async for line in proc.stdout:
            output_lines.append(line.decode())

        stderr = await proc.stderr.read()
        await proc.wait()

        jobs[job_id].update({
            "status": "completed" if proc.returncode == 0 else "failed",
            "exit_code": proc.returncode,
            "output": output_lines,
            "stderr": stderr.decode(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
    except asyncio.TimeoutError:
        jobs[job_id].update({"status": "timeout"})
    except Exception as exc:
        jobs[job_id].update({"status": "error", "error": str(exc)})
    finally:
        execution_lock.release()


@app.get("/health")
async def health():
    return {"status": "ok", "busy": execution_lock.locked()}


@app.post("/execute", status_code=202)
async def execute(
    request: ExecuteRequest,
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)

    if execution_lock.locked():
        raise HTTPException(status_code=409, detail="Agent is busy")

    await execution_lock.acquire()

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "status": "running",
        "prompt": request.prompt,
        "agent": request.agent,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "metadata": request.metadata,
    }

    asyncio.create_task(
        asyncio.wait_for(
            run_agent(job_id, request),
            timeout=request.timeout_seconds,
        )
    )

    return {"job_id": job_id, "status": "running"}


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]
