"""Uncertain acknowledgements, crash recovery and restart equivalence (T30).

The commit sequence deliberately persists the transaction *before* the session's
committed transition, so a crash between them leaves a durable transaction and an
in-doubt session.  Recovery must resolve that by transaction id and must never
issue a second charge.
"""

from __future__ import annotations

import json

import pytest

from commerce.simulator import (
    COMMITTED,
    IN_DOUBT,
    IN_DOUBT_RECONCILE_FIRST,
    KNOWN_FAILURE,
    RECONCILE_REQUIRED,
    REFUSED,
    REPLAYED,
    UNCERTAIN,
    FaultKind,
    StoreCrashed,
)
from harness.clock import ManualClock
from harness.commerce import confirm, simulator_for, start_flow
from harness.scenarios import SHOPPER, TOKEN


def read_store(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Failure before any side effect
# --------------------------------------------------------------------------- #
def test_fail_before_write_is_a_known_failure_with_no_side_effect(catalog, fixture_clock, store_dir):
    flow = start_flow(catalog, fixture_clock, store_path=store_dir / "store.json")
    flow.sim.faults.push(FaultKind.FAIL_BEFORE_WRITE)

    result = confirm(flow)

    assert result.outcome == KNOWN_FAILURE
    assert flow.session.state == "DECISION_READY"
    assert flow.sim.inventory.get("NAVY-84").stock == 4
    assert [t.status for t in flow.sim.transactions()] == ["ABANDONED"]
    assert flow.sim.ledger.records == ()
    assert flow.sim.metrics(eligible_optins=1)["retainedOriginalGmvMinor"] == 0


def test_after_a_known_failure_the_shopper_retries_with_a_new_key(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.faults.push(FaultKind.FAIL_BEFORE_WRITE)
    assert confirm(flow).outcome == KNOWN_FAILURE

    assert flow.sim.choose(flow.session_id, "NAVY-84").accepted
    retried = confirm(flow, idempotency_key="idem-retry")

    assert retried.outcome == COMMITTED
    assert len([t for t in flow.sim.transactions() if t.status == "COMMITTED"]) == 1
    assert flow.sim.inventory.get("NAVY-84").stock == 3


# --------------------------------------------------------------------------- #
# T30 — acknowledgement lost
# --------------------------------------------------------------------------- #
def test_t30_lost_ack_parks_in_reconciling(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.faults.push(FaultKind.FAIL_AFTER_WRITE)

    result = confirm(flow)

    assert result.outcome == UNCERTAIN
    assert result.reason == IN_DOUBT
    assert result.required_action == RECONCILE_REQUIRED
    assert flow.session.state == "RECONCILING"
    # The write did happen exactly once.
    assert len(flow.sim.transactions()) == 1
    assert flow.sim.transactions()[0].status == "COMMITTED"
    assert flow.sim.inventory.get("NAVY-84").stock == 3


def test_t30_reconciliation_resolves_the_existing_transaction(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.faults.push(FaultKind.FAIL_AFTER_WRITE)
    first = confirm(flow)
    assert first.outcome == UNCERTAIN

    resolved = flow.sim.reconcile(flow.session_id)

    assert resolved.outcome == COMMITTED
    assert resolved.transaction_id == first.transaction_id
    assert flow.session.state == "EXCHANGE_COMMITTED"
    assert len(flow.sim.transactions()) == 1
    assert flow.sim.inventory.get("NAVY-84").stock == 3
    assert len(flow.sim.ledger.records) == 1


def test_t30_no_blind_retry_while_in_doubt(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.faults.push(FaultKind.FAIL_AFTER_WRITE)
    assert confirm(flow).outcome == UNCERTAIN

    # A *new* key must not start a second charge while the outcome is unknown.
    blocked = confirm(flow, idempotency_key="idem-second")
    assert blocked.outcome == REFUSED
    assert blocked.reason == IN_DOUBT_RECONCILE_FIRST
    assert len(flow.sim.transactions()) == 1
    assert flow.sim.inventory.get("NAVY-84").stock == 3


def test_t30_same_key_while_in_doubt_verifies_and_never_duplicates(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.faults.push(FaultKind.FAIL_AFTER_WRITE)
    first = confirm(flow)
    assert first.outcome == UNCERTAIN

    replay = confirm(flow)

    assert replay.outcome == REPLAYED
    assert replay.transaction_id == first.transaction_id
    assert flow.session.state == "EXCHANGE_COMMITTED"
    assert len(flow.sim.transactions()) == 1
    assert flow.sim.inventory.get("NAVY-84").stock == 3


def test_reconcile_is_refused_when_not_in_doubt(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    result = flow.sim.reconcile(flow.session_id)
    assert result.outcome == REFUSED
    assert result.reason == "WRONG_STATE"


# --------------------------------------------------------------------------- #
# Process death and restart
# --------------------------------------------------------------------------- #
def test_restart_after_crash_recovers_the_committed_transaction(catalog, store_dir):
    clock = ManualClock(catalog.clock)
    path = store_dir / "store.json"
    flow = start_flow(catalog, clock, store_path=path)
    flow.sim.faults.push(FaultKind.PERSIST_THEN_CRASH)

    with pytest.raises(StoreCrashed):
        confirm(flow)

    reopened = simulator_for(catalog, clock=clock, store_path=path)

    # Recovery parks the in-doubt session rather than repeating the write.
    session = reopened.session(flow.session_id)
    assert session.state == "RECONCILING"
    assert reopened.inventory.get("NAVY-84").stock == 3
    assert len(reopened.transactions()) == 1
    assert reopened.transactions()[0].status == "COMMITTED"

    resolved = reopened.reconcile(flow.session_id)

    assert resolved.outcome == COMMITTED
    assert reopened.session(flow.session_id).state == "EXCHANGE_COMMITTED"
    assert len(reopened.transactions()) == 1
    assert reopened.inventory.get("NAVY-84").stock == 3
    assert len(reopened.ledger.records) == 1
    assert reopened.ledger.records[0].retained_gmv_minor == 8400


def test_crash_recovery_never_charges_twice(catalog, store_dir):
    clock = ManualClock(catalog.clock)
    path = store_dir / "store.json"
    flow = start_flow(catalog, clock, store_path=path)
    flow.sim.faults.push(FaultKind.PERSIST_THEN_CRASH)
    with pytest.raises(StoreCrashed):
        confirm(flow)

    for _ in range(3):
        reopened = simulator_for(catalog, clock=clock, store_path=path)
        reopened.reconcile(flow.session_id)
        reopened.flush_ledger()
        assert reopened.inventory.get("NAVY-84").stock == 3
        assert len(reopened.transactions()) == 1
        assert len(reopened.ledger.records) == 1


def test_restart_reproduces_state_and_replays_idempotently(catalog, store_dir):
    clock = ManualClock(catalog.clock)
    path = store_dir / "store.json"
    flow = start_flow(catalog, clock, store_path=path)
    result = confirm(flow)
    assert result.outcome == COMMITTED
    before = flow.sim.to_state()

    reopened = simulator_for(catalog, clock=clock, store_path=path)
    after = reopened.to_state()

    assert before == after
    assert reopened.session(flow.session_id).state == "EXCHANGE_COMMITTED"
    replay = reopened.confirm_exchange(flow.request())
    assert replay.outcome == REPLAYED
    assert replay.transaction_id == result.transaction_id
    assert reopened.inventory.get("NAVY-84").stock == 3


def test_restart_before_any_commit_preserves_the_decision_state(catalog, store_dir):
    clock = ManualClock(catalog.clock)
    path = store_dir / "store.json"
    flow = start_flow(catalog, clock, store_path=path)
    assert flow.session.state == "QUOTE_READY"

    reopened = simulator_for(catalog, clock=clock, store_path=path)

    assert reopened.session(flow.session_id).state == "QUOTE_READY"
    assert reopened.session(flow.session_id).quote == flow.quote_ref
    committed = reopened.confirm_exchange(flow.request())
    assert committed.outcome == COMMITTED


def test_store_contains_no_media_or_urls(catalog, store_dir):
    clock = ManualClock(catalog.clock)
    path = store_dir / "store.json"
    flow = start_flow(catalog, clock, store_path=path)
    confirm(flow)

    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)

    assert set(payload) == {
        "formatVersion",
        "sessions",
        "orders",
        "transactions",
        "idempotency",
        "inventory",
        "ledger",
    }
    assert "http://" not in text and "https://" not in text
    assert "image" not in text.lower()
    assert "base64" not in text.lower()


def test_refund_handoff_survives_restart_as_a_zero_retained_outcome(catalog, store_dir):
    clock = ManualClock(catalog.clock)
    path = store_dir / "store.json"
    flow = start_flow(catalog, clock, store_path=path)
    refund = flow.sim.request_refund(
        flow.session_id, shopper_identity=SHOPPER, request_token=TOKEN
    )
    assert refund.outcome == "REFUND_HANDOFF_STARTED"

    reopened = simulator_for(catalog, clock=clock, store_path=path)

    assert reopened.session(flow.session_id).state == "REFUND_HANDOFF"
    records = reopened.ledger.records
    assert len(records) == 1
    assert records[0].outcome == "REFUND_HANDOFF"
    assert records[0].retained_gmv_minor == 0
    assert reopened.metrics(eligible_optins=1)["retainedOriginalGmvMinor"] == 0
