# Parallel, independent execution — design

**Date:** 2026-06-02
**Status:** approved, in implementation
**Scope:** `claude-agent-service` — remove the single-flight execution lock so
multiple agent calls run concurrently, each in its own isolated workspace.

## Problem

Today a single global `asyncio.Lock` (`execution_lock`) serializes **every**
agent invocation:

- `POST /execute` returns `409 Agent is busy` when a job is in flight.
- `POST /v1/chat/completions` returns `503 agent is busy` likewise.
- All calls run `claude -p` with `cwd=/workspace/infra` — one shared working
  tree, `git pull --rebase`'d before each call.

The lock exists because two `claude -p` processes in the *same* working tree
would clobber each other's file edits and git state (`.git/index.lock`
contention, racing `git pull --rebase`).

## Goal

Run calls **in parallel**, each **fully independent** of the others, without
the git/file collisions that the lock currently prevents — on a single pod
(`replicas=1`), keeping the in-memory job registry coherent for `/jobs/{id}`
polling.

## Design

### Workspace isolation — per-job local clone

Each job gets its **own git checkout** so file edits and git operations never
touch another job's state:

1. A warm **base clone** lives at `/workspace/base` (created by the existing
   init container; renamed from `/workspace/infra`), git-crypt-unlocked.
2. Per job, under a short-held `git_lock`:
   - Debounced `git fetch origin && git reset --hard origin/master` on the base
     (skipped if fetched within `FETCH_DEBOUNCE_SECONDS`) so bursts share one
     network fetch.
   - `git clone --local /workspace/base /workspace/jobs/<id>` — objects are
     hardlinked (near-free disk, no `.terraform` carried since clone takes
     tracked content only).
   - Re-point `origin` to the GitHub URL and `git-crypt unlock <key>` in the
     job dir.
3. The job runs `claude -p` with `cwd=/workspace/jobs/<id>` **holding no lock**.
4. `finally` → `rm -rf /workspace/jobs/<id>`.

`git_lock` is held only for the fast setup/teardown (~<2 s); execution is fully
parallel. Rejected alternatives: **git worktree** (shares one `.git` → agents
that `git commit`/`pull` still contend — not truly independent) and **`cp -a`**
(copies accumulated `.terraform` provider caches → disk blowup).

Distinct `cwd` per job also isolates Claude CLI per-project state
(`~/.claude/projects/<cwd-hash>/`). The long-lived `CLAUDE_CODE_OAUTH_TOKEN`
avoids credential-file write races in the shared `~/.claude`.

### Concurrency model

- `execution_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)` replaces
  `execution_lock`. Default **`MAX_CONCURRENCY=10`** ("soft-unbounded").
- Requests beyond the limit **queue FIFO** (asyncio fairness) — they are not
  rejected.
- `MAX_QUEUE_DEPTH` safety valve (default **100**): if `active + queued` exceeds
  it, reject (`429` on `/execute`, `503` on chat) to bound memory.
- A `concurrency_slot()` async context manager wraps acquire/release and keeps
  `inflight_active` / `inflight_queued` counters for `/health`.

### Endpoint behavior

| Endpoint | Before | After |
|---|---|---|
| `POST /execute` | `202` or `409` busy | `202` always (unless queue full → `429`); job `status="queued"` until a slot frees, then `running`. **Timeout clock starts on execution, not queue-wait.** |
| `POST /v1/chat/completions` | `200` or `503` busy | **queues** for a slot (caller waits, bounded by the 900 s timeout); still `503` on execution failure/timeout or if queue full |
| `GET /jobs/{id}` | unchanged | unchanged (can now report `queued`) |
| `GET /health` | `{status, busy=lock.locked()}` | `{status, busy=(active>=capacity), active, queued, capacity}` — keeps BeadBoard `/api/agent-status` + beads-dispatcher working |

### Housekeeping

- **Job eviction**: completed/failed/timeout/error jobs older than
  `JOB_TTL_SECONDS` (default 3600) are evicted; the in-memory `jobs` dict
  currently grows unbounded and parallelism increases churn.
- Pod restart still loses in-flight jobs (pre-existing; out of scope — no
  shared store, matching the in-pod decision).

### Infra (`infra/stacks/claude-agent-service/main.tf`)

- Mount the existing `git-crypt-key` configmap into the **main container**
  (today only the init container has it) — needed for per-job unlock.
- Pod memory: request `2Gi`, limit `12Gi` (Burstable, tier-aux); CPU request
  `1`, no CPU limit. Fits node2/3/5 headroom (~22–26 GB free).
- Wire `MAX_CONCURRENCY` env. Rename init-container clone target to
  `/workspace/base`; `WORKSPACE_DIR`→ base path.
- `replicas=1`, `Recreate` unchanged.

## Blast radius (verified)

All callers handle the busy responses gracefully or fail safely, so removing
them is safe:

- **n8n DIUN** (`/execute`) — rate-limited 5/6h, no retry; 409 was rare.
- **payslip-ingest** (`/execute`+poll) — 90× retry; big win from parallelism.
- **recruiter-responder** (`/execute`+poll) — returns `busy`, OpenClaw retries.
- **fire-planner** (`/v1/chat/completions`) — client-side semaphore; can be
  relaxed after this.
- **BeadBoard** (`/execute`) — UI shows busy via `/api/agent-status` (`/health`).
- **beads-dispatcher** CronJob — gates on `/health` busy; 2-min tick.

## Testing (TDD)

Rewrite `test_execute_respects_sequential_lock` and
`test_chat_completions_returns_503_when_agent_busy` (they encode the removed
behavior). New tests: two concurrent `/execute` both run; safety-queue at
`MAX_CONCURRENCY=2`; concurrent chat-completions both run; `/health` capacity
fields; per-job distinct workspace `cwd`; timeout excludes queue-wait; job
eviction; queue-depth rejection. An autouse fixture resets semaphore + counters
+ jobs between tests.

## Docs to update (same change)

`infra/docs/architecture/automated-upgrades.md`,
`infra/docs/runbooks/beads-auto-dispatch.md`, `infra/AGENTS.md`, root
`CLAUDE.md` — all currently describe "sequential / single-slot".
