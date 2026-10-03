"""Forgejo adapter — the tracker port, plus the verbs the fixer needs.

``ForgejoClient`` satisfies ``app.afk.tracker.GitHubClient`` (a structural
Protocol, so no subclassing) against Forgejo's Gitea-compatible API, which lets
the whole existing loop — ``Tracker``, ``dispatch_policy``, ``watcher`` — run
against Forgejo with nothing else changed. It adds the verbs the fixer needs
beyond that port: creating a follow-up issue, reading an issue and its comments
(the run's own memory, see the design doc), assigning an escalation, and
answering who is trusted on a repo.

Three details of Forgejo's API shape the code:

  * **Labels are ids, not names.** ``POST /issues/{n}/labels`` and the delete
    path both take numeric ids, so the client keeps a name→id index per repo and
    refreshes it once when asked for a name it has not seen — a label created
    after the process started still resolves.
  * **There is no ``author_association``.** The tracker's trust gate reads that
    field, so the client synthesizes it from the repo's collaborator list plus
    the owner: ``OWNER`` / ``COLLABORATOR`` / ``NONE``. That keeps
    ``Tracker._is_labeled_by_trusted`` working unchanged and fail-closed.
  * **The issues endpoint also returns pull requests.** They are filtered out, so
    the loop never treats a PR as a dispatchable issue.

Transport is injected. Production passes nothing and gets a stdlib
``urllib`` sender (the image carries no HTTP library beyond the stdlib, and no
``gh``); tests pass a fake and assert the exact request without a socket.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

# (method, url, body|None) -> (status, raw bytes)
Transport = Callable[[str, str, dict | None], tuple[int, bytes]]

_ASSOCIATION_OWNER = "OWNER"
_ASSOCIATION_COLLABORATOR = "COLLABORATOR"
_ASSOCIATION_NONE = "NONE"

# Gitea encodes a label ADD as comment type "label" with content "1", and a
# removal as the same type with empty content. The timeline exposes content as
# ``body``.
_LABEL_ADDED_BODY = "1"


def _urllib_transport(token: str) -> Transport:
    """The production sender: stdlib only, 30s timeout, token auth."""

    def send(method: str, url: str, body: dict | None) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"token {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    return send


class ForgejoClient:
    """Read/write access to one Forgejo owner's repos."""

    def __init__(
        self,
        base_url: str,
        token: str,
        owner: str,
        transport: Transport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._owner = owner
        self._send: Transport = transport or _urllib_transport(token)
        self._label_ids: dict[str, dict[str, int]] = {}
        self._trusted: dict[str, frozenset[str]] = {}

    # ------------------------------------------------------------ plumbing #
    def _slug(self, repo: str) -> str:
        return f"{self._owner}/{repo}"

    def _call(self, method: str, path: str, body: dict | None = None):
        status, raw = self._send(method, self._base + path, body)
        if status >= 400:
            detail = raw[:200].decode("utf-8", "replace") if raw else ""
            raise RuntimeError(f"forgejo {method} {path} -> HTTP {status} {detail}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # -------------------------------------------------- the tracker port #
    def list_issues(self, repo: str, label: str) -> list[dict]:
        """Open issues carrying ``label``. Pull requests are excluded."""
        q = urllib.parse.urlencode({"state": "open", "labels": label, "limit": 100})
        raw = self._call("GET", f"/repos/{self._slug(repo)}/issues?{q}") or []
        return [i for i in raw if not i.get("pull_request")]

    def label_events(self, repo: str, number: int) -> list[dict]:
        """Label ADDITIONS on this issue, in the port's GitHub-timeline shape.

        ``author_association`` is synthesized from the repo's trusted set, since
        Forgejo does not carry the field. Chronological order is preserved, which
        is what makes the tracker's "most recent application wins" rule correct.
        """
        q = urllib.parse.urlencode({"limit": 100})
        raw = self._call("GET", f"/repos/{self._slug(repo)}/issues/{number}/timeline?{q}") or []
        trusted = self.trusted_actors(repo)
        out: list[dict] = []
        for entry in raw:
            if entry.get("type") != "label":
                continue
            if str(entry.get("body") or "") != _LABEL_ADDED_BODY:
                continue
            label = entry.get("label") or {}
            actor = str((entry.get("user") or {}).get("login") or "")
            out.append({
                "event": "labeled",
                "label": {"name": label.get("name")},
                "author_association": self._association(actor, trusted),
            })
        return out

    def add_label(self, repo: str, number: int, label: str) -> None:
        lid = self._label_id(repo, label)
        self._call("POST", f"/repos/{self._slug(repo)}/issues/{number}/labels",
                   {"labels": [lid]})

    def remove_label(self, repo: str, number: int, label: str) -> None:
        lid = self._label_id(repo, label)
        self._call("DELETE", f"/repos/{self._slug(repo)}/issues/{number}/labels/{lid}")

    def comment(self, repo: str, number: int, body: str) -> None:
        self._call("POST", f"/repos/{self._slug(repo)}/issues/{number}/comments",
                   {"body": body})

    def close(self, repo: str, number: int) -> None:
        self._call("PATCH", f"/repos/{self._slug(repo)}/issues/{number}",
                   {"state": "closed"})

    # ------------------------------------------------ beyond the port #
    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str] | None = None
    ) -> int:
        """File an issue; returns its number. Labels are resolved to ids."""
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = [self._label_id(repo, name) for name in labels]
        out = self._call("POST", f"/repos/{self._slug(repo)}/issues", payload) or {}
        return int(out.get("number") or 0)

    def get_issue(self, repo: str, number: int) -> dict:
        return self._call("GET", f"/repos/{self._slug(repo)}/issues/{number}") or {}

    def list_comments(self, repo: str, number: int) -> list[dict]:
        """Every comment on the issue, oldest first — the run's own memory."""
        q = urllib.parse.urlencode({"limit": 100})
        return self._call("GET", f"/repos/{self._slug(repo)}/issues/{number}/comments?{q}") or []

    def assign(self, repo: str, number: int, assignee: str) -> None:
        self._call("PATCH", f"/repos/{self._slug(repo)}/issues/{number}",
                   {"assignees": [assignee]})

    def trusted_actors(self, repo: str) -> frozenset[str]:
        """Who may trigger on this repo: its collaborators plus its owner.

        Cached per process. The set changes rarely and a stale answer fails in the
        safe direction — a newly-added collaborator is refused until the next
        pod, rather than a removed one being admitted.
        """
        if repo not in self._trusted:
            raw = self._call("GET", f"/repos/{self._slug(repo)}/collaborators") or []
            logins = {str(c.get("login")) for c in raw if c.get("login")}
            self._trusted[repo] = frozenset(logins | {self._owner})
        return self._trusted[repo]

    # ---------------------------------------------------------- helpers #
    def _association(self, actor: str, trusted: frozenset[str]) -> str:
        if actor and actor == self._owner:
            return _ASSOCIATION_OWNER
        if actor and actor in trusted:
            return _ASSOCIATION_COLLABORATOR
        return _ASSOCIATION_NONE

    def _label_id(self, repo: str, name: str) -> int:
        """Resolve a label name to its id, refreshing the index once on a miss."""
        index = self._label_ids.get(repo)
        if index is None or name not in index:
            index = self._load_label_index(repo)
        try:
            return index[name]
        except KeyError:
            raise KeyError(
                f"label {name!r} does not exist on {self._slug(repo)}"
            ) from None

    def _load_label_index(self, repo: str) -> dict[str, int]:
        q = urllib.parse.urlencode({"limit": 100})
        raw = self._call("GET", f"/repos/{self._slug(repo)}/labels?{q}") or []
        index = {str(lbl["name"]): int(lbl["id"]) for lbl in raw if lbl.get("name")}
        self._label_ids[repo] = index
        return index
