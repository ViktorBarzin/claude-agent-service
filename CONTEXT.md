# Claude Agent Service

In-cluster FastAPI wrapper that runs the Claude CLI headlessly for other
services (issue automation, recruiter triage, nextcloud todos, …). This
glossary covers the **breakglass** capability layered on top of it; the
existing job-runner concepts (Job, Execute, OpenAI-compat) are documented in
the code.

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
