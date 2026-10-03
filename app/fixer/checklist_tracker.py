"""A tracker that posts the progress checklist only when a box changes.

``watcher.tick`` posts a phase checklist on every tick, including a plain WAIT.
A tick runs every couple of minutes, so posting each one as a new comment would
bury the issue's real conversation under near-identical blocks.

This wrapper sits between the watcher and the tracker. A checklist is posted as a
new comment only when its box states differ from the last checklist the bot
posted on that issue; when they match, nothing is posted. Everything that is not
a checklist passes straight through, so findings, escalations and resolutions are
appended normally.

It used to edit the previous checklist in place and post a new one if the edit
failed. On infra#95 the PATCH returned 404 twelve times in two days while a GET
of the same comment returned 200, and each fallback posted a fresh identical
checklist, leaving 10 duplicates. Posting only on change needs no edit call, so
that failure mode is gone.

Identifying a checklist by its rendered heading keeps the coupling to one string
that ``phase_checklist`` owns, rather than threading a flag through the watcher's
signature.
"""
import re

# ``phase_checklist.render`` titles its block "### <repo>#<issue> — AFK run progress".
_CHECKLIST_MARKER = "AFK run progress"

# One box line as ``phase_checklist`` renders it: "- [x] label", "- [~] label",
# "- [ ] label".
_BOX = re.compile(r"^- \[([x~ ])\] (.*)$")


def is_checklist(body: str) -> bool:
    """Whether ``body`` is a rendered progress checklist."""
    first_line = (body or "").lstrip().split("\n", 1)[0]
    return first_line.startswith("###") and _CHECKLIST_MARKER in first_line


def box_states(body: str) -> list[tuple[str, str]]:
    """The ordered ``(label, state)`` pairs of every box line in ``body``.

    Every other line (the heading with its thread id, notes) is ignored, so two
    checklists compare equal exactly when their boxes do.
    """
    out = []
    for line in (body or "").splitlines():
        match = _BOX.match(line.strip())
        if match:
            out.append((match.group(2).strip(), match.group(1)))
    return out


class ChecklistCollapsingTracker:
    """Delegates to a tracker, dropping checklists whose boxes have not changed."""

    def __init__(self, inner, forgejo, bot_actor: str) -> None:
        self._inner = inner
        self._forgejo = forgejo
        self._bot = bot_actor

    # ------------------------------------------------------------- passthrough #
    def list_ready(self, repos):
        return self._inner.list_ready(repos)

    def list_in_progress(self, repos, label):
        # The dispatch lock reads this to find repos with a run already in
        # flight, and it reads it through getattr with a silent fallback — so
        # omitting this passthrough would not raise, it would quietly put the
        # lock back on the weaker ready-set derivation.
        return self._inner.list_in_progress(repos, label)

    def add_label(self, repo, issue, label):
        self._inner.add_label(repo, issue, label)

    def remove_label(self, repo, issue, label):
        self._inner.remove_label(repo, issue, label)

    def close(self, repo, issue):
        self._inner.close(repo, issue)

    def _to_issue(self, repo, raw):
        return self._inner._to_issue(repo, raw)  # noqa: SLF001

    # ---------------------------------------------------------------- the point #
    def comment(self, repo, issue, body):
        """Post ``body``, unless it is a checklist whose boxes match the last one."""
        if is_checklist(body):
            last = self._last_checklist(repo, issue)
            if last is not None and box_states(last) == box_states(body):
                return
        self._inner.comment(repo, issue, body)

    def _last_checklist(self, repo, issue) -> str | None:
        """The body of the bot's most recent checklist comment, if there is one."""
        for entry in reversed(self._forgejo.list_comments(repo, issue)):
            author = str((entry.get("user") or {}).get("login") or "")
            if author and author != self._bot:
                continue
            body = str(entry.get("body") or "")
            if is_checklist(body):
                return body
        return None
