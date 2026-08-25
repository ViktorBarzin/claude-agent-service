"""What a fixer run is told at dispatch time.

The durable playbook — how to triage, what to fix, what to escalate — lives in
the agent definition (``infra/.claude/agents/issue-responder.md``), which is
version-controlled beside the code it changes. This module builds only the
per-dispatch context: which issue, whether this is a first attempt or a
corrective turn, and the state the previous run left behind.

That split matters because runs are one-shot. A fix-forward turn starts a cold
process with no memory of the turn before it, so everything it needs to not
repeat itself has to arrive in the prompt — and the source of that is the issue
thread, which is exactly what a human reading along sees too.
"""
from app.fixer.runstate import RunRecord

_FIRST_TURN = """\
Fix Forgejo issue {owner}/{repo}#{number} ("{title}").

It is labelled `{trigger_label}`, which means someone reports something is
broken right now and cannot fix it themselves. You are the fixer: diagnose it,
repair it, and see the repair land. Nobody is watching — report everything you
learn on the issue as you go, because the issue is the only record.

Read the issue and every comment first:
  {issue_url}

Follow the issue-responder playbook. Two rules specific to this run:

1. **Finish the root cause, or hand the remainder on.** If you can only fix part
   of it, file a NEW issue describing precisely what remains, labelled
   `{trigger_label}`, and say in your comment which issue continues the work. A
   partial fix that is silently left partial is worse than an escalation.
2. **On the platform stacks — vault, dbaas, traefik, authentik, kyverno — post
   your findings BEFORE you change anything.** You are allowed to fix them, but
   they carry your own ability to report: a bad change there can remove the
   channel you would have used to say what you did.

When you have pushed a commit, state its full sha in a comment. That is how the
watcher knows what to follow through CI.
"""

_FIX_FORWARD_TURN = """\
Continue fixing Forgejo issue {owner}/{repo}#{number} ("{title}").

A previous run of yours pushed `{commit}` for this issue and CI came back RED.
This is fix-forward attempt {attempt}: diagnose why the pipeline failed and fix
it forward. Do not revert your own commit — the decision on this loop is to
correct rather than roll back.

Read the issue and every comment first; your predecessor's findings are there:
  {issue_url}

What you know already:
{notes}

If the failure shows the original diagnosis was wrong, say so plainly in a
comment and fix the real cause. If you cannot make CI green, stop and explain
what you tried — a human takes it from there with your commit left in place.
"""


def _format_notes(record: RunRecord | None) -> str:
    """The previous run's notes as a bullet list, or a plain statement of none."""
    if record is None or not record.notes:
        return "  (nothing recorded beyond the issue comments)"
    return "\n".join(f"  - {note}" for note in record.notes)


def first_turn(
    *, owner: str, repo: str, number: int, title: str, issue_url: str, trigger_label: str
) -> str:
    """The dispatch prompt for a run that has not started yet."""
    return _FIRST_TURN.format(
        owner=owner, repo=repo, number=number, title=title,
        issue_url=issue_url, trigger_label=trigger_label,
    )


def fix_forward_turn(
    *,
    owner: str,
    repo: str,
    number: int,
    title: str,
    issue_url: str,
    record: RunRecord,
) -> str:
    """The dispatch prompt for a corrective turn after a red pipeline."""
    return _FIX_FORWARD_TURN.format(
        owner=owner, repo=repo, number=number, title=title, issue_url=issue_url,
        commit=record.commit or "(unknown)",
        attempt=record.fix_forward_attempts + 1,
        notes=_format_notes(record),
    )
