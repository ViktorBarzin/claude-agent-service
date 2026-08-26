"""Durable, as-it-happens capture of what an agent run actually did.

A run's own comments say what it concluded. They do not say where it went round
in circles, which tool kept failing, how many turns it burned before finding the
thing, or that it never called `kubectl` at all. That is the material for
improving the agent, and until now it lived only in an in-process dict that a pod
restart threw away.

Every run therefore streams its events to a JSONL file on the persistent volume
**as they arrive**, not at the end: a run that is killed mid-flight is exactly the
one worth reading afterwards, and a log written only on completion would lose it.
Each file is one run. A trailing ``summary`` record carries the derived signals —
turn count, tool histogram, tool failures, duration, whether it pushed — so a
question like "which tool fails most often" is a grep rather than a re-read of
every transcript.

Layout::

    /persistent/fixer-runs/2026-08-26/infra-56-65ac0a5d90b5.jsonl

Dated directories keep a day's runs together and make retention a directory
delete. The label (``infra-56``) comes from the caller, so a run is findable by
the issue it was for rather than only by an opaque job id.
"""
import json
import os
import re
import time
from collections import Counter
from typing import Any

DEFAULT_BASE = os.environ.get("FIXER_RUNLOG_DIR", "/persistent/fixer-runs")

# A label must be safe as a filename fragment: this is built from an issue ref
# and a job id, both of ours, but it lands in a path so it is bounded anyway.
_LABEL_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def safe_label(raw: str) -> str:
    """A filename-safe fragment, e.g. ``infra#56`` -> ``infra-56``."""
    cleaned = _LABEL_SAFE.sub("-", (raw or "").strip()).strip("-")
    return cleaned[:60] or "run"


class RunLog:
    """One run's event log, flushed on every write.

    Deliberately forgiving: logging must never be the reason a run fails, so an
    unwritable volume degrades to a no-op rather than raising into the job.
    """

    def __init__(self, job_id: str, label: str = "", base_dir: str = DEFAULT_BASE,
                 clock=time.time) -> None:
        self.job_id = job_id
        self.label = safe_label(label)
        self._clock = clock
        self._started = clock()
        self._lines: list[str] = []
        self._fh = None
        self.path = ""
        try:
            day = time.strftime("%Y-%m-%d", time.gmtime(self._started))
            directory = os.path.join(base_dir, day)
            os.makedirs(directory, exist_ok=True)
            self.path = os.path.join(directory, f"{self.label}-{job_id}.jsonl")
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError:
            self._fh = None  # no volume, no log — never a failed run

    # ------------------------------------------------------------------ write #
    def event(self, kind: str, **fields: Any) -> None:
        """Append one record of our own making (start, finish, note)."""
        self._write({"ts": self._clock(), "kind": kind, "job_id": self.job_id, **fields})

    def raw(self, line: str) -> None:
        """Append one line the CLI emitted, verbatim.

        Kept verbatim rather than re-serialised: this is the evidence, and a
        parse that changes it is a parse that can lose something. Unparseable
        lines are wrapped so the file stays valid JSONL.
        """
        text = (line or "").strip()
        if not text:
            return
        self._lines.append(text)
        try:
            json.loads(text)
            self._append(text)
        except ValueError:
            self._write({"ts": self._clock(), "kind": "stdout", "text": text[:4000]})

    def _write(self, obj: dict) -> None:
        try:
            self._append(json.dumps(obj, default=str))
        except (TypeError, ValueError):
            pass

    def _append(self, text: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(text + "\n")
            self._fh.flush()  # as-it-goes: a killed run keeps everything so far
        except OSError:
            self._fh = None

    # ----------------------------------------------------------------- finish #
    def finish(self, exit_code: int | None, status: str, stderr: str = "") -> dict:
        """Write the summary record and close. Returns the summary."""
        summary = summarise(self._lines)
        summary.update({
            "kind": "summary",
            "ts": self._clock(),
            "job_id": self.job_id,
            "label": self.label,
            "status": status,
            "exit_code": exit_code,
            "duration_s": round(self._clock() - self._started, 1),
            "stderr_tail": (stderr or "")[-1000:],
        })
        self._write(summary)
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        return summary


def summarise(lines: list[str]) -> dict:
    """Derive the signals worth acting on from a run's stream-json events.

    These are chosen to answer "where is the agent struggling": which tools it
    reached for, which of them came back as errors, how many turns it took, and
    whether it ever pushed. A run that used 40 turns and 12 failed Bash calls to
    change nothing is visible here without reading the transcript.

    Pure and defensive — a partial or truncated file still summarises.
    """
    tools: Counter = Counter()
    tool_errors: Counter = Counter()
    # tool_use id -> name. A tool_result carries only the id, so without this
    # every failure is filed under "tool" and the question the summary exists to
    # answer — WHICH tool keeps failing — cannot be asked.
    name_by_id: dict[str, str] = {}
    turns = 0
    result_text = ""
    cost_usd = None
    error_lines = 0
    pushed = False

    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "assistant":
            turns += 1
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")
                    tools[name] += 1
                    if block.get("id"):
                        name_by_id[str(block["id"])] = name
                    if name == "Bash":
                        command = str((block.get("input") or {}).get("command") or "")
                        if "git push" in command:
                            pushed = True
        elif etype == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if block.get("is_error"):
                        which = name_by_id.get(str(block.get("tool_use_id") or ""),
                                               str(block.get("name") or "tool"))
                        tool_errors[which] += 1
                        error_lines += 1
        elif etype == "result":
            result_text = str(event.get("result") or "")[:2000]
            cost_usd = event.get("total_cost_usd", cost_usd)

    return {
        "turns": turns,
        "tool_calls": dict(tools),
        "tool_call_total": sum(tools.values()),
        "tool_errors": dict(tool_errors),
        "tool_error_total": error_lines,
        "pushed": pushed,
        "cost_usd": cost_usd,
        "result_tail": result_text[-600:],
    }
