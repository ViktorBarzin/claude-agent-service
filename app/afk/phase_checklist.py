"""Render an AFK run's progress as a live markdown checklist.

``render(current, meta)`` is a PURE function: it maps a ``Phase`` plus a bag of
optional context (``meta``) to a markdown task list, with no I/O and no hidden
state. The loop posts the result as an issue comment so a human glancing at the
tracker can see exactly how far an unattended run has got — worktree created,
test written, green, pushed, CI, deployed, done.

The list always shows all seven lifecycle phases in order. Phases strictly
*before* ``current`` are checked (``- [x]``); ``current`` is marked in-progress
(``- [~]``); later phases are empty (``- [ ]``). ``Phase.DONE`` is terminal — at
that point every line, including DONE itself, is checked.

``meta`` is best-effort decoration only. Recognised keys (all optional):
``repo`` / ``issue`` (header title), ``thread_id`` (header suffix), and
``fix_forward_attempts`` (a note line when non-zero). Unknown keys are ignored,
and a missing key never raises — the checklist degrades gracefully to just the
phase list. Nothing here mutates ``meta``.
"""
from typing import Any

from .types import Phase

# Lifecycle order — the single source of truth for both ordering and the
# checked/active/empty partition. Must stay in sync with ``Phase`` (the
# checklist tests assert every phase appears, so a divergence is caught).
_ORDER: tuple[Phase, ...] = (
    Phase.WORKTREE,
    Phase.TESTS_RED,
    Phase.GREEN,
    Phase.PUSHED,
    Phase.CI,
    Phase.DEPLOYED,
    Phase.DONE,
)

# Human-readable label per phase (what shows on each checklist line).
_LABELS: dict[Phase, str] = {
    Phase.WORKTREE: "Worktree created",
    Phase.TESTS_RED: "Failing test written (TDD red)",
    Phase.GREEN: "Implementation passing (TDD green)",
    Phase.PUSHED: "Pushed to master",
    Phase.CI: "CI green on pushed commit",
    Phase.DEPLOYED: "Deployed / rolled out",
    Phase.DONE: "Done — issue closed",
}

# Task-list markers. ``[~]`` (in-progress) is a common markdown convention and,
# crucially, is neither ``[x]`` nor ``[ ]`` so the active line is always visually
# distinct from a checked or empty box.
_DONE = "- [x]"
_ACTIVE = "- [~]"
_TODO = "- [ ]"


def render(current: Phase, meta: dict[str, Any]) -> str:
    """Render the run's progress checklist as markdown (see module docstring).

    ``current`` is the phase the run is in right now; ``meta`` supplies optional
    header/context fields. Pure: identical inputs yield byte-identical output and
    ``meta`` is never mutated.
    """
    current_index = _ORDER.index(current)
    is_done = current is Phase.DONE

    lines = [_header(meta), ""]
    for index, phase in enumerate(_ORDER):
        lines.append(f"{_marker(index, current_index, is_done)} {_LABELS[phase]}")

    note = _fix_forward_note(meta)
    if note is not None:
        lines.extend(["", note])

    # Trailing newline so the block sits cleanly when concatenated into a comment.
    return "\n".join(lines) + "\n"


def _marker(index: int, current_index: int, is_done: bool) -> str:
    """The checkbox marker for the phase at ``index`` given the current phase.

    Earlier phases are checked; the current phase is in-progress; later phases
    are empty. When the run is DONE, every phase (including DONE) is checked.
    """
    if is_done or index < current_index:
        return _DONE
    if index == current_index:
        return _ACTIVE
    return _TODO


def _header(meta: dict[str, Any]) -> str:
    """The ``###`` title line. Includes ``repo#issue`` when both are present and
    a ``(thread ...)`` suffix when a thread id is known; degrades to a bare title
    otherwise."""
    repo = meta.get("repo")
    issue = meta.get("issue")
    if repo is not None and issue is not None:
        title = f"{repo}#{issue} — AFK run progress"
    else:
        title = "AFK run progress"

    thread_id = meta.get("thread_id")
    if thread_id:
        title = f"{title} (thread {thread_id})"
    return f"### {title}"


def _fix_forward_note(meta: dict[str, Any]) -> str | None:
    """A note line when one or more fix-forward attempts have happened, else
    ``None`` (no line). Zero/absent attempts add nothing — the clean path stays
    uncluttered."""
    attempts = meta.get("fix_forward_attempts")
    if not attempts:
        return None
    plural = "attempt" if attempts == 1 else "attempts"
    return f"_Fix-forward: {attempts} {plural}._"
