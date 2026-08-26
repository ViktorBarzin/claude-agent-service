"""One-shot ``/execute`` jobs behind the loop's T3-shaped ports.

The AFK loop was written against a T3 executor: it dispatches work and reads
liveness from a fleet snapshot. The fixer runs one-shot ``/execute`` jobs
instead (design doc, "Execution model"), so this adapter presents that job
runner in the shape ``poller.T3Port`` and ``watcher.SnapshotPort`` already
expect — ``dispatch(repo, issue, prompt) -> id`` plus ``snapshot() -> dict`` —
and the loop mechanics run unchanged.

The status mapping is where the one real decision lives:

    queued · running          -> "running"     (the watcher WAITs)
    completed                 -> "completed"   (turn finished; the state machine
                                                then judges pushed/CI)
    failed · error · timeout  -> "errored"     (escalate)
    unknown job id            -> "errored"

That last row closes a gap the design doc flagged as an open question. ``/execute``
registers a job in an in-process dict before it answers, so a job we dispatched
is always known to a live service; a job that has become unknown means the
process restarted and took the run with it. Reporting that as ``errored`` makes
the state machine escalate a run nobody is driving any more, rather than leaving
an ``agent-in-progress`` label parked forever behind a thread status of "no idea
yet".

Both I/O calls are injected, so the same class serves the in-process caller (the
webhook receiver, which starts jobs directly) and the CronJob (which reaches the
service over HTTP), and the tests use neither.
"""
from collections.abc import Callable

# submit(prompt) -> job_id
SubmitFn = Callable[[str], str]
# fetch(job_id) -> the job record, or None when the runner has never heard of it
FetchFn = Callable[[str], dict | None]

STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_ERRORED = "errored"
#: "I could not find out." The watcher does not recognise this string, so it
#: reads as "no status yet" and WAITs — which is the only safe reading when the
#: runner was momentarily unreachable. Distinct from ERRORED, which asserts the
#: run is dead; asserting that on a network blip escalates a healthy run.
STATE_UNKNOWN = "unknown"

#: What a fetch returns when the runner could not be reached at all, as opposed
#: to answering "no such job". The two are NOT the same and conflating them was
#: a real defect: a live run got escalated two minutes in.
UNREACHABLE = {"status": "__unreachable__"}

#: How a ``/execute`` job status reads as a turn state. Anything absent from
#: this map — including a status a future version adds — folds to ``errored``
#: rather than to "still running", so an unrecognised terminal state escalates
#: instead of hanging.
_TURN_STATE_BY_JOB_STATUS = {
    "__unreachable__": STATE_UNKNOWN,
    "queued": STATE_RUNNING,
    "running": STATE_RUNNING,
    "completed": STATE_COMPLETED,
    "failed": STATE_ERRORED,
    "error": STATE_ERRORED,
    "timeout": STATE_ERRORED,
}


class ExecuteClient:
    """The ``/execute`` job runner, wearing the loop's T3 ports."""

    def __init__(self, submit: SubmitFn, fetch: FetchFn) -> None:
        self._submit = submit
        self._fetch = fetch
        # Every job this adapter has dispatched, so ``snapshot`` can report a
        # job the runner has forgotten. Process-local by design: a restart loses
        # the list and the runner has lost the job too, which is the same fact.
        self._dispatched: list[str] = []
        # Set by the caller before dispatch so the run's log is named for the
        # issue; empty is fine and the runner falls back to its own default.
        self.label = ""

    def dispatch(self, repo: str, issue: int, prompt: str) -> str:
        """Start a run for ``repo#issue``; returns the job id.

        ``repo`` and ``issue`` are not passed to the runner — the prompt already
        names the issue, and ``/execute`` takes no routing fields. They are part
        of the port's signature, so they are accepted and used only for the
        error message if submission fails.
        """
        try:
            job_id = self._submit(prompt, self.label or f"{repo}#{issue}")
        except TypeError:
            # A submit that takes only a prompt (older wiring, and the tests'
            # simplest fake) still works — the label is a convenience.
            job_id = self._submit(prompt)
        if not job_id:
            raise RuntimeError(f"dispatch for {repo}#{issue} returned no job id")
        self._dispatched.append(job_id)
        return job_id

    def snapshot(self) -> dict:
        """Every dispatched job, in the fleet-snapshot shape the watcher reads."""
        threads = []
        for job_id in self._dispatched:
            threads.append({
                "id": job_id,
                "latestTurn": {"state": self.turn_state(job_id)},
            })
        return {"threads": threads}

    def track(self, job_id: str) -> None:
        """Adopt a job this process did not dispatch.

        The CronJob reads a run's job id out of the issue footer, so it has to
        tell the adapter about a job started by an earlier tick — otherwise
        ``snapshot`` would omit it and the watcher would see no status at all.
        """
        if job_id and job_id not in self._dispatched:
            self._dispatched.append(job_id)

    def turn_state(self, job_id: str) -> str:
        """One job's status as a turn state, with unknown folding to errored."""
        record = self._fetch(job_id)
        if not record:
            return STATE_ERRORED
        return _TURN_STATE_BY_JOB_STATUS.get(str(record.get("status")), STATE_ERRORED)
