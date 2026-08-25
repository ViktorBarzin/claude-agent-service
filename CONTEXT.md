# Claude Agent Service

In-cluster FastAPI wrapper that runs the Claude CLI headlessly for other
services (issue automation, recruiter triage, nextcloud todos, …). This
glossary covers two capabilities layered on top of it — **breakglass** and the
**fixer** — while the existing job-runner concepts (Job, Execute,
OpenAI-compat) are documented in the code.

## Language

### Breakglass

**Breakglass**:
The emergency capability for regaining control of the **devvm** when it is down
but the cluster is healthy — a Claude-driven web UI that SSHes *into* the devvm
to diagnose/repair and can power-cycle it via the PVE host.
_Avoid_: "disaster recovery", "the cold breakglass" (that is the separate
cluster-down SSH path — see **Warm case / Cold case**).

**Breakglass agent**:
The single, isolated Claude agent the breakglass UI talks to. It has host
access (sudo on the devvm, PVE power verbs) and a deliberately narrow tool
surface — no web/untrusted-input tools — so it carries no prompt-injection
vector.
_Avoid_: reusing the general job-runner agents (recruiter-triage,
nextcloud-todos-exec) for breakglass — those ingest untrusted input.

**Warm case** / **Cold case**:
The **warm case** is "devvm wedged, cluster healthy" — the breakglass's entire
scope. The **cold case** is "cluster or PVE host down", which an in-cluster UI
cannot survive (devvm and all nodes are guests of one PVE host) and is handled
elsewhere (knock-gated PVE SSH design + iDRAC), explicitly out of scope here.
_Avoid_: calling the in-cluster UI a general "devvm is down" tool — it only
covers the warm case.

**Forced-command verb**:
A single whitelisted operation a breakglass SSH key may invoke — enforced by
`command="…" restrict` in the host's `authorized_keys`, never a free shell on
the PVE host. The verbs are `status | forensics | reset | stop | start |
cycle`, scoped to VM 102 only.
_Avoid_: "remote command", "ssh command" (those imply an open shell).

**Cycle**:
A full **stop→start** of VM 102 — distinct from a warm reset/reboot because it
spawns a fresh QEMU process and so applies staged VM config (the fix for the
2026-06-11 QEMU I/O stall). A warm reset reuses the wedged process.
_Avoid_: using "reset" or "reboot" to mean a stop→start.

**Forensics**:
The unconditional pre-mutation state capture (`qm status/config/pending` + QMP
query, guest diagnostics) that runs *before* any mutating verb, so an erroneous
reset never destroys the evidence of why the devvm was wedged.
_Avoid_: "logs", "snapshot" (this is a point-in-time diagnostic dump, not a
disk snapshot).

### Fixer

Design record: `docs/2026-08-25-forgejo-fixer-design.md`.

**Fixer**:
The capability that turns a `broken` issue on Forgejo `viktor/infra` into a
diagnosed, repaired, deployed change without a human in the loop. Scoped to the
cluster and the `infra` repo.
_Avoid_: "the AFK loop" (that is the parked, T3-executed pipeline this reuses
parts of), "the issue-responder" (that is the agent definition the fixer runs,
not the capability).

**Fixer run**:
One `/execute` job dispatched for one issue. Runs are **one-shot**: a run holds
no session, so anything a successor needs is written to the issue.
_Avoid_: "session", "thread" (both imply continuity a run does not have).

**`broken`** / **`change`**:
The two labels a person — or a person's agent — applies. **`broken`** means
something is not working right now and **dispatches a fixer**. **`change`** is a
proposal with nothing currently failing, and dispatches nothing.
_Avoid_: `user-report` / `feature-request` (the retired GitHub vocabulary; they
name the reporter or the wish rather than the observed state).

**Follow-up issue**:
The mechanism for continuing across one-shot runs: a run that cannot fix the
whole root cause files a new `broken` issue naming what remains, which
dispatches the next run.
_Avoid_: "retry" (a follow-up is new work with a new root cause, not the same
work again).

**Chain**:
A root issue plus the follow-up issues descended from it. Each issue records its
chain parent, so the whole path is readable from the tracker.

**Per-repo lock**:
The invariant that at most one fixer run holds a repo at a time. With `infra` as
the only enrolled repo, it is what bounds burn rate — there are no budget, time,
or depth caps.
_Avoid_: "rate limit", "quota" (nothing is counted; a second issue simply
queues).

**Fix forward**:
The response to a red pipeline on a commit the fixer pushed: dispatch another
corrective run rather than reverting. Chosen over revert-on-red.
_Avoid_: "rollback", "revert" (the fixer does not undo its own commit).

**Freeze**:
The terminal state when fix-forward cannot continue: the broken commit is left
in place, the issue is labelled `needs-human` and assigned to `viktor`, and the
doorbell fires.
_Avoid_: "fail" (the run reached a definite state and handed over deliberately).

**Brake**:
The two human stops. The **`paused`** label stops the next dispatch for one
issue, instantly and without a deploy. **`AFK_KILL_SWITCH`** stops all dispatch
globally via a committed config change. Neither cancels a run already in flight.
_Avoid_: "kill switch" for the label (that name belongs to the global one).

**Doorbell**:
The terminal-state alert (`done` / `needs-human` / `frozen`) sent to ntfy. The
reporter separately receives Forgejo's own email on every comment.
_Avoid_: "notification" unqualified — the reporter's email and the owner's
doorbell are different channels with different audiences.

## Relationships

- The **Breakglass** UI is served by an in-cluster pod and reaches the
  **devvm** over SSH; it does **not** proxy to anything hosted on the devvm
  (unlike `terminal.viktorbarzin.me`), so it survives the devvm being down.
- A **Breakglass agent** invokes **Forced-command verbs** on the PVE host;
  every mutating verb runs **Forensics** first.
- A **Cycle** is the verb that applies staged VM config; a **reset** is the
  warm variant that does not.
- **Breakglass** covers only the **Warm case**; the **Cold case** is a
  separate, out-of-scope recovery path.
- A **`broken`** label dispatches one **Fixer run**; a **`change`** label
  dispatches nothing.
- A **Fixer run** that cannot finish the root cause files a **Follow-up issue**,
  extending the **Chain**; a run that pushes a commit CI rejects goes to **Fix
  forward**, and when that cannot continue it ends in **Freeze**.
- The **Per-repo lock** admits one **Fixer run** at a time; a **Brake** stops
  the next dispatch but never one in flight.
- **Freeze** and a successful close both fire the **Doorbell**; the reporter
  hears about every comment by email regardless.
- The **Fixer** and the **Breakglass agent** are deliberately separate: the
  fixer repairs the cluster from inside it, the breakglass repairs the devvm
  from outside. Neither is a fallback for the other.

## Example dialogue

> **Dev:** "If the devvm OOMs, can the **Breakglass agent** just **reset** it?"
> **Owner:** "It can, autonomously — but a **reset** is a warm reboot. If the
> QEMU process is wedged (the 2026-06-11 class), it needs a **cycle** —
> stop→start — to apply the staged config. Either way it captures
> **Forensics** first."
> **Dev:** "And if the whole cluster is down?"
> **Owner:** "Then the breakglass is down too — that's the **Cold case**, not
> this tool. This one assumes the cluster is healthy."

## Flagged ambiguities

- "reset" was used to mean both a warm reboot and a stop→start — resolved:
  **reset** is warm, **cycle** is stop→start (and is what applies staged
  config).
- "breakglass" was used for both this warm UI and the cluster-down SSH path —
  resolved: this context's **Breakglass** is the **Warm case** UI only.
- "act as me" was used for both a shell session as `wizard` and an agent holding
  admin capability — resolved: the **Fixer** is not an act-as. terminal-lobby's
  `?as=` lens is a separate feature and is not part of this capability.
- "a label for fixing" was read as both a router (which playbook to run) and a
  permission (whether the agent may mutate) — resolved: **`broken`** routes to
  the incident playbook; there is no separate permission label.
- "no limits" appeared to contradict a chain guard — resolved: no budget, time or
  depth caps, with the **Per-repo lock** as the only throttle.
