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


# --------------------------------------------------------------------------- #
# Post-on-change.
#
# A checklist is posted as a new comment only when its box states differ from
# the last checklist the bot posted on the issue. There is no edit-in-place:
# the PATCH path returned 404 in production and its fallback posted duplicates.
# --------------------------------------------------------------------------- #
BOT = "infra-agent"


def _checklist(states, thread="abc", note=None):
    labels = ["Diagnose", "Fix", "Verify"]
    lines = [f"### infra#95 — AFK run progress (thread {thread})", ""]
    lines += [f"- [{s}] {label}" for s, label in zip(states, labels)]
    if note:
        lines += ["", note]
    return "\n".join(lines) + "\n"


class _Inner:
    def __init__(self):
        self.posted = []

    def comment(self, repo, issue, body):
        self.posted.append((repo, issue, body))


class _Forgejo:
    """Only ``list_comments``; any other call (an edit) raises AttributeError."""

    def __init__(self, comments):
        self._comments = comments

    def list_comments(self, repo, number):
        return list(self._comments)


def _wrap(comments):
    inner = _Inner()
    wrapper = checklist_tracker.ChecklistCollapsingTracker(
        inner, _Forgejo(comments), BOT
    )
    return wrapper, inner


def _c(body, author=BOT, cid=1):
    return {"id": cid, "body": body, "user": {"login": author}}


def test_box_states_reads_label_and_state_in_order():
    body = _checklist("x~ ", note="Fix-forward attempts: 1")
    assert checklist_tracker.box_states(body) == [
        ("Diagnose", "x"), ("Fix", "~"), ("Verify", " "),
    ]


def test_first_checklist_on_an_issue_is_posted():
    wrapper, inner = _wrap([_c("a human comment", author="viktor")])
    body = _checklist("~  ")
    wrapper.comment("infra", 95, body)
    assert inner.posted == [("infra", 95, body)]


def test_same_box_states_as_last_bot_checklist_posts_nothing():
    wrapper, inner = _wrap([_c(_checklist("x~ "))])
    wrapper.comment("infra", 95, _checklist("x~ "))
    assert inner.posted == []


def test_one_changed_box_posts_a_new_comment():
    wrapper, inner = _wrap([_c(_checklist("x~ "))])
    body = _checklist("xx~")
    wrapper.comment("infra", 95, body)
    assert inner.posted == [("infra", 95, body)]


def test_comparison_uses_the_latest_bot_checklist():
    wrapper, inner = _wrap([
        _c(_checklist("~  "), cid=1),
        _c("a finding", cid=2),
        _c(_checklist("x~ "), cid=3),
    ])
    wrapper.comment("infra", 95, _checklist("x~ "))
    assert inner.posted == []


def test_non_checklist_body_passes_straight_through():
    wrapper, inner = _wrap([_c("same text")])
    wrapper.comment("infra", 95, "same text")
    assert inner.posted == [("infra", 95, "same text")]


def test_checklist_by_another_author_is_ignored_when_comparing():
    wrapper, inner = _wrap([_c(_checklist("x~ "), author="viktor")])
    body = _checklist("x~ ")
    wrapper.comment("infra", 95, body)
    assert inner.posted == [("infra", 95, body)]


def test_comparison_ignores_non_box_lines():
    wrapper, inner = _wrap([_c(_checklist("x~ ", thread="old"))])
    wrapper.comment(
        "infra", 95,
        _checklist("x~ ", thread="new", note="Fix-forward attempts: 2"),
    )
    assert inner.posted == []
