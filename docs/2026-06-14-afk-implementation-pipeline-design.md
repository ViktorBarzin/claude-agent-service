# AFK implementation pipeline — design

**Date:** 2026-06-14
**Status:** proposed — pilot pending (see "Pilot" below; no code yet)
**Scope:** A new autonomous path that turns a triaged `ready-for-agent` issue
into tested, deployed code with no human at the keyboard. `claude-agent-service`
becomes the **control plane**; a dedicated in-cluster **T3 Code** instance
becomes the **executor + cockpit**. Touches: `claude-agent-service` (new poller
+ dispatch + watcher), a new T3 stack in `infra/`, a shared SSD-NFS volume, and
the per-repo issue trackers.

> Provenance: this design is the output of a long grilling session
> (2026-06-14). It records the decisions *and* the alternatives that were
> considered and dropped, so the reasoning survives. The three hardest-to-reverse
> calls are split into ADRs 0002–0004.

## Problem

Today the development flow is **grill-with-docs → to-prd → to-issues → triage →
implement**, and *every* stage is human-in-the-loop (HITL), including
implementation. The owner wants the HITL boundary to stop at **design + spec**:
once an issue is triaged `ready-for-agent`, an agent should pick it up and
implement it **AFK** (away from keyboard) — write it test-first, push it, and
see it through to a healthy deploy — escalating to a human only when it genuinely
can't proceed.

Two gaps block this today:

- The only existing issue→agent automation is the **infra `issue-responder`**,
  which fires on `user-report`/`feature-request` labels on the `infra` repo
  only — not on `ready-for-agent`, not on the other sub-project repos that the
  general design flow produces.
- `claude-agent-service` only ever clones `infra`, runs one-shot fire-and-forget
  `claude -p` jobs (no session, no live stream, no attach), and has no
  multi-repo checkout. The owner wants to *watch and steer* in-flight work, which
  the batch model can't offer.

## Goal

- HITL covers design + spec only. Publishing `ready-for-agent` issues is the
  release signal (the `to-issues` quiz is the review gate).
- An autonomous loop picks up unblocked `ready-for-agent` issues from
  **enrolled** repos, implements them test-first, and lands them — pushing
  straight to `master` so CI deploys them (see ADR 0002 for the risk posture).
- The owner can **see all in-flight workers and converse with any of them** from
  one UI — the T3 cockpit (see ADR 0003).
- Reuse before building: lean on the existing CI/CD chain, the design skills, T3
  Code's multi-agent cockpit, and the persistence/worktree machinery — rather
  than hand-building a session console and a bespoke runtime.

## Design

### Roles: control plane vs executor + cockpit

| Concern | Owner |
|---|---|
| When to start, which issue, the prompt, the safety envelope | **claude-agent-service** (control plane) — poller + watcher |
| Running the agent (Claude Agent SDK), the worktree, the fleet UI | **T3 Code** (executor + cockpit) — one dedicated in-cluster instance |
| Build → image → deploy → rollout | existing CI/CD (GHA → ghcr → Woodpecker → Keel) |
| Issue queue + state | the per-repo GitHub issue trackers |

The pivotal constraint that forces this split: **T3 can only display sessions it
launched itself** — it has no command to adopt an externally-started session. So
"viewable in T3" ⟺ "launched by T3". To keep `claude-agent-service` in charge
*and* get the fleet view, the control plane **dispatches into T3** rather than
running `claude` itself. See ADR 0003.

### End-to-end flow

```
HUMAN (interactive session)
  /grill-with-docs → /to-prd → /to-issues → /triage
     └ produces ready-for-agent issues (dependency-ordered), labeled by a
       trusted collaborator. Publishing them = the release signal.
══════════════════════ HANDOFF ══════════════════════
CONTROL PLANE  (claude-agent-service, in-cluster)
  poller CronJob (every few min):
    for repo in allowlist:
      skip repo if it already has an agent-in-progress issue   (per-repo lock)
      pick highest-priority ready-for-agent issue where:
        • all "Blocked by" closed   • labeled by a trusted collaborator
      → stamp agent-in-progress
      → POST /api/orchestration/dispatch  (thread.turn.start + bootstrap:
            create thread, prepare worktree, run setup, deliver the prompt)
EXECUTOR + COCKPIT  (dedicated T3 instance, in-cluster)
  runs the issue-implementer agent (our prompt) in the worktree:
    read issue + AGENT-BRIEF + repo CONTEXT.md/ADRs → TDD red-green-refactor
    → commit (paraphrase issue, "Closes #N", AFK trailer) → push master
  watcher (control plane) polls GET /api/orchestration/snapshot + CI:
    ├─ healthy ──────► comment + close issue, drop lock, notify ✅
    ├─ pre-push block ► do NOT push, relabel ready-for-human, escalate
    └─ post-push red ► fix-forward (≤5 attempts / 60 min)
                         ├─ recovers ► healthy
                         └─ exhausts ► FREEZE broken (preserve forensics),
                                       relabel ready-for-human, hard page
```

### Trigger & dispatch predicate

A poller CronJob (mirrors the existing `beads-dispatcher` pattern; stays
in-cluster because neither the service nor T3 has public ingress). It dispatches
issue *I* in repo *R* iff **all** hold:

- `R` is in the **allowlist** ConfigMap, and the **kill switch** is off;
- `I` has label `ready-for-agent`, applied by a **trusted collaborator** (the
  trust gate — on private repos only collaborators can label, so the label *is*
  the authorization; external/bot issues never auto-run);
- every issue in `I`'s "Blocked by" is closed;
- `R` has no issue currently labeled `agent-in-progress` (the per-repo lock).

On dispatch it stamps `agent-in-progress`; on any terminal outcome it removes it.

### Concurrency & locking

**Parallel across repos, serial within a repo.** Multiple repos progress at
once; at most one agent per repo (two agents in one repo would collide on the
working tree). Enforced by the `agent-in-progress` label as a per-repo lock.
Starting value; raise later.

### Merge & failure posture — see ADR 0002

- **Always push to master** (no PR gate). Tests-green is the merge gate; CI +
  rollback are the safety net, matching the human allow-then-audit model.
- **Pre-push** failure (can't get green / blocked / would need a disallowed op):
  do *not* push; relabel `ready-for-human`; comment what was tried; page.
- **Post-push** failure (CI build or rollout red): **fix-forward** up to **5
  attempts or 60 minutes**, then if still red **freeze in the broken state**
  (preserve forensics — do not auto-revert), relabel `ready-for-human`, hard
  page. The owner explicitly chose debuggability over availability here.
- **Budget:** `max_budget_usd = 100` per issue (time/attempt caps usually bite
  first).

### Build/test environment & worktrees — see ADR 0004

The agent must run the target repo's test suite (TDD gate) before pushing.
Therefore:

- **Local toolchains scoped to the allowlist** — the executor image carries only
  the *enrolled* repos' runtimes; the toolchain set grows in lockstep with the
  allowlist.
- **Persistent per-repo checkout + `git worktree` per issue** on a shared
  **SSD-NFS** volume, so git objects, installed deps, and package-manager caches
  stay warm across jobs. This **supersedes** the throwaway `git clone --local`
  model from `2026-06-02-parallel-execution-design.md`; that rejection was
  correct for *concurrent* same-repo jobs, but the serial-within-repo choice
  here removes the `.git` contention it guarded against (ADR 0004). It pays off
  precisely because `to-issues` clusters many slices in one repo, processed
  serially — slice N reuses the warm checkout slice 1 paid for.

### T3 integration: thin dispatch — see ADR 0003

The control plane holds a capability-scoped **`orchestration:operate`** bearer
token (minted via `t3 auth`, stored in Vault, refreshed for the 1-hour expiry)
and calls T3's HTTP API:

- `POST /api/orchestration/dispatch` → `thread.turn.start` with a `bootstrap`
  that creates the thread, prepares the worktree, optionally runs a setup
  script, and delivers the prompt — one call spawns a worktree-isolated worker.
- `GET /api/orchestration/snapshot` → the full fleet read-model (per-thread
  `running`/`idle`/`error`, `hasPendingUserInput`, `hasPendingApprovals`,
  `branch`, `worktreePath`). T3 has **no outbound webhooks**, so the watcher
  **polls** this to drive CI-watch, freeze, and label transitions.

The AFK *behavior and safety* (issue-implementer prompt, guardrails, always-push,
fix-forward/freeze, issue integration) live in **our** thin layer, so T3 is a
**swappable, version-pinned backend** — never Keel-auto-upgraded, reversible to a
self-hosted runtime if it goes sideways.

### Observability & interaction

The "active sessions layer" and the "attach and converse" surface **converge
into one screen — the T3 cockpit**: a live list of all worker threads grouped by
project; click one to stream its transcript and send it a turn. This dissolves
the earlier intermediate ideas of a generalized-breakglass console and a
raw-tmux hybrid attach — T3 provides converse / approve / resume natively
(`thread.user-input.respond`, `thread.approval.respond`).

Cross-system, durable signals the control plane still emits:

- **Phase-checklist comment** on the issue, edited in place as phases complete
  (worktree → tests-red → green → pushed → CI → deployed). Durable, low-noise,
  lives on the issue, doubles as audit trail.
- **Loki** logs labeled `{repo, issue}` for deep-dive.
- **Presence** claim per running session (`repo:<name>`, purpose `AFK #N`),
  heartbeated — so AFK work shows up next to human sessions in the layer the
  prompt hook already injects.
- **Doorbell**: Slack / ntfy ping on terminal states, deep-linking into the T3
  thread. Notify, not control — the dedicated-Slack-control-plane idea is
  dropped in favour of the T3 cockpit.

### Safety envelope

- **Trust gate** — only collaborator-labeled `ready-for-agent` issues run.
- **Allowlist** — a repo is untouchable until enrolled (prereqs: tests + GHA CI
  + `CONTEXT.md`). Start with 1–2 repos; expand deliberately.
- **Kill switch** — one ConfigMap flag pauses all pickup (the Keel
  scale-to-0 reflex, built in from day one).
- **Per-repo lock** — ≤1 agent per repo.
- **Guardrails** (reused from `issue-responder`) — no PVC/PV deletes, no direct
  Vault edits, no force-push to master, infra changes Terraform-only, never
  `[ci skip]`.
- **Identity & audit** — shared service identity; each commit body paraphrases
  the issue and carries `Closes #N` + an AFK-agent trailer, so the commit
  message stays the audit trail.

## Parameters (chosen starting values — all tunable)

| Knob | Value |
|---|---|
| Merge gate | always push to master |
| Post-push failure | fix-forward, then freeze-broken |
| Fix-forward cap | 5 attempts **or** 60 minutes |
| Per-issue budget | `max_budget_usd = 100` |
| Concurrency | parallel across repos, serial within a repo |
| Repo scope | opt-in allowlist, start small |
| Progress detail | phase-checklist on issue + Loki logs |
| Alert channel | Slack (+ ntfy), as a doorbell into T3 |
| Executor | dedicated in-cluster T3 (thin dispatch), version-pinned |

## Pilot — validate before wiring the poller

The thin model rests on five unknowns. Stand up the dedicated T3 instance and
drive a couple of allowlist-repo issues **by hand** via the dispatch API to
confirm each, *before* building the poller and committing the architecture:

1. **Per-thread custom agent + skip-permissions** — can a dispatched thread
   carry *our* `issue-implementer` system prompt and run unattended without
   stalling on T3's approval gating? *(biggest unknown)*
2. **Dispatch auth** — mint `orchestration:operate`, store in Vault, refresh the
   1-hour token.
3. **Status/completion** — drive CI-watch/freeze/labels purely from polling
   `GET /api/orchestration/snapshot`.
4. **Worktree reconciliation** — T3's native `prepareWorktree` vs our
   persistent-checkout-with-warm-caches; pick one or make them cooperate on the
   volume.
5. **The in-cluster T3 pod** — headless `t3 serve --no-browser`, version-pinned
   and **Keel-excluded**, internal ingress + Authentik, with tokens / toolchains
   / SSD volume / `claude auth` provisioned.

## Relationship to prior decisions

- **Supersedes** the worktree rejection in
  `2026-06-02-parallel-execution-design.md` (contextualized, not contradicted —
  ADR 0004).
- **Drops** two intermediate ideas explored and rejected this session:
  evolving `claude-agent-service` into its own session/tmux/worktree runtime,
  and building a bespoke breakglass-generalized console — both replaced by T3.
- **Reuses** the `issue-responder` guardrails, the CI/CD chain, the
  `beads-dispatcher` CronJob pattern, presence, Loki, and the design skills.

## Out of scope / open questions

- Raw-terminal "take-over" of a worker (T3 is a GUI cockpit, not a terminal); if
  ever needed, that's a separate add-on.
- Multi-tenant T3 (it is single-operator by design — fine, it matches the shared
  service identity).
- Cross-repo dependency orchestration beyond per-issue "Blocked by".
- T3 Code is pre-1.0 (~v0.0.x) and churny; the version-pin + Keel-exclude +
  swappable-backend discipline (ADR 0003) is the mitigation.
