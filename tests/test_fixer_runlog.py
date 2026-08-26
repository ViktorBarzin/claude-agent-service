"""Tests for ``app.fixer.runlog`` — durable capture of what a run actually did.

Two properties carry the weight. **It must never fail a run**: an unwritable
volume degrades to a no-op, because losing a log is an inconvenience and losing
the fix is not. And **it must survive a kill**: everything written before the
run died has to be on disk, which is what makes a crashed run readable at all.
"""
import json
import os

from app.fixer.runlog import RunLog, safe_label, summarise


def read(path: str) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def assistant(*tool_names: str, command: str = "") -> str:
    content = [{"type": "tool_use", "name": n,
                "input": ({"command": command} if n == "Bash" else {})}
               for n in tool_names]
    return json.dumps({"type": "assistant", "message": {"content": content}})


def tool_error(name: str = "Bash") -> str:
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "name": name, "is_error": True}]}})


# --------------------------------------------------------------------------- #
# Filenames.
# --------------------------------------------------------------------------- #
def test_a_label_becomes_filename_safe():
    assert safe_label("infra#56") == "infra-56"
    assert safe_label("  weird/../path  ") == "weird-path"
    assert safe_label("") == "run"


def test_the_log_is_named_for_the_issue_and_the_job(tmp_path):
    log = RunLog("abc123", "infra#56", base_dir=str(tmp_path))
    assert log.path.endswith("infra-56-abc123.jsonl")
    assert os.path.isdir(os.path.dirname(log.path))  # dated directory


# --------------------------------------------------------------------------- #
# As-it-goes: written before the run ends.
# --------------------------------------------------------------------------- #
def test_events_are_on_disk_before_finish_is_called(tmp_path):
    """A run killed mid-flight is the one worth reading, so nothing may be
    buffered until completion."""
    log = RunLog("abc123", "infra#56", base_dir=str(tmp_path))
    log.event("start", agent="issue-responder")
    log.raw(assistant("Bash", command="kubectl get pods"))
    records = read(log.path)          # no finish() yet
    assert [r.get("kind") or r.get("type") for r in records] == ["start", "assistant"]


def test_raw_lines_are_kept_verbatim(tmp_path):
    log = RunLog("j", "l", base_dir=str(tmp_path))
    line = assistant("Read")
    log.raw(line)
    assert read(log.path)[0] == json.loads(line)


def test_a_non_json_line_is_wrapped_so_the_file_stays_valid_jsonl(tmp_path):
    log = RunLog("j", "l", base_dir=str(tmp_path))
    log.raw("plain text from the CLI")
    rec = read(log.path)[0]
    assert rec["kind"] == "stdout" and rec["text"] == "plain text from the CLI"


def test_blank_lines_are_skipped(tmp_path):
    log = RunLog("j", "l", base_dir=str(tmp_path))
    log.raw("")
    log.raw("   \n")
    assert read(log.path) == []


# --------------------------------------------------------------------------- #
# Never fail a run.
# --------------------------------------------------------------------------- #
def test_an_unwritable_volume_degrades_to_a_no_op(tmp_path):
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("x")
    log = RunLog("j", "l", base_dir=str(blocked))   # cannot mkdir under a file
    log.event("start")
    log.raw(assistant("Bash"))
    summary = log.finish(0, "completed")
    assert summary["turns"] == 1                     # still summarises in memory


def test_finish_is_safe_to_call_when_nothing_was_written(tmp_path):
    log = RunLog("j", "l", base_dir=str(tmp_path))
    assert log.finish(None, "error")["turns"] == 0


# --------------------------------------------------------------------------- #
# The summary — the signals for "where is the agent struggling".
# --------------------------------------------------------------------------- #
def test_the_summary_counts_turns_and_tools(tmp_path):
    log = RunLog("j", "infra#56", base_dir=str(tmp_path))
    log.raw(assistant("Bash", command="kubectl get pods"))
    log.raw(assistant("Read"))
    log.raw(assistant("Bash", command="kubectl scale --replicas=1"))
    summary = log.finish(0, "completed")
    assert summary["turns"] == 3
    assert summary["tool_calls"] == {"Bash": 2, "Read": 1}
    assert summary["tool_call_total"] == 3


def test_the_summary_counts_tool_failures():
    lines = [assistant("Bash"), tool_error("Bash"), assistant("Bash"), tool_error("Bash")]
    s = summarise(lines)
    assert s["tool_error_total"] == 2
    assert s["tool_errors"] == {"Bash": 2}


def test_a_push_is_detected_from_the_command():
    assert summarise([assistant("Bash", command="git push origin HEAD:master")])["pushed"]


def test_no_push_is_the_default():
    assert summarise([assistant("Bash", command="kubectl get pods")])["pushed"] is False


def test_the_result_and_cost_are_captured():
    lines = [json.dumps({"type": "result", "result": "Resolved: scaled back to 1",
                         "total_cost_usd": 0.42})]
    s = summarise(lines)
    assert "Resolved" in s["result_tail"] and s["cost_usd"] == 0.42


def test_a_truncated_log_still_summarises():
    """A killed run leaves a half-written last line; that must not lose the rest."""
    lines = [assistant("Bash"), '{"type": "assist']
    assert summarise(lines)["turns"] == 1


def test_summarise_tolerates_junk():
    assert summarise(["", "not json", "[]", "null"])["turns"] == 0


def test_the_summary_record_lands_in_the_file(tmp_path):
    log = RunLog("j", "infra#56", base_dir=str(tmp_path))
    log.raw(assistant("Bash", command="git push origin HEAD:master"))
    log.finish(0, "completed", stderr="a warning")
    last = read(log.path)[-1]
    assert last["kind"] == "summary"
    assert last["status"] == "completed" and last["pushed"] is True
    assert last["stderr_tail"] == "a warning"
    assert "duration_s" in last
