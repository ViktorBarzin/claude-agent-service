"""AFK loop: the autonomous issue-implementer control plane.

This package is the "away-from-keyboard" automation that watches the issue
tracker for ``ready-for-agent`` issues, dispatches each to a fresh **T3** thread
(the full-access ``claudeAgent`` runtime) with the issue-implementer preamble
prepended, then drives the resulting run through its lifecycle — tests-red →
green → pushed → CI → deployed — escalating or fix-forwarding per a small,
testable state machine. It owns no agent behaviour itself; the agent's standing
rules are injected as a prompt preamble (``issue_implementer_prompt``) because
T3 does NOT honour ``~/.claude/CLAUDE.md``.

The whole loop ships **DISABLED**, by two independent gates: ``Config`` defaults
to ``kill_switch=True`` AND an empty ``allowlist`` (see ``config.py``). Importing
this package, scheduling the CronJob entrypoints, or constructing the default
``Config`` therefore dispatches NOTHING and performs zero I/O — a disabled tick
is wholly inert. The package is also not imported by the running service
(``app.main``), so wiring it in changes nothing on its own.

>>> ENABLING IS A DELIBERATE MANUAL STEP, PERFORMED LATER, NEVER BY THIS CODE. <<<
Arming the loop takes BOTH of, on purpose (either alone stays inert, so one
fat-fingered env var can't arm every repo):
  1. clear the kill switch  (``AFK_KILL_SWITCH=false`` / ConfigMap ``kill_switch: "false"``), AND
  2. enrol the exact repos   (``AFK_ALLOWLIST=repo-a,repo-b`` / ConfigMap ``allowlist``).
There is no auto-enable path anywhere in this package; do not add one here.

Every test in the suite runs against fakes — this package never talks to a real
T3 server, GitHub/Forgejo, the cluster, or Slack.

Module map (each is independently testable against the interfaces in
``types.py``):
  * ``types``                    — shared dataclasses + enums (the contract).
  * ``config``                   — disabled-by-default Config + env/configmap loaders.
  * ``issue_implementer_prompt`` — the preamble prepended to every dispatch.
  * ``dispatch_policy``          — which ready issues to dispatch right now (pure).
  * ``run_state_machine``        — snapshot + CI status → next Action (pure).
  * ``phase_checklist``          — render the run's progress as a markdown checklist (pure).
  * ``t3_client``                — the two-POST T3 dispatch + snapshot reader.
  * ``tracker``                  — issue-tracker reads/labels/comments/close.
  * ``ci_watcher``               — commit → CI status.
  * ``notifier``                 — escalation/notification sink.
  * ``poller``                   — CronJob tick #1: select + dispatch ready issues.
  * ``watcher``                  — CronJob tick #2: drive one in-flight run to a verdict.
"""
