"""Tests for ``app.fixer.checklist_tracker.ChecklistCollapsingTracker``.

It wraps a tracker and forwards a hand-written list of methods, so its risk is
not what it does but what it forgets: a capability added to the inner tracker
does not reach the caller unless a passthrough is added here too. The dispatch
lock reads one such capability through ``getattr`` and falls back silently when
it is missing, so an omission degrades behaviour instead of raising.
"""
from app.fixer import checklist_tracker

# --------------------------------------------------------------------------- #
# Passthrough completeness.
#
# This wrapper forwards a fixed list of methods by hand, so a capability added
# to the inner tracker does not reach the poller unless it is added here too. The
# dispatch lock reads `list_in_progress` via getattr and silently falls back when
# it is absent, so a missing passthrough would not raise — it would just quietly
# restore the bug it was added to fix.
# --------------------------------------------------------------------------- #
def test_list_in_progress_reaches_the_inner_tracker():
    calls = []

    class Inner:
        def list_in_progress(self, repos, label):
            calls.append((list(repos), label))
            return ["sentinel"]

    wrapper = checklist_tracker.ChecklistCollapsingTracker(
        Inner(), object(), "infra-agent"
    )
    assert wrapper.list_in_progress(["infra"], "agent-in-progress") == ["sentinel"]
    assert calls == [(["infra"], "agent-in-progress")]


def test_the_wrapper_forwards_every_method_the_poller_and_watcher_use():
    """Names the surface explicitly, so adding a port method without a
    passthrough fails here instead of degrading quietly at runtime."""
    required = [
        "list_ready", "list_in_progress", "add_label", "remove_label",
        "comment", "close",
    ]
    missing = [name for name in required
               if not callable(getattr(
                   checklist_tracker.ChecklistCollapsingTracker, name, None))]
    assert missing == []
