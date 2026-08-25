"""Tests for ``app.fixer.forgejo.ForgejoClient`` — the Forgejo half of the
tracker port, and the extra verbs the fixer needs.

Every test drives an injected fake transport, so the suite asserts the exact
request the client makes (method, path, body) without a socket. That matters
more than usual here: Forgejo's label endpoints take label **ids**, not names,
so the client has to resolve names first — and a silent regression there would
look like "the fixer stopped labelling" in production rather than a test failure.
"""
import json

import pytest

from app.fixer.forgejo import ForgejoClient

BASE = "https://forgejo.example/api/v1"


class FakeTransport:
    """Records requests and replays canned responses keyed by ``METHOD path``."""

    def __init__(self, responses: dict[str, tuple[int, object]] | None = None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}

    def __call__(self, method: str, url: str, body: dict | None):
        path = url[len(BASE):] if url.startswith(BASE) else url
        self.calls.append((method, path, body))
        key = f"{method} {path.split('?')[0]}"
        status, payload = self.responses.get(key, (200, []))
        return status, json.dumps(payload).encode()

    def paths(self) -> list[str]:
        return [f"{m} {p}" for m, p, _ in self.calls]

    def count(self, method: str, path_prefix: str) -> int:
        """How many requests matched, ignoring the query string."""
        return sum(1 for m, p, _ in self.calls if m == method and p.startswith(path_prefix))

    def body_for(self, method: str, path_prefix: str) -> dict | None:
        for m, p, b in self.calls:
            if m == method and p.startswith(path_prefix):
                return b
        raise AssertionError(f"no {method} {path_prefix} in {self.paths()}")


LABELS = [
    {"id": 11, "name": "broken"},
    {"id": 12, "name": "change"},
    {"id": 13, "name": "agent-in-progress"},
]


def make_client(responses: dict | None = None) -> tuple[ForgejoClient, FakeTransport]:
    base = {"GET /repos/viktor/infra/labels": (200, LABELS)}
    base.update(responses or {})
    t = FakeTransport(base)
    return ForgejoClient(BASE, "tok", "viktor", transport=t), t


# --------------------------------------------------------------------------- #
# The tracker port: reads.
# --------------------------------------------------------------------------- #
def test_list_issues_asks_for_open_issues_with_the_label():
    c, t = make_client({
        "GET /repos/viktor/infra/issues": (200, [
            {"number": 5, "labels": [{"name": "broken"}], "body": "boom"},
        ]),
    })
    out = c.list_issues("infra", "broken")
    assert out == [{"number": 5, "labels": [{"name": "broken"}], "body": "boom"}]
    path = t.paths()[0]
    assert path.startswith("GET /repos/viktor/infra/issues?")
    assert "state=open" in path and "labels=broken" in path


def test_list_issues_drops_pull_requests():
    """Forgejo returns PRs from the issues endpoint; the loop must not see them."""
    c, _ = make_client({
        "GET /repos/viktor/infra/issues": (200, [
            {"number": 5, "labels": [], "body": ""},
            {"number": 6, "labels": [], "body": "", "pull_request": {"merged": False}},
        ]),
    })
    assert [i["number"] for i in c.list_issues("infra", "broken")] == [5]


def test_label_events_maps_the_timeline_to_the_ports_shape():
    c, _ = make_client({
        "GET /repos/viktor/infra/issues/5/timeline": (200, [
            {"type": "comment", "body": "hello", "user": {"login": "ebarzin"}},
            {"type": "label", "body": "1", "label": {"name": "broken"},
             "user": {"login": "ebarzin"}},
        ]),
        "GET /repos/viktor/infra/collaborators": (200, [{"login": "ebarzin"}]),
    })
    assert c.label_events("infra", 5) == [{
        "event": "labeled",
        "label": {"name": "broken"},
        "author_association": "COLLABORATOR",
    }]


def test_label_events_marks_the_repo_owner_as_owner():
    c, _ = make_client({
        "GET /repos/viktor/infra/issues/5/timeline": (200, [
            {"type": "label", "body": "1", "label": {"name": "broken"},
             "user": {"login": "viktor"}},
        ]),
        "GET /repos/viktor/infra/collaborators": (200, []),
    })
    assert c.label_events("infra", 5)[0]["author_association"] == "OWNER"


def test_label_events_leaves_a_stranger_untrusted():
    c, _ = make_client({
        "GET /repos/viktor/infra/issues/5/timeline": (200, [
            {"type": "label", "body": "1", "label": {"name": "broken"},
             "user": {"login": "stranger"}},
        ]),
        "GET /repos/viktor/infra/collaborators": (200, [{"login": "ebarzin"}]),
    })
    assert c.label_events("infra", 5)[0]["author_association"] == "NONE"


def test_label_events_ignores_label_removals():
    """Gitea encodes add as body == "1" and removal as an empty body."""
    c, _ = make_client({
        "GET /repos/viktor/infra/issues/5/timeline": (200, [
            {"type": "label", "body": "", "label": {"name": "broken"},
             "user": {"login": "viktor"}},
        ]),
        "GET /repos/viktor/infra/collaborators": (200, []),
    })
    assert c.label_events("infra", 5) == []


# --------------------------------------------------------------------------- #
# The tracker port: mutations.
# --------------------------------------------------------------------------- #
def test_add_label_resolves_the_name_to_an_id():
    c, t = make_client()
    c.add_label("infra", 5, "agent-in-progress")
    assert t.body_for("POST", "/repos/viktor/infra/issues/5/labels") == {"labels": [13]}


def test_add_label_caches_the_label_index():
    c, t = make_client()
    c.add_label("infra", 5, "broken")
    c.add_label("infra", 6, "change")
    assert t.count("GET", "/repos/viktor/infra/labels") == 1


def test_add_label_refreshes_the_index_once_for_an_unknown_name():
    """A label created after the cache was warmed must still resolve."""
    c, t = make_client()
    c.add_label("infra", 5, "broken")
    t.responses["GET /repos/viktor/infra/labels"] = (
        200, LABELS + [{"id": 99, "name": "sev1"}],
    )
    c.add_label("infra", 5, "sev1")
    assert t.body_for("POST", "/repos/viktor/infra/issues/5/labels")
    assert t.calls[-1][2] == {"labels": [99]}


def test_add_label_raises_for_a_label_that_does_not_exist():
    c, _ = make_client()
    with pytest.raises(KeyError, match="nonexistent"):
        c.add_label("infra", 5, "nonexistent")


def test_remove_label_deletes_by_id():
    c, t = make_client()
    c.remove_label("infra", 5, "agent-in-progress")
    assert t.paths()[-1] == "DELETE /repos/viktor/infra/issues/5/labels/13"


def test_comment_posts_the_body():
    c, t = make_client()
    c.comment("infra", 5, "investigating")
    assert t.body_for("POST", "/repos/viktor/infra/issues/5/comments") == {"body": "investigating"}


def test_close_patches_the_state():
    c, t = make_client()
    c.close("infra", 5)
    assert t.paths()[-1] == "PATCH /repos/viktor/infra/issues/5"
    assert t.body_for("PATCH", "/repos/viktor/infra/issues/5") == {"state": "closed"}


# --------------------------------------------------------------------------- #
# The extra verbs the fixer needs beyond the port.
# --------------------------------------------------------------------------- #
def test_create_issue_sends_title_body_and_label_ids():
    c, t = make_client({"POST /repos/viktor/infra/issues": (201, {"number": 31})})
    number = c.create_issue("infra", "still broken", "the rest of it", ["broken"])
    assert number == 31
    assert t.body_for("POST", "/repos/viktor/infra/issues") == {
        "title": "still broken", "body": "the rest of it", "labels": [11],
    }


def test_get_issue_returns_the_raw_object():
    c, _ = make_client({"GET /repos/viktor/infra/issues/5": (200, {"number": 5, "title": "x"})})
    assert c.get_issue("infra", 5)["title"] == "x"


def test_list_comments_returns_bodies_in_order():
    c, _ = make_client({
        "GET /repos/viktor/infra/issues/5/comments": (200, [
            {"body": "first", "user": {"login": "infra-agent"}},
            {"body": "second", "user": {"login": "infra-agent"}},
        ]),
    })
    assert [x["body"] for x in c.list_comments("infra", 5)] == ["first", "second"]


def test_assign_puts_the_assignee():
    c, t = make_client()
    c.assign("infra", 5, "viktor")
    assert t.body_for("PATCH", "/repos/viktor/infra/issues/5") == {"assignees": ["viktor"]}


def test_trusted_actors_is_collaborators_plus_the_owner():
    c, _ = make_client({
        "GET /repos/viktor/infra/collaborators": (200, [
            {"login": "ebarzin"}, {"login": "infra-agent"},
        ]),
    })
    assert c.trusted_actors("infra") == frozenset({"viktor", "ebarzin", "infra-agent"})


def test_trusted_actors_is_cached():
    c, t = make_client({"GET /repos/viktor/infra/collaborators": (200, [{"login": "ebarzin"}])})
    c.trusted_actors("infra")
    c.trusted_actors("infra")
    assert t.count("GET", "/repos/viktor/infra/collaborators") == 1


# --------------------------------------------------------------------------- #
# Failure handling.
# --------------------------------------------------------------------------- #
def test_a_failing_request_raises_with_the_status():
    c, _ = make_client({"POST /repos/viktor/infra/issues/5/comments": (403, {"message": "no"})})
    with pytest.raises(RuntimeError, match="403"):
        c.comment("infra", 5, "hi")


def test_an_empty_body_is_not_a_parse_error():
    """204 with no body is the normal answer to a DELETE."""
    c, _ = make_client({"DELETE /repos/viktor/infra/issues/5/labels/13": (204, None)})
    c.remove_label("infra", 5, "agent-in-progress")
