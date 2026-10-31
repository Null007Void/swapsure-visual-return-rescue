"""End-to-end offline journeys — the executable evidence behind AC01–AC04.

These are not new rules; they walk the real modules in the order a session does,
so that a regression in any single layer shows up as a broken shopper journey.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from adapters.youcam_stub import FailureCode, MockPreviewAdapter
from domain.catalog import select_candidates
from domain.eligibility import decide
from domain.preview import PreviewPlan, PreviewResult
from harness.commerce import confirm, simulator_for, start_flow
from harness.scenarios import CONSENT, SHOPPER, TOKEN, confirm_request


def test_rescue_journey_ends_in_a_simulated_exchange_with_a_receipt(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    result = confirm(flow)

    assert result.outcome == "COMMITTED"
    assert result.receipt is not None
    assert result.receipt.mode_label.startswith("SIMULATED EXCHANGE")
    assert result.receipt.preview_mode == "MOCK"
    assert result.receipt.retained_gmv_minor == 8400
    assert result.receipt.partial_refund_minor == 500
    assert result.receipt.return_support_ref.startswith("host://returns/")
    assert flow.sim.metrics(eligible_optins=1)["rescueConversion"] == 1.0


def test_fit_refusal_journey_never_touches_the_visual_path(catalog, fixture_clock):
    sim = simulator_for(catalog, clock=fixture_clock)
    session = sim.open_session(catalog.order, shopper_identity=SHOPPER, request_token=TOKEN)
    decision = decide(session.session_id, "1.0", ["TOO_SMALL"], False)

    assert sim.evaluate(session.session_id, decision).accepted
    assert decision.preview_dispatch_permitted is False

    opt_in = sim.opt_in(session.session_id)
    assert opt_in.accepted is False
    assert opt_in.refusal == "NOT_ELIGIBLE"

    refund = sim.request_refund(
        session.session_id, shopper_identity=SHOPPER, request_token=TOKEN
    )
    assert refund.outcome == "REFUND_HANDOFF_STARTED"
    assert refund.refund_route_available is True
    assert sim.session(session.session_id).state == "REFUND_HANDOFF"

    # Zero preview work, zero units, zero retained value.
    assert sim.ledger.records[0].retained_gmv_minor == 0
    assert sim.metrics(eligible_optins=1)["committedExchanges"] == 0


def test_unknown_reason_journey_fails_open_to_the_return_route(catalog, fixture_clock):
    sim = simulator_for(catalog, clock=fixture_clock)
    session = sim.open_session(catalog.order, shopper_identity=SHOPPER, request_token=TOKEN)
    decision = decide(session.session_id, "1.0", ["OTHER"], False)
    assert decision.status == "UNKNOWN_REQUIRES_REFUND_PATH"

    assert sim.evaluate(session.session_id, decision).accepted
    assert sim.opt_in(session.session_id).accepted is False
    assert sim.request_refund(
        session.session_id, shopper_identity=SHOPPER, request_token=TOKEN
    ).outcome == "REFUND_HANDOFF_STARTED"


def test_preview_failure_still_allows_a_decision_and_a_refund(catalog, fixture_clock):
    """Fail open: no previews, but candidates, prices, refund and commit survive."""

    sim = simulator_for(catalog, clock=fixture_clock)
    session = sim.open_session(catalog.order, shopper_identity=SHOPPER, request_token=TOKEN)
    sim.evaluate(session.session_id, decide(session.session_id, "1.0", ["COLOR"], False))
    sim.opt_in(session.session_id)
    sim.record_consent(session.session_id, CONSENT)
    selection = select_candidates(
        catalog.query(session_id=session.session_id), now=fixture_clock()
    )
    assert sim.prepare_candidates(session.session_id, selection).accepted

    adapter = MockPreviewAdapter(clock=fixture_clock)
    plan = PreviewPlan(
        session_id=session.session_id,
        consent_version=CONSENT,
        candidate_ids=selection.candidate_skus,
        provider_version="mock-v1",
        estimated_units=0,
        authorization_ref=None,
        deadline=fixture_clock() + timedelta(seconds=60),
    )
    adapter.fail_next(FailureCode.TIMEOUT)
    with pytest.raises(Exception):
        adapter.create_preview(plan)
    assert adapter.units_consumed == 0

    # The app falls back to an honest empty result: no images, cards remain.
    empty = PreviewResult(
        session_id=session.session_id,
        mode="MOCK",
        status="FAILED",
        candidate_ids=selection.candidate_skus,
        task_ids=tuple(f"failed-{index}" for index in range(len(selection.candidate_skus))),
        created_at=fixture_clock(),
        expires_at=fixture_clock() + timedelta(seconds=60),
        failure_code="TIMEOUT",
    )
    assert sim.receive_preview_result(session.session_id, empty).accepted
    assert sim.session(session.session_id).state == "DECISION_READY"

    assert sim.choose(session.session_id, selection.candidate_skus[0]).accepted
    current = sim.session(session.session_id)
    committed = sim.confirm_exchange(confirm_request(current, current.quote))
    assert committed.outcome == "COMMITTED"
    assert committed.receipt.previews_delivered == 0
    assert committed.receipt.preview_mode == "MOCK"


def test_selection_output_feeds_the_quote_without_translation(catalog, fixture_clock):
    """The candidate the shopper saw is the one that gets priced."""

    flow = start_flow(catalog, fixture_clock)
    selection = flow.selection
    quote = flow.quote
    assert quote.candidate_variant_id == flow.sku
    assert quote.candidate_variant_id in selection.candidate_skus
    priced = next(item for item in selection.candidates if item.sku == flow.sku)
    assert quote.replacement_value_minor == priced.price_minor
    assert quote.original_value_minor == catalog.original.price_minor
    assert quote.inventory_version == selection.inventory_version
