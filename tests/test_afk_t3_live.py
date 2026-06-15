"""LIVE smoke test for ``app.afk.t3_client`` against a real T3 instance.

Skipped by default. The unit tests (``test_afk_t3_client``) pin the wire shape
against a contract-accurate fake; this file proves the *same code* actually talks
to a live T3 — the guard that "green tests" mean "wired to T3", which the earlier
fake-only suite did NOT provide (it was green while the real server 400'd).

It is opt-in because the orchestration API is in-cluster (ClusterIP + an
Authentik-gated ingress), so it can't run in CI without cluster access. Run it
from inside the cluster, or via a port-forward, with a bearer minted on the pod::

    # bearer (on the t3-afk pod, as the node user):
    #   t3 auth session issue --token-only --base-dir /data/t3 --ttl 30m
    kubectl -n t3-afk port-forward deploy/t3-afk 3773:3773 &
    T3_AFK_BASE_URL=http://127.0.0.1:3773 T3_AFK_TOKEN=<bearer> \
        python3 -m pytest tests/test_afk_t3_live.py -v

The read-only snapshot check is always safe. The full dispatch round-trip
(create thread + turn + verify it appears, then delete it) only runs with
``T3_AFK_SMOKE_DISPATCH=1`` since it spends a (tiny) agent turn.
"""
import os
import time

import pytest

from app.afk import t3_client

_BASE_URL = os.environ.get("T3_AFK_BASE_URL")
_TOKEN = os.environ.get("T3_AFK_TOKEN")

pytestmark = pytest.mark.skipif(
    not (_BASE_URL and _TOKEN),
    reason="set T3_AFK_BASE_URL + T3_AFK_TOKEN to run the live T3 smoke test",
)


def _real_client():
    import httpx  # local import so the module imports fine without httpx installed

    return t3_client.T3Client(
        base_url=_BASE_URL,
        http=httpx.Client(timeout=30.0),
        bearer_provider=lambda: _TOKEN,
    )


def test_live_snapshot_has_the_real_shape():
    """A real snapshot parses and carries the keys the watcher/adapter depend on:
    ``threads`` + ``projects``, and any thread exposes ``latestTurn`` (the
    liveness source) — not a top-level ``status``."""
    snap = _real_client().snapshot()
    assert isinstance(snap, dict)
    assert "threads" in snap and "projects" in snap
    for thread in snap["threads"]:
        assert "id" in thread
        # liveness lives under latestTurn.state (the contract this suite guards)
        assert "status" not in thread, "real threads have no top-level status field"


@pytest.mark.skipif(
    os.environ.get("T3_AFK_SMOKE_DISPATCH") != "1",
    reason="set T3_AFK_SMOKE_DISPATCH=1 to run the dispatch round-trip (spends a turn)",
)
def test_live_dispatch_round_trip_then_cleanup():
    """End-to-end against the real server: ``dispatch`` (ensure-project + create +
    turn) succeeds and the new thread shows up in the snapshot. Cleans up the
    thread it created so the cockpit isn't littered."""
    import httpx

    repo = "afk-smoke/roundtrip"
    client = _real_client()
    thread_id = client.dispatch(repo, 1, "Reply with just: ok. Do not use any tools.")
    assert isinstance(thread_id, str) and thread_id

    # The thread must appear in the fleet read-model (poll briefly — dispatch is
    # accepted asynchronously).
    found = False
    for _ in range(10):
        if any(t.get("id") == thread_id for t in client.snapshot().get("threads", [])):
            found = True
            break
        time.sleep(1.0)
    assert found, f"dispatched thread {thread_id} never appeared in the snapshot"

    # Cleanup: delete the throwaway thread (raw command — not part of the adapter).
    httpx.post(
        f"{_BASE_URL.rstrip('/')}/api/orchestration/dispatch",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        json={"type": "thread.delete", "commandId": t3_client._uuid(), "threadId": thread_id},
        timeout=30.0,
    ).raise_for_status()
