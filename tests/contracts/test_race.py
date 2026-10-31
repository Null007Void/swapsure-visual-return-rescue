"""Concurrency: the refund/exchange race (T17) and duplicate confirmations (T15).

These are real threads against a real re-entrant lock, not a model of one.  The
invariant under test is that the store never ends up with two payment actions for
one session, whichever side wins.
"""

from __future__ import annotations

import threading

from commerce.simulator import (
    REFUND_AFTER_COMMIT,
    REFUND_HANDOFF_STARTED,
    REFUND_RECORDED,
    REFUSED,
    REFUND_WON,
    StoreError,
)
from harness.clock import ManualClock
from harness.commerce import confirm, start_flow
from harness.scenarios import SHOPPER, TOKEN


def _race(catalog, iterations: int = 1):
    """Run one confirm/refund race per iteration and collect the outcomes."""

    observations = []
    for _ in range(iterations):
        clock = ManualClock(catalog.clock)
        flow = start_flow(catalog, clock)
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        errors: list[BaseException] = []

        def do_confirm() -> None:
            try:
                barrier.wait(timeout=5)
                results["confirm"] = confirm(flow)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        def do_refund() -> None:
            try:
                barrier.wait(timeout=5)
                results["refund"] = flow.sim.request_refund(
                    flow.session_id, shopper_identity=SHOPPER, request_token=TOKEN
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=do_confirm), threading.Thread(target=do_refund)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()

        assert not errors, errors
        observations.append((flow, results["confirm"], results["refund"]))
    return observations


def test_t17_refund_and_exchange_race_has_exactly_one_winner(catalog):
    observations = _race(catalog, iterations=25)

    winners: set[str] = set()
    for flow, confirmation, refund in observations:
        transactions = flow.sim.transactions()
        session = flow.sim.session(flow.session_id)

        if session.state == "EXCHANGE_COMMITTED":
            winners.add("exchange")
            assert confirmation.outcome == "COMMITTED"
            assert refund.outcome in (REFUND_AFTER_COMMIT, REFUND_RECORDED)
            assert len(transactions) == 1
            assert flow.sim.inventory.get("NAVY-84").stock == 3
        elif session.state == "REFUND_HANDOFF":
            winners.add("refund")
            assert refund.outcome == REFUND_HANDOFF_STARTED
            assert confirmation.outcome == REFUSED
            assert confirmation.reason == REFUND_WON
            assert transactions == ()
            assert flow.sim.inventory.get("NAVY-84").stock == 4
        else:  # pragma: no cover - would be a real defect
            raise AssertionError(f"unexpected terminal state {session.state}")

        committed = [r for r in flow.sim.ledger.records if r.outcome == "EXCHANGE_COMMITTED"]
        assert len(committed) <= 1
        if committed:
            assert sum(record.retained_gmv_minor for record in committed) == 8400
        else:
            assert all(record.retained_gmv_minor == 0 for record in flow.sim.ledger.records)

    assert winners <= {"exchange", "refund"}


def test_t17_refund_first_then_approval_is_refused_deterministically(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    refund = flow.sim.request_refund(
        flow.session_id, shopper_identity=SHOPPER, request_token=TOKEN
    )
    assert refund.outcome == REFUND_HANDOFF_STARTED
    assert flow.session.state == "REFUND_HANDOFF"

    blocked = confirm(flow)

    assert blocked.outcome == REFUSED
    assert blocked.reason == REFUND_WON
    assert flow.sim.transactions() == ()
    assert flow.sim.inventory.get("NAVY-84").stock == 4


def test_t15_concurrent_duplicate_clicks_commit_once(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    threads_count = 8
    barrier = threading.Barrier(threads_count)
    outcomes: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def click() -> None:
        try:
            barrier.wait(timeout=5)
            result = confirm(flow)
            with lock:
                outcomes.append(result.outcome)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=click) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert not errors, errors
    assert outcomes.count("COMMITTED") == 1
    assert outcomes.count("REPLAYED") == threads_count - 1
    assert len(flow.sim.transactions()) == 1
    assert flow.sim.inventory.get("NAVY-84").stock == 3
    assert len(flow.sim.ledger.records) == 1


def test_concurrent_refunds_are_recorded_once(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    barrier = threading.Barrier(4)
    outcomes: list[str] = []
    lock = threading.Lock()

    def refund() -> None:
        barrier.wait(timeout=5)
        result = flow.sim.request_refund(
            flow.session_id, shopper_identity=SHOPPER, request_token=TOKEN
        )
        with lock:
            outcomes.append(result.outcome)

    threads = [threading.Thread(target=refund) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == sorted(
        [REFUND_HANDOFF_STARTED, "REFUND_ALREADY_STARTED", "REFUND_ALREADY_STARTED", "REFUND_ALREADY_STARTED"]
    )
    assert len([r for r in flow.sim.ledger.records if r.outcome == "REFUND_HANDOFF"]) == 1
    assert flow.sim.inventory.get("NAVY-84").stock == 4


def test_refund_requires_the_shoppers_own_session(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    assert (
        flow.sim.request_refund(
            flow.session_id, shopper_identity="intruder", request_token=TOKEN
        ).outcome
        == "REFUND_REFUSED"
    )
    assert (
        flow.sim.request_refund(
            flow.session_id, shopper_identity=SHOPPER, request_token="forged"
        ).outcome
        == "REFUND_REFUSED"
    )
    assert flow.session.state == "QUOTE_READY"


def test_refund_after_commit_keeps_ordinary_return_and_adds_no_transaction(catalog, fixture_clock):
    from harness.commerce import committed_flow

    flow, _ = committed_flow(catalog, fixture_clock)
    refund = flow.sim.request_refund(
        flow.session_id, shopper_identity=SHOPPER, request_token=TOKEN
    )
    assert refund.outcome == REFUND_AFTER_COMMIT
    assert refund.transaction_id is not None
    assert flow.session.refund_recorded is True
    assert len(flow.sim.transactions()) == 1
    assert flow.sim.inventory.get("NAVY-84").stock == 3


def test_commit_pending_is_not_observable_from_outside(catalog, fixture_clock):
    """Confirmation and commit share one atomic boundary.

    No public call can leave a session parked in COMMIT_PENDING, which is what
    makes the refund-vs-exchange race decidable at all.
    """

    flow = start_flow(catalog, fixture_clock)
    before = flow.session.version
    confirm(flow)
    after = flow.session
    assert after.state in ("EXCHANGE_COMMITTED", "RECONCILING", "DECISION_READY")
    assert after.version > before


def test_dead_store_refuses_further_use(catalog, fixture_clock, store_dir):
    from commerce.simulator import FaultKind

    path = store_dir / "store.json"
    flow = start_flow(catalog, fixture_clock, store_path=path)
    flow.sim.faults.push(FaultKind.PERSIST_THEN_CRASH)
    import pytest

    with pytest.raises(StoreError):
        confirm(flow)
    with pytest.raises(StoreError):
        flow.sim.session(flow.session_id)
