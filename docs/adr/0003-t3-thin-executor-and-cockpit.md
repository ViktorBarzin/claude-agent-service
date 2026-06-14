# AFK workers run inside a dedicated T3 Code instance; claude-agent-service dispatches into it

The owner wants one UI to see and converse with every in-flight AFK worker, and
named **T3 Code** (the self-hosted multi-agent cockpit already running at
`t3.viktorbarzin.me`) as that UI. Research into T3's source
(`pingdotgg/t3code`, ~v0.0.27) found it is genuinely built for this — a fleet of
worker "threads" with a live read-model and a scoped HTTP dispatch API — **but**
it can only display sessions **it launched itself**; there is no command to adopt
a session another process started. So "viewable in T3" ⟺ "launched by T3". This
ADR records the resulting architecture: `claude-agent-service` stays the
**control plane** and **dispatches into a dedicated, in-cluster T3 instance**
which is the **executor + cockpit**. The agent runs inside T3; we keep the brain.

## Status

accepted (2026-06-14) — direction decided; **gated on a pilot** (the five
unknowns in the design doc) before the poller is wired and the architecture is
committed.

## Why T3, and why "thin"

T3 provides, out of the box, what we would otherwise hand-build: a three-panel
fleet cockpit (`projects → threads → conversation`), an
`OrchestrationReadModel` with per-thread live status, and
`POST /api/orchestration/dispatch` whose `thread.turn.start` + `bootstrap` can
**create a thread, prepare a git worktree, run a setup script, and deliver a
prompt in one call** — exactly the worker-spawn primitive. Converse / approve /
resume are native (`thread.user-input.respond`, `thread.approval.respond`). For
Claude it embeds `@anthropic-ai/claude-agent-sdk`.

"Thin" = the AFK *behavior and safety* (the `issue-implementer` prompt,
guardrails, always-push, fix-forward/freeze, CI-watch, issue integration) live
in **our** layer (the poller + watcher), not in T3. T3 is a **swappable backend**
we drive over its API.

## Considered options

- **Thin: claude-agent-service dispatches into T3 (chosen).** Control plane calls
  T3's dispatch API; T3 runs the agent in a worktree and shows it. Get the fleet
  view, keep the brain, least to build. Cost: execution moves into the T3 pod, so
  T3's runtime is in the *hot path* (not just the window).
- **claude-agent-service runs the agent, T3 only displays it** — rejected because
  it is impossible: T3 cannot adopt an externally-started session
  (`thread.session.set` is server-internal; no external-session-id field). This
  is the constraint that shaped the whole decision.
- **Deep: claude-agent-service as a custom T3 provider (ACP-style)** — rejected
  for now: keeps the runtime ours with a T3 UI, but means building and
  maintaining a provider against a pre-1.0, internal, no-contributions interface
  — effectively a fork. Revisit only if "thin" proves too limiting.
- **Skip T3; build our own console** (generalized breakglass + tmux) — rejected:
  most stable and fully in-house, but abandons the owner's explicit "see workers
  in T3" goal and means owning a session console forever.

## Consequences

- A **dedicated in-cluster T3 instance** (a pod, consistent with the earlier
  in-cluster-over-devvm substrate choice) is the worker host, separate from the
  per-user devvm T3 instances. It needs the SSD worktree volume, git/Anthropic
  tokens, toolchains, `claude auth`, and an internal Authentik-gated ingress.
- T3's runtime is now in the **execution hot path** — its maturity affects
  whether work *runs*, not only whether it can be *seen*. Mitigations: **pin the
  version and exclude it from Keel** (its churn + hard-cutover auth migrations
  make auto-upgrade a Keel-class hazard), keep the integration thin and the
  backend swappable, and **pilot** the five unknowns first.
- T3 is **single-operator** — fine here: it matches the already-accepted shared
  service identity for AFK work.
- No outbound webhooks from T3 → the watcher **polls**
  `GET /api/orchestration/snapshot`.
- This supersedes the intermediate ideas of evolving `claude-agent-service` into
  its own session/tmux/worktree runtime and building a bespoke attach console.
