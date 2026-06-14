# Implementation agents use persistent per-repo checkouts + git worktrees, reversing the throwaway-clone rule for this path

`2026-06-02-parallel-execution-design.md` deliberately **rejected git worktrees**
and chose throwaway `git clone --local` per job, "because worktrees share one
`.git` → agents that `git commit`/`pull` still contend — not truly independent".
The AFK implementation pipeline
(`docs/2026-06-14-afk-implementation-pipeline-design.md`) **reverses that for its
own path**: each enrolled repo gets a **persistent checkout**, and each issue
runs in a **`git worktree`** off it, on a shared **SSD-NFS** volume. This ADR
records why the earlier rejection does not apply here — so the two decisions
read as complementary, not contradictory.

## Status

accepted (2026-06-14) — for the AFK implementation path only; the existing
job-runner (recruiter-triage, nextcloud-todos, etc.) keeps throwaway clones.

## Why the 2026-06-02 rejection doesn't bind this path

The rejection's premise was **concurrent jobs in the same checkout** contending
on `.git/index.lock` and racing `git pull`. The AFK pipeline's concurrency model
is **serial within a repo, parallel only across repos** (ADR-adjacent decision in
the design doc): at most one agent ever touches a given repo's `.git` at a time,
and different repos are different checkouts. The contention the rejection guarded
against cannot occur here. With that removed, worktrees become the *better*
choice because they unlock cache reuse the throwaway model can't.

## Considered options

- **Persistent checkout + worktree per issue, on SSD-NFS (chosen).** Warm git
  objects, **persisted `node_modules`/venv/build caches**, and shared
  package-manager caches survive across jobs, so the TDD loop stops reinstalling
  deps every run. Compounds with `to-issues` clustering many slices in one repo,
  processed serially — slice N reuses slice 1's warm tree.
- **Throwaway `git clone --local` per job (status quo elsewhere)** — rejected for
  this path: correct for the concurrent job-runner, but re-pays dependency
  install on every issue, which dominates wall-clock for an
  implement-test-fix-forward loop.
- **`cp -a` of a warm tree** — rejected (same reason as 2026-06-02): copies
  accumulated caches → disk blowup, and no git isolation.

## Considered options — storage

- **SSD-NFS (chosen).** The current `/persistent` PVC is `5Gi` **HDD NFS**
  (`nfs-truenas` → `/srv/nfs`) and unused; git checkouts + `node_modules` are
  death-by-small-files on HDD NFS and 5Gi is too small. Provision an SSD-backed
  NFS class over `/srv/nfs-ssd` (other apps already use that path) at a realistic
  size (tens of GB).
- **HDD NFS / `/persistent` as-is** — rejected: too slow for many small files,
  too small.
- **Local block (proxmox-lvm)** — rejected: faster but HDD and node-pinned (RWO),
  lost on reschedule; NFS RWX survives and the volume also holds session state.

## Consequences

- One **SSD-NFS volume** holds, per enrolled repo: the persistent checkout, the
  warm dep/package caches, and (under ADR 0003) the worktrees T3 prepares. Cache
  env (`pip`, `GOMODCACHE`/`GOCACHE`, `PNPM_HOME`/npm, cargo) must be wired to it
  — today caching is off (`pip --no-cache-dir`, no cache envs set).
- Housekeeping the throwaway model didn't need: `git fetch` before each
  `worktree add`, periodic `git worktree prune` + `git gc`, and cache eviction if
  the volume fills.
- **`infra` stays on its own path** — it is git-crypt, and editing encrypted
  files from a worktree is disallowed; the persistent-worktree model is for the
  non-`infra` app repos in the allowlist.
- Open reconciliation (pilot): whether T3's native `prepareWorktree` writes into
  this volume + our persistent checkouts, or we manage the checkout and point T3
  at it. Resolve before committing the architecture.
