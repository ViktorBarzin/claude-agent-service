"""A tracker that refreshes the progress checklist instead of repeating it.

``watcher.tick`` posts a phase checklist on every tick, including a plain WAIT.
That is the right intent — a reader should be able to see where a run is — but a
tick runs every couple of minutes, so posting it as a new comment each time would
bury the issue's real conversation under dozens of near-identical blocks.

This wrapper sits between the watcher and the tracker and makes the checklist
idempotent: the first one is posted, and every later one edits that same comment.
Everything that is not a checklist passes straight through, so the run's findings,
escalations and resolutions are appended normally.

Identifying a checklist by its rendered heading keeps the coupling to one string
that ``phase_checklist`` owns, rather than threading a flag through the watcher's
signature.
"""
import logging

# ``phase_checklist.render`` titles its block "### <repo>#<issue> — AFK run progress".
_CHECKLIST_MARKER = "AFK run progress"

log = logging.getLogger(__name__)


def is_checklist(body: str) -> bool:
    """Whether ``body`` is a rendered progress checklist."""
    first_line = (body or "").lstrip().split("\n", 1)[0]
    return first_line.startswith("###") and _CHECKLIST_MARKER in first_line


class ChecklistCollapsingTracker:
    """Delegates to a tracker, collapsing repeated checklists into one comment."""

    def __init__(self, inner, forgejo, bot_actor: str) -> None:
        self._inner = inner
        self._forgejo = forgejo
        self._bot = bot_actor

    # ------------------------------------------------------------- passthrough #
    def list_ready(self, repos):
        return self._inner.list_ready(repos)

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
        """Post ``body``, or edit the existing checklist when that is what it is."""
        if not is_checklist(body):
            self._inner.comment(repo, issue, body)
            return
        existing = self._find_checklist(repo, issue)
        if existing is None:
            self._inner.comment(repo, issue, body)
            return
        try:
            self._forgejo.edit_comment(repo, existing, body)
        except Exception as exc:
            # An edit that fails must not lose the update: fall back to posting.
            log.warning("checklist edit on %s#%s failed (%s) — posting instead",
                        repo, issue, exc)
            self._inner.comment(repo, issue, body)

    def _find_checklist(self, repo, issue) -> int | None:
        """The id of the bot's existing checklist comment, if there is one."""
        for entry in reversed(self._forgejo.list_comments(repo, issue)):
            author = str((entry.get("user") or {}).get("login") or "")
            if author and author != self._bot:
                continue
            if is_checklist(str(entry.get("body") or "")):
                return int(entry.get("id") or 0) or None
        return None
