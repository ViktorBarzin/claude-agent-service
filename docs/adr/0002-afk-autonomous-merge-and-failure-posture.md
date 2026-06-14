# AFK agents push straight to master; failures fix-forward then freeze, not revert

The AFK implementation pipeline (see
`docs/2026-06-14-afk-implementation-pipeline-design.md`) lets an autonomous
agent land code with no human at the keyboard. The owner deliberately chose the
most hands-off posture: **AFK-written code pushes straight to `master`** (which
then deploys via the existing CI/CD chain) with **no pull-request review gate**,
and when a deploy breaks, the agent **fixes forward and then freezes the broken
state** rather than auto-reverting. This ADR records that risk posture and why it
was chosen over the safer alternatives, because it is surprising and not cheap to
walk back once callers and habits depend on it.

## Status

accepted (2026-06-14) — posture decided; enforced once the pipeline ships
(pilot-gated).

## Context

`master` on every enrolled repo deploys continuously (GHA build → ghcr →
Woodpecker → Keel). So "where AFK code lands" is really "what reaches a live
deploy without a human looking". The owner weighed three merge gates and three
post-push failure responses and picked the autonomy-maximizing end of both,
accepting the blast radius explicitly.

## Considered options — merge gate

- **Always push to master (chosen).** Tests-green is the gate; CI + rollback are
  the safety net. Matches the existing human allow-then-audit model (non-admins
  already push straight to master). Most hands-off.
- **Adaptive (push if confident, else PR)** — rejected as the *default* though it
  is what `issue-responder` does; the owner wanted full hands-off, not a
  confidence-gated PR for otherwise-working code.
- **Always open a PR** — rejected: reintroduces a human merge step on every
  issue, i.e. "AFK implementation, human merge" — not the goal.

## Considered options — post-push failure (CI/rollout goes red after a green push)

- **Fix-forward then freeze (chosen).** Iterate with corrective commits up to
  **5 attempts or 60 minutes**; if still red, **leave the broken state in place**
  (do not revert), relabel the issue `ready-for-human`, and hard-page. Same
  forensics-first instinct as the breakglass (ADR 0001): preserve the exact
  failing state for debugging rather than auto-cleaning it away.
- **Auto-revert + escalate** — rejected (was the recommendation): restores green
  fastest, but destroys the forensic state the owner wants to inspect.
- **Alert and freeze immediately (no fix-forward)** — rejected: gives up on
  transient/env-drift failures a corrective commit would clear.

Pre-push failure (can't reach green, blocked, or would need a disallowed op) is
not a dilemma: the agent does **not** push, relabels `ready-for-human`, comments
what it tried, and pages.

## Consequences

- An unreviewed logic error can deploy before any human sees it; rollback (not
  review) is the safety net. Bounded by: tests-as-gate, the start-small
  allowlist, the per-repo lock, and the kill switch.
- A frozen-broken deploy can sit unhealthy until the owner answers the page —
  availability is traded for debuggability, by explicit choice. Acceptable
  because enrolled repos are non-critical by the allowlist prerequisite, and the
  owner is paged hard (Slack + ntfy).
- Fix-forward can stack up to 5 commits on a bad change before freezing; the
  60-minute cap bounds the churn window.
- Per-issue spend is capped at `max_budget_usd = 100`.
- Guardrails still hold underneath this posture: no PVC/PV deletes, no direct
  Vault edits, no force-push, infra changes Terraform-only, never `[ci skip]`.
- Reversible: tightening to adaptive/PR or to auto-revert is a config + watcher
  change, not a re-architecture — but callers/habits will have formed around
  "it just lands", so flag loudly if reversing.
