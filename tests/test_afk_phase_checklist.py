"""Tests for ``app.afk.phase_checklist`` — the live progress checklist.

``render(current, meta)`` is PURE: same inputs → byte-identical markdown, no I/O.
It draws the seven-phase lifecycle (worktree → tests-red → green → pushed → CI →
deployed → done) as a markdown task list, with phases *before* ``current`` checked
off, ``current`` marked in-progress, and later phases left empty.

Style matches the existing suite: plain ``assert`` functions, parametrized cases,
and a couple of full-output snapshots so the rendered shape is pinned, not just
its line count.
"""
import pytest

from app.afk.phase_checklist import render
from app.afk.types import Phase


# Lifecycle order, mirrored from the contract so a reordering of the enum that
# the renderer didn't track shows up as a test failure rather than silent drift.
PHASES_IN_ORDER = [
    Phase.WORKTREE,
    Phase.TESTS_RED,
    Phase.GREEN,
    Phase.PUSHED,
    Phase.CI,
    Phase.DEPLOYED,
    Phase.DONE,
]


# --------------------------------------------------------------------------- #
# Structure: one line per phase, in order, always all seven.
# --------------------------------------------------------------------------- #
def _checklist_lines(out: str) -> list[str]:
    """The markdown task-list lines (``- [ ]`` / ``- [x]`` ...), in order."""
    return [ln for ln in out.splitlines() if ln.lstrip().startswith("- [")]


def test_renders_a_string():
    assert isinstance(render(Phase.WORKTREE, {}), str)


@pytest.mark.parametrize("current", PHASES_IN_ORDER)
def test_every_phase_has_exactly_one_checklist_line(current):
    lines = _checklist_lines(render(current, {}))
    assert len(lines) == len(PHASES_IN_ORDER)


@pytest.mark.parametrize("current", PHASES_IN_ORDER)
def test_checklist_lines_are_in_lifecycle_order(current):
    lines = _checklist_lines(render(current, {}))
    # Each phase's human label appears, and in the lifecycle order.
    positions = [
        next(i for i, ln in enumerate(lines) if _has_label(ln, phase))
        for phase in PHASES_IN_ORDER
    ]
    assert positions == sorted(positions)


def _has_label(line: str, phase: Phase) -> bool:
    """Whether a checklist line carries ``phase``'s headline word (case-insensitive
    substring — the test asserts the label is *present*, not its exact decoration)."""
    return _phase_label(phase).lower() in line.lower()


def _phase_label(phase: Phase) -> str:
    """The headline word(s) the renderer must use for a phase. Loose on purpose:
    the test asserts the label is *present*, not the exact decoration."""
    return {
        Phase.WORKTREE: "worktree",
        Phase.TESTS_RED: "test",
        Phase.GREEN: "green",
        Phase.PUSHED: "push",
        Phase.CI: "CI",
        Phase.DEPLOYED: "deploy",
        Phase.DONE: "done",
    }[phase]


# --------------------------------------------------------------------------- #
# Check/in-progress/empty partitioning around ``current``.
# --------------------------------------------------------------------------- #
def _classify(line: str) -> str:
    """Bucket a checklist line by its marker: 'done' ``[x]``, 'todo' ``[ ]``, or
    'active' (anything else, e.g. an in-progress glyph)."""
    body = line.lstrip()
    if body.startswith("- [x]"):
        return "done"
    if body.startswith("- [ ]"):
        return "todo"
    return "active"


@pytest.mark.parametrize("idx,current", list(enumerate(PHASES_IN_ORDER)))
def test_earlier_checked_current_active_later_empty(idx, current):
    lines = _checklist_lines(render(current, {}))
    buckets = [_classify(ln) for ln in lines]

    # Everything strictly before the current phase is checked off.
    assert all(b == "done" for b in buckets[:idx]), buckets

    if current is Phase.DONE:
        # Terminal phase: the whole list is checked, nothing left active/empty.
        assert all(b == "done" for b in buckets), buckets
    else:
        # The current phase is the single in-progress marker...
        assert buckets[idx] == "active", buckets
        assert buckets.count("active") == 1, buckets
        # ...and every phase after it is still an empty checkbox.
        assert all(b == "todo" for b in buckets[idx + 1 :]), buckets


def test_first_phase_has_nothing_checked_before_it():
    lines = _checklist_lines(render(Phase.WORKTREE, {}))
    assert _classify(lines[0]) == "active"
    assert "done" not in [_classify(ln) for ln in lines]


def test_done_checks_every_phase_including_done():
    lines = _checklist_lines(render(Phase.DONE, {}))
    assert all(_classify(ln) == "done" for ln in lines)
    # The DONE line itself is checked, not merely the ones before it.
    done_line = next(ln for ln in lines if _has_label(ln, Phase.DONE))
    assert _classify(done_line) == "done"


# --------------------------------------------------------------------------- #
# Active-phase emphasis: the current phase is visually distinguishable.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("current", [p for p in PHASES_IN_ORDER if p is not Phase.DONE])
def test_active_phase_line_differs_from_todo_and_done_markers(current):
    lines = _checklist_lines(render(current, {}))
    active = [ln for ln in lines if _classify(ln) == "active"]
    assert len(active) == 1
    # Not a plain checkbox in either state.
    assert not active[0].lstrip().startswith("- [x]")
    assert not active[0].lstrip().startswith("- [ ]")


# --------------------------------------------------------------------------- #
# meta rendering: optional context is surfaced, omission never explodes.
# --------------------------------------------------------------------------- #
def test_meta_empty_does_not_raise_and_still_lists_phases():
    out = render(Phase.GREEN, {})
    assert _checklist_lines(out)  # non-empty


def test_meta_issue_and_repo_appear_in_output():
    out = render(Phase.GREEN, {"repo": "infra", "issue": 42})
    assert "infra" in out
    assert "42" in out


def test_meta_thread_id_appears_when_present():
    out = render(Phase.PUSHED, {"thread_id": "thread-7"})
    assert "thread-7" in out


def test_meta_thread_id_absent_is_silent():
    out = render(Phase.PUSHED, {})
    assert "thread-" not in out


def test_meta_fix_forward_attempt_surfaced():
    out = render(Phase.CI, {"fix_forward_attempts": 3})
    assert "3" in out


def test_meta_unknown_keys_are_ignored():
    # An unexpected key must not crash or leak its raw value as a stray line.
    out = render(Phase.WORKTREE, {"totally_unknown_field": "should-not-appear"})
    assert "should-not-appear" not in out


# --------------------------------------------------------------------------- #
# Determinism + idempotence (it's pure).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("current", PHASES_IN_ORDER)
def test_render_is_deterministic(current):
    meta = {"repo": "infra", "issue": 9, "thread_id": "thread-1"}
    assert render(current, meta) == render(current, meta)


def test_render_does_not_mutate_meta():
    meta = {"repo": "infra", "issue": 1}
    before = dict(meta)
    render(Phase.GREEN, meta)
    assert meta == before


# --------------------------------------------------------------------------- #
# Snapshots: pin the exact rendered shape for two representative phases. If the
# format changes intentionally, update these strings; an accidental change to
# wording/markers/order fails here loudly.
# --------------------------------------------------------------------------- #
WORKTREE_SNAPSHOT = """\
### infra#7 — AFK run progress

- [~] Worktree created
- [ ] Failing test written (TDD red)
- [ ] Implementation passing (TDD green)
- [ ] Pushed to master
- [ ] CI green on pushed commit
- [ ] Deployed / rolled out
- [ ] Done — issue closed
"""


def test_snapshot_worktree_phase():
    out = render(Phase.WORKTREE, {"repo": "infra", "issue": 7})
    assert out == WORKTREE_SNAPSHOT


CI_SNAPSHOT = """\
### infra#7 — AFK run progress (thread thread-3)

- [x] Worktree created
- [x] Failing test written (TDD red)
- [x] Implementation passing (TDD green)
- [x] Pushed to master
- [~] CI green on pushed commit
- [ ] Deployed / rolled out
- [ ] Done — issue closed
"""


def test_snapshot_ci_phase_with_thread():
    out = render(Phase.CI, {"repo": "infra", "issue": 7, "thread_id": "thread-3"})
    assert out == CI_SNAPSHOT


DONE_SNAPSHOT = """\
### infra#7 — AFK run progress

- [x] Worktree created
- [x] Failing test written (TDD red)
- [x] Implementation passing (TDD green)
- [x] Pushed to master
- [x] CI green on pushed commit
- [x] Deployed / rolled out
- [x] Done — issue closed
"""


def test_snapshot_done_phase():
    out = render(Phase.DONE, {"repo": "infra", "issue": 7})
    assert out == DONE_SNAPSHOT
