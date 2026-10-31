"""Outcome ledger — retained GMV, dedupe (T34), outbox, zero denominator (T38)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from commerce.ledger import (
    OUTCOME_EXCHANGE_COMMITTED,
    OUTCOME_PENDING,
    OUTCOME_RECONCILING,
    OUTCOME_REFUND_HANDOFF,
    LedgerError,
    OutcomeLedger,
    OutcomeRecord,
    committed_exchange,
    non_committed_outcome,
    retained_gmv_minor,
)
from harness.commerce import committed_flow, start_flow
from harness.scenarios import SHOPPER, TOKEN


class RecordingSink:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.delivered: list[str] = []
        self.attempts = 0

    def __call__(self, record: OutcomeRecord) -> None:
        self.attempts += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("sink unavailable")
        # Idempotent sink: keyed by transaction id, as the contract requires.
        if record.transaction_id not in self.delivered:
            self.delivered.append(record.transaction_id)


def make_exchange(txn: str = "txn_1", original: int = 8900, replacement: int = 8400, now=None):
    from datetime import datetime, timezone

    return committed_exchange(
        transaction_id=txn,
        session_id="ses-1",
        order_id="ORD-1",
        original_sku="ORIGINAL",
        replacement_sku="NAVY-84",
        original_value_minor=original,
        replacement_value_minor=replacement,
        currency="USD",
        committed_at=now or datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc),
        preview_mode="MOCK",
        policy_version="1.0",
        eligibility_status="ELIGIBLE_VISUAL_RESCUE",
    )


# --------------------------------------------------------------------------- #
# retained GMV definition
# --------------------------------------------------------------------------- #
def test_retained_gmv_is_the_minimum_of_the_two_values():
    assert retained_gmv_minor(8900, 8400) == 8400
    assert retained_gmv_minor(8900, 9900) == 8900
    assert retained_gmv_minor(8400, 8400) == 8400
    assert retained_gmv_minor(0, 0) == 0


def test_retained_gmv_rejects_non_integers():
    for bad in (84.0, True, -1, "8400"):
        with pytest.raises(LedgerError):
            retained_gmv_minor(8900, bad)  # type: ignore[arg-type]


def test_89_to_84_retains_8400_and_refunds_500():
    record = make_exchange()
    assert record.retained_gmv_minor == 8400
    assert record.partial_refund_minor == 500
    assert record.additional_charge_minor == 0
    assert record.outcome == OUTCOME_EXCHANGE_COMMITTED


def test_89_to_99_retains_8900_and_never_counts_9900_as_rescued():
    record = make_exchange(replacement=9900)
    assert record.retained_gmv_minor == 8900
    assert record.retained_gmv_minor != 9900
    assert record.additional_charge_minor == 1000
    assert record.partial_refund_minor == 0


def test_record_refuses_an_inflated_retained_value():
    with pytest.raises(LedgerError):
        OutcomeRecord(
            transaction_id="txn_bad",
            session_id="ses-1",
            order_id="ORD-1",
            original_sku="ORIGINAL",
            replacement_sku="NAVY-84",
            original_value_minor=8900,
            replacement_value_minor=8400,
            retained_gmv_minor=8900,  # inflated on purpose
            partial_refund_minor=500,
            additional_charge_minor=0,
            currency="USD",
            outcome=OUTCOME_EXCHANGE_COMMITTED,
            committed_at=make_exchange().committed_at,
            preview_mode="MOCK",
            policy_version="1.0",
            eligibility_status="ELIGIBLE_VISUAL_RESCUE",
        )


@pytest.mark.parametrize("outcome", [OUTCOME_REFUND_HANDOFF, OUTCOME_PENDING, OUTCOME_RECONCILING])
def test_non_committed_outcomes_retain_nothing(outcome):
    record = non_committed_outcome(
        transaction_id="txn_x",
        session_id="ses-1",
        order_id="ORD-1",
        original_sku="ORIGINAL",
        replacement_sku=None,
        original_value_minor=8900,
        replacement_value_minor=0,
        currency="USD",
        outcome=outcome,
        committed_at=make_exchange().committed_at,
        preview_mode="MOCK",
        policy_version="1.0",
        eligibility_status="ELIGIBLE_VISUAL_RESCUE",
    )
    assert record.retained_gmv_minor == 0
    assert record.partial_refund_minor == 0


def test_non_committed_factory_refuses_committed_outcome():
    with pytest.raises(LedgerError):
        non_committed_outcome(
            transaction_id="txn_x",
            session_id="ses-1",
            order_id="ORD-1",
            original_sku="ORIGINAL",
            replacement_sku=None,
            original_value_minor=8900,
            replacement_value_minor=0,
            currency="USD",
            outcome=OUTCOME_EXCHANGE_COMMITTED,
            committed_at=make_exchange().committed_at,
            preview_mode="MOCK",
            policy_version="1.0",
            eligibility_status="ELIGIBLE_VISUAL_RESCUE",
        )


# --------------------------------------------------------------------------- #
# T34 — dedupe by committed transaction id
# --------------------------------------------------------------------------- #
def test_t34_duplicate_record_does_not_duplicate_retained_value():
    ledger = OutcomeLedger()
    record = make_exchange()

    first = ledger.record(record)
    second = ledger.record(record)
    third = ledger.record(make_exchange())

    assert first.recorded is True and first.duplicate is False
    assert second.recorded is False and second.duplicate is True
    assert third.duplicate is True
    assert len(ledger) == 1
    assert len(ledger.pending) == 1
    assert ledger.metrics(eligible_optins=1).retained_gmv_minor == 8400


def test_t34_retry_loop_cannot_inflate_totals():
    ledger = OutcomeLedger()
    for _ in range(50):
        ledger.record(make_exchange())
    metrics = ledger.metrics(eligible_optins=3)
    assert metrics.committed_exchanges == 1
    assert metrics.retained_gmv_minor == 8400
    assert metrics.partial_refund_minor == 500


# --------------------------------------------------------------------------- #
# Outbox
# --------------------------------------------------------------------------- #
def test_outbox_delivers_each_record_once():
    ledger = OutcomeLedger()
    sink = RecordingSink()
    ledger.record(make_exchange("txn_a"))
    ledger.record(make_exchange("txn_b", replacement=9900))

    first = ledger.flush(sink)
    second = ledger.flush(sink)

    assert first.delivered == 2 and first.remaining == 0 and first.ok
    assert second.delivered == 0
    assert sink.delivered == ["txn_a", "txn_b"]
    assert sink.attempts == 2


def test_outbox_retries_after_a_sink_failure():
    ledger = OutcomeLedger()
    sink = RecordingSink(failures=2)
    ledger.record(make_exchange())

    failed = ledger.flush(sink)
    assert failed.failed == 1
    assert failed.remaining == 1
    assert failed.ok is False
    assert ledger.pending

    assert ledger.flush(sink).failed == 1
    recovered = ledger.flush(sink)

    assert recovered.delivered == 1
    assert recovered.remaining == 0
    assert sink.delivered == ["txn_1"]
    assert ledger.metrics(eligible_optins=1).retained_gmv_minor == 8400


def test_outbox_survives_a_restart_with_pending_work():
    ledger = OutcomeLedger()
    ledger.record(make_exchange())
    state = ledger.to_state()

    restored = OutcomeLedger.from_state(state)
    sink = RecordingSink()
    result = restored.flush(sink)

    assert result.delivered == 1
    assert sink.delivered == ["txn_1"]
    assert len(restored) == 1


def test_ledger_state_round_trip_preserves_delivery_markers():
    ledger = OutcomeLedger()
    sink = RecordingSink()
    ledger.record(make_exchange())
    ledger.flush(sink)

    restored = OutcomeLedger.from_state(ledger.to_state())

    assert restored.delivered_ids == ledger.delivered_ids
    assert restored.pending == ()
    assert restored.flush(sink).delivered == 0
    assert sink.attempts == 1


# --------------------------------------------------------------------------- #
# T38 — zero-denominator safety
# --------------------------------------------------------------------------- #
def test_t38_zero_attempts_yields_no_percentage():
    ledger = OutcomeLedger()
    metrics = ledger.metrics(eligible_optins=0)

    assert metrics.has_denominator is False
    assert metrics.rescue_conversion is None
    payload = metrics.as_dict()
    assert payload["rescueConversion"] == "N/A"
    assert payload["eligibleOptins"] == 0
    assert payload["committedExchanges"] == 0
    assert payload["retainedOriginalGmvMinor"] == 0


def test_t38_zero_committed_with_a_denominator_is_a_real_zero():
    ledger = OutcomeLedger()
    metrics = ledger.metrics(eligible_optins=4)
    assert metrics.rescue_conversion == 0.0
    assert metrics.as_dict()["rescueConversion"] == 0.0


def test_t38_conversion_uses_committed_over_optins():
    ledger = OutcomeLedger()
    ledger.record(make_exchange("txn_a"))
    ledger.record(make_exchange("txn_b", replacement=9900))
    metrics = ledger.metrics(eligible_optins=4)
    assert metrics.rescue_conversion == 0.5
    assert metrics.as_dict()["rescueConversion"] == 0.5


def test_metrics_count_zero_retained_outcomes():
    ledger = OutcomeLedger()
    ledger.record(make_exchange())
    ledger.record(
        non_committed_outcome(
            transaction_id="refund:ses-1:4",
            session_id="ses-1",
            order_id="ORD-1",
            original_sku="ORIGINAL",
            replacement_sku=None,
            original_value_minor=8900,
            replacement_value_minor=0,
            currency="USD",
            outcome=OUTCOME_REFUND_HANDOFF,
            committed_at=make_exchange().committed_at,
            preview_mode="MOCK",
            policy_version="1.0",
            eligibility_status="ELIGIBLE_VISUAL_RESCUE",
        )
    )
    metrics = ledger.metrics(eligible_optins=2)
    assert metrics.committed_exchanges == 1
    assert metrics.zero_retained == 1
    assert metrics.retained_gmv_minor == 8400


# --------------------------------------------------------------------------- #
# Integration with the simulator
# --------------------------------------------------------------------------- #
def test_committed_flow_records_the_fixture_accounting(catalog, fixture_clock):
    flow, _ = committed_flow(catalog, fixture_clock)
    records = flow.sim.ledger.records
    assert len(records) == 1
    assert records[0].retained_gmv_minor == 8400
    assert records[0].partial_refund_minor == 500
    assert records[0].additional_charge_minor == 0
    assert records[0].preview_mode == "MOCK"
    assert records[0].eligibility_status == "ELIGIBLE_VISUAL_RESCUE"


def test_dearer_exchange_records_incremental_sales_not_rescued_value(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock, candidate_sku="GREEN-99")
    result = flow.sim.confirm_exchange(flow.request())
    assert result.outcome == "COMMITTED"

    record = flow.sim.ledger.records[0]
    assert record.original_value_minor == 8900
    assert record.replacement_value_minor == 9900
    assert record.retained_gmv_minor == 8900
    assert record.additional_charge_minor == 1000
    assert flow.sim.metrics(eligible_optins=1)["retainedOriginalGmvMinor"] == 8900


def test_refund_only_session_records_zero_retained(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.request_refund(flow.session_id, shopper_identity=SHOPPER, request_token=TOKEN)
    metrics = flow.sim.metrics(eligible_optins=1)
    assert metrics["retainedOriginalGmvMinor"] == 0
    assert metrics["committedExchanges"] == 0
    assert metrics["zeroRetainedOutcomes"] == 1
    assert metrics["rescueConversion"] == 0.0


def test_no_attempts_at_all_still_reports_na(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    assert flow.sim.metrics(eligible_optins=0)["rescueConversion"] == "N/A"
