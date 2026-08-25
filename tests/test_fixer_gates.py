"""Tests for ``app.fixer.gates`` — the pure webhook admission matrix.

The function under test reads only its arguments, so every test here is an
in-memory call. The suite walks each gate in the order the receiver relies on,
because the ORDER is part of the contract: a paused issue labelled by the bot
must report ``OWN_ACTION`` (loop guard first) so a log line never blames the
brake for something the bot did, and a delivery that is not a trigger at all
must never reach the trust check.
"""
import pytest

from app.afk.types import Config
from app.fixer.gates import Delivery, Verdict, decide, parse_delivery

TRIGGER = "broken"
BOT = "infra-agent"
TRUSTED = frozenset({"viktor", "ebarzin"})


def make_config(**kw) -> Config:
    """An ARMED config by default — the shipped default is disabled, and these
    tests are about the other gates. Kill-switch behaviour is asserted explicitly.
    """
    return Config(
        allowlist=kw.pop("allowlist", ["infra"]),
        kill_switch=kw.pop("kill_switch", False),
        **kw,
    )


def make_delivery(**kw) -> Delivery:
    base = dict(
        event="issues",
        action="opened",
        repo="infra",
        number=42,
        actor="ebarzin",
        labels=frozenset({TRIGGER}),
    )
    base.update(kw)
    return Delivery(**base)


def verdict(delivery: Delivery, config: Config | None = None, **kw) -> Verdict:
    return decide(
        delivery,
        config or make_config(),
        trigger_label=TRIGGER,
        bot_actor=BOT,
        trusted_actors=TRUSTED,
        **kw,
    )


# --------------------------------------------------------------------------- #
# The happy path, by both routes in.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("action", ["opened", "label_updated"])
@pytest.mark.parametrize("event", ["issues", "issue_label"])
def test_trigger_label_from_a_trusted_actor_dispatches(event, action):
    assert verdict(make_delivery(event=event, action=action)) is Verdict.DISPATCH


def test_extra_labels_do_not_block_the_trigger():
    d = make_delivery(labels=frozenset({TRIGGER, "sev2", "incident"}))
    assert verdict(d) is Verdict.DISPATCH


# --------------------------------------------------------------------------- #
# Gate 1 — event shape.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("event", ["push", "issue_comment", "pull_request", ""])
def test_non_issue_events_are_not_triggers(event):
    assert verdict(make_delivery(event=event)) is Verdict.NOT_A_TRIGGER


@pytest.mark.parametrize("action", ["closed", "reopened", "edited", "assigned", ""])
def test_non_triggering_actions_are_not_triggers(action):
    assert verdict(make_delivery(action=action)) is Verdict.NOT_A_TRIGGER


def test_the_change_label_alone_never_dispatches():
    assert verdict(make_delivery(labels=frozenset({"change"}))) is Verdict.NOT_A_TRIGGER


def test_an_unlabelled_issue_never_dispatches():
    assert verdict(make_delivery(labels=frozenset())) is Verdict.NOT_A_TRIGGER


def test_a_closed_issue_never_dispatches():
    assert verdict(make_delivery(state="closed")) is Verdict.NOT_A_TRIGGER


# --------------------------------------------------------------------------- #
# Gate 2 — the loop guard, which must win over every later gate.
# --------------------------------------------------------------------------- #
def test_the_bots_own_labelling_is_ignored():
    assert verdict(make_delivery(actor=BOT)) is Verdict.OWN_ACTION


def test_loop_guard_precedes_the_brake_and_the_kill_switch():
    d = make_delivery(actor=BOT, labels=frozenset({TRIGGER, "paused"}))
    assert verdict(d, make_config(kill_switch=True)) is Verdict.OWN_ACTION


# --------------------------------------------------------------------------- #
# Gates 3-6 — the brakes, trust, enrolment.
# --------------------------------------------------------------------------- #
def test_the_paused_label_stops_dispatch():
    d = make_delivery(labels=frozenset({TRIGGER, "paused"}))
    assert verdict(d) is Verdict.PAUSED


def test_paused_precedes_the_kill_switch_so_the_log_names_the_narrower_brake():
    d = make_delivery(labels=frozenset({TRIGGER, "paused"}))
    assert verdict(d, make_config(kill_switch=True)) is Verdict.PAUSED


def test_the_kill_switch_stops_dispatch():
    assert verdict(make_delivery(), make_config(kill_switch=True)) is Verdict.KILL_SWITCH


def test_a_shipped_default_config_dispatches_nothing():
    """The disabled-by-default posture: no allowlist and the switch on."""
    disabled = Config(allowlist=[], kill_switch=True)
    assert verdict(make_delivery(), disabled) is Verdict.KILL_SWITCH


def test_an_untrusted_actor_is_refused():
    assert verdict(make_delivery(actor="stranger")) is Verdict.UNTRUSTED_ACTOR


def test_an_unattributable_actor_is_refused():
    """Fail-closed: an empty actor is never in the trusted set."""
    assert verdict(make_delivery(actor="")) is Verdict.UNTRUSTED_ACTOR


def test_an_unenrolled_repo_is_refused():
    assert verdict(make_delivery(repo="terminal-lobby")) is Verdict.REPO_NOT_ENROLLED


def test_an_empty_allowlist_admits_nothing():
    cfg = make_config(allowlist=[])
    assert verdict(make_delivery(), cfg) is Verdict.REPO_NOT_ENROLLED


def test_a_custom_paused_label_is_honoured():
    d = make_delivery(labels=frozenset({TRIGGER, "on-hold"}))
    assert verdict(d, paused_label="on-hold") is Verdict.PAUSED


# --------------------------------------------------------------------------- #
# parse_delivery — the payload reduction.
# --------------------------------------------------------------------------- #
def _payload(**kw) -> dict:
    base = {
        "action": "label_updated",
        "issue": {
            "number": 7,
            "state": "open",
            "labels": [{"name": "broken"}, {"name": "sev2"}],
        },
        "repository": {"name": "infra", "full_name": "viktor/infra"},
        "sender": {"login": "ebarzin"},
    }
    base.update(kw)
    return base


def test_parse_delivery_reads_the_fields_the_gates_need():
    d = parse_delivery("issues", _payload())
    assert d is not None
    assert (d.event, d.action, d.repo, d.number, d.actor) == (
        "issues", "label_updated", "infra", 7, "ebarzin",
    )
    assert d.labels == frozenset({"broken", "sev2"})
    assert d.state == "open"


def test_parse_delivery_survives_a_payload_with_no_issue():
    assert parse_delivery("push", {"ref": "refs/heads/master"}) is None


def test_parse_delivery_tolerates_missing_optional_fields():
    d = parse_delivery("issues", {"issue": {"number": 3}})
    assert d is not None
    assert (d.actor, d.repo, d.labels, d.state) == ("", "", frozenset(), "open")


def test_parse_delivery_ignores_malformed_label_entries():
    p = _payload()
    p["issue"]["labels"] = [{"name": "broken"}, "not-a-dict", {"no_name": 1}]
    d = parse_delivery("issues", p)
    assert d is not None and d.labels == frozenset({"broken"})


def test_parse_delivery_repo_name_override_wins():
    d = parse_delivery("issues", _payload(), repo_name="override")
    assert d is not None and d.repo == "override"


def test_a_parsed_delivery_flows_through_the_gates():
    d = parse_delivery("issues", _payload())
    assert d is not None
    assert verdict(d) is Verdict.DISPATCH
