# Breakglass: isolated deployment, warm-case scope, bounded host capabilities

We are adding a Claude-driven web UI ("breakglass") to recover the devvm when
it is down. It runs as a **separate deployment in its own `claude-breakglass`
namespace** (own ServiceAccount, own Vault role/policy scoped to *only* the
breakglass SSH keys), **not** in the existing `claude-agent` pod, because that
pod runs agents that ingest untrusted input (recruiter emails, nextcloud
todos) with `Bash`, and the shared `terraform-state` Vault policy grants the
whole namespace `secret/data/*` — so co-locating the keys would let a
prompt-injected agent read root-on-devvm credentials. We also add an explicit
`deny` on the breakglass key path to `terraform-state`.

## Status

accepted (2026-06-12)

## Scope decision: warm case only

The devvm (VM 102) and all 7 Kubernetes nodes are guests of the **same single
PVE host**. An in-cluster UI therefore cannot be a true breakglass for
cluster- or host-down events — it would be dead exactly when needed. We scope
it deliberately to the **warm case**: devvm wedged (OOM / disk-full / stuck
service / QEMU I/O stall) while the cluster is healthy. The owner accepted this
limitation explicitly. The **cold case** (cluster/host down) stays with the
separate knock-gated PVE-SSH design (`infra/docs/plans/2026-05-30-breakglass-ssh-access-design.md`)
and the `server-lifecycle` iDRAC CLI — out of scope here.

## Considered options

- **Same pod, gate by endpoint** — rejected: endpoint-gating is HTTP-layer,
  but key exfiltration is filesystem/Vault-layer; a `Bash` agent reads the key
  regardless of which route is exposed.
- **App-level bearer login** — rejected in favour of reusing the ingress
  `auth = "required"` resilience proxy, which already does Authentik SSO with
  an HTTP basic-auth fallback when Authentik is down (the chosen failure
  domain), plus CrowdSec + rate-limit by default.
- **Proxmox API token instead of SSH** — rejected: weaker forensics (no
  QMP/console capture) and would duplicate the SSH mechanism still needed for
  devvm diagnostics.

## Consequences

- Host capabilities are intentionally broad but **bounded**: full sudo shell on
  the devvm (any soft repair), and **autonomous** PVE power verbs
  (`status|forensics|reset|stop|start|cycle` on VM 102 only) via a
  `command="…" restrict` forced-command — never a free shell on the
  hypervisor. Every mutating verb captures forensics first, unconditionally.
- The breakglass agent *can* trigger a reset on its own judgement (the owner
  chose autonomy over a human-confirm gate). In the isolated pod there is no
  untrusted-input injection vector; the residual risk is a model misread
  rebooting a devvm that did not strictly need it — bounded and recoverable.
- The SSH private key is loaded into an in-pod `ssh-agent` (not written to
  disk). This is an availability/hygiene measure, **not** the primary control —
  the dedicated narrow Vault policy is, since any in-pod process could
  otherwise re-fetch the key from Vault.
- The pod is hardened against the very pressure event it exists to fix:
  high `priorityClassName` (anti-eviction), broad tolerations, anti-affinity
  off the contended GPU node, `imagePullPolicy: IfNotPresent`, hardcoded target
  IPs (no DNS dependency), emptyDir-only (no NFS dependency).
