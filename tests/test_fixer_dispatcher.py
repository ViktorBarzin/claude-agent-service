"""Tests for ``app.fixer.tick.FixerDispatcher`` — which prompt a dispatch sends.

A fixer run is one-shot: the process that handles a corrective turn shares
nothing with the one before it, so everything the turn needs has to arrive in
its prompt. The dispatcher is what builds that prompt, and until 2026-08-28 it
built the FIRST-TURN prompt for every dispatch and discarded the prompt it was
handed. ``prompts.fix_forward_turn`` existed, took the run record, and was never
called by anything.

That was survivable rather than harmless: the corrective turn on infra#69 worked
out what had happened by reading the issue thread, where the tick had posted "CI
came back red". It got there by diligence, not by being told.

There were no tests here at all, which is the more useful fact — a discarded
argument is invisible until something asserts on it.
"""
import pytest

from app.fixer import config as fixer_config
from app.fixer import tick
from app.fixer.execute_client import ExecuteClient
from app.fixer.runstate import RunRecord, render_comment


class FakeForgejo:
    """Only what the dispatcher asks of it: the issue, and its comments."""

    def __init__(self, title: str = "tuya-bridge is down", comments=None):
        self._title = title
        self._comments = comments or []

    def get_issue(self, repo: str, number: int) -> dict:
        return {"title": self._title, "number": number}

    def list_comments(self, repo: str, number: int) -> list[dict]:
        return [{"body": b} for b in self._comments]


def make_dispatcher(forgejo=None):
    submitted: list[str] = []

    def submit(prompt: str, label: str = "") -> str:
        submitted.append(prompt)
        return "job-new"

    inner = ExecuteClient(submit, lambda job_id: {"status": "running"})
    cfg = fixer_config.FixerConfig(
        forgejo_api="https://forgejo.example/api/v1",
        forgejo_web="https://forgejo.example",
        owner="viktor",
        token="t",
        webhook_secret="s",
        trigger_label="broken",
        human_label="needs-human",
        bot_actor="infra-agent",
        agent="issue-responder",
    )
    return tick.FixerDispatcher(inner, forgejo or FakeForgejo(), cfg), submitted


def test_a_first_dispatch_sends_the_first_turn_prompt():
    dispatcher, submitted = make_dispatcher()
    assert dispatcher.dispatch("infra", 7, "ignored") == "job-new"
    assert len(submitted) == 1
    assert "Fix Forgejo issue viktor/infra#7" in submitted[0]
    assert "tuya-bridge is down" in submitted[0]


def test_a_fix_forward_dispatch_sends_the_corrective_prompt():
    """The whole point of the kind: the turn is told its own commit went red."""
    record = RunRecord(job_id="job-old", started_at=1.0, commit="deadbeef1234",
                       fix_forward_attempts=0, notes=["scaled the deployment"])
    forgejo = FakeForgejo(comments=[render_comment("pushed", record)])
    dispatcher, submitted = make_dispatcher(forgejo)

    dispatcher.dispatch("infra", 7, "ignored", kind="fix_forward")

    prompt = submitted[0]
    assert "Continue fixing Forgejo issue viktor/infra#7" in prompt
    assert "deadbeef1234" in prompt
    assert "fix-forward attempt 1" in prompt
    # The predecessor's notes ride along — that is why the record is read.
    assert "scaled the deployment" in prompt
    assert "Fix Forgejo issue" not in prompt


def test_a_fix_forward_dispatch_with_no_record_still_dispatches():
    """A thread whose footer is missing or unparseable must not wedge the run."""
    dispatcher, submitted = make_dispatcher(FakeForgejo(comments=["no footer here"]))
    dispatcher.dispatch("infra", 7, "ignored", kind="fix_forward")
    assert "Continue fixing Forgejo issue viktor/infra#7" in submitted[0]
    assert "(unknown)" in submitted[0]


def test_a_redispatch_says_the_previous_turn_was_lost():
    dispatcher, submitted = make_dispatcher()
    dispatcher.dispatch("infra", 7, "ignored", kind="redispatch")
    prompt = submitted[0]
    assert "viktor/infra#7" in prompt
    assert "lost" in prompt.lower()
    # It must not read as "your commit broke CI" — nothing was pushed.
    assert "RED" not in prompt


def test_an_unknown_kind_is_refused_rather_than_silently_first_turned():
    """A typo in a kind must not quietly send a first-turn prompt to a run that
    is mid-chain — that is the failure this whole module exists to prevent."""
    dispatcher, _ = make_dispatcher()
    with pytest.raises(ValueError):
        dispatcher.dispatch("infra", 7, "ignored", kind="fix_forwards")
