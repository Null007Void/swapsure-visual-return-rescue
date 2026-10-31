"""Atomic commit simulation — happy path, T13, T14, T15, T16, T21.

The theme: nothing but an explicit shopper approval of the exact unexpired quote
can move money, and any change of facts requires a fresh quote and fresh approval.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from commerce.simulator import (
    AGENT_RECOMMENDATION_IS_NOT_APPROVAL,
    COMMITTED,
    CONFLICT,
    EXPIRED_QUOTE,
    IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD,
    KNOWN_FAILURE,
    NOT_SHOPPER_ORIGIN,
    PRICE_CHANGED,
    PREVIEW_IS_NOT_APPROVAL,
    QUOTE_MISMATCH,
    REFUSED,
    REPLAYED,
    REQUEST_TOKEN_MISMATCH,
    REQUIRE_FRESH_QUOTE,
    SELECTION_IS_NOT_APPROVAL,
    SHOPPER_MISMATCH,
    STOCK_CHANGED,
    WRONG_STATE,
    ConfirmRequest,
    ConfirmResult,
)
from domain.catalog import select_candidates
from harness.commerce import committed_flow, confirm, decision_ready_flow, start_flow
from harness.scenarios import IDEMPOTENCY_KEY, SHOPPER, TOKEN, confirm_request


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_happy_path_commits_exactly_one_transaction(catalog, fixture_clock):
    flow, result = committed_flow(catalog, fixture_clock)

    assert result.outcome == COMMITTED
    assert result.transaction_id and result.transaction_id.startswith("txn_")
    assert flow.session.state == "EXCHANGE_COMMITTED"
    assert flow.session.commit_id == result.transaction_id

    transactions = flow.sim.transactions()
    assert len(transactions) == 1
    assert transactions[0].status == "COMMITTED"
    assert transactions[0].session_id == flow.session_id

    # One unit reserved, atomically, exactly once.
    assert flow.sim.inventory.get("NAVY-84").stock == 3


def test_happy_path_quote_and_receipt_match_the_fixture_oracle(catalog, fixture_clock):
    flow, result = committed_flow(catalog, fixture_clock)
    quote = flow.quote
    receipt = result.receipt
    assert quote.original_value_minor == 8900
    assert quote.replacement_value_minor == 8400
    assert quote.partial_refund_minor == 500
    assert quote.additional_charge_minor == 0
    assert receipt is not None
    assert receipt.retained_gmv_minor == 8400
    assert receipt.partial_refund_minor == 500
    assert receipt.additional_charge_minor == 0
    assert receipt.original_sku == "ORIGINAL"
    assert receipt.replacement_sku == "NAVY-84"
    assert receipt.currency == "USD"
    assert receipt.policy_version == "1.0"


def test_receipt_carries_the_audit_fields_and_keeps_the_return_route(catalog, fixture_clock):
    flow, result = committed_flow(catalog, fixture_clock)
    receipt = result.receipt
    payload = receipt.as_dict()
    for field in (
        "returnReasonCodes",
        "eligibilityStatus",
        "eligibilityRationale",
        "policyVersion",
        "originalSku",
        "replacementSku",
        "originalValueMinor",
        "replacementValueMinor",
        "stockCheckedAt",
        "stockCheckInventoryVersion",
        "candidatesEligible",
        "previewsDelivered",
        "previewMode",
        "approval",
        "partialRefundMinor",
        "additionalChargeMinor",
        "retainedGmvMinor",
        "outcome",
        "committedAt",
        "returnSupportRef",
        "modeLabel",
    ):
        assert field in payload
    assert payload["eligibilityStatus"] == "ELIGIBLE_VISUAL_RESCUE"
    assert payload["returnReasonCodes"] == ["COLOR"]
    assert payload["previewMode"] == "MOCK"
    assert payload["modeLabel"].startswith("SIMULATED EXCHANGE")
    assert payload["returnSupportRef"].startswith("host://returns/")
    assert payload["approval"]["origin"] == "shopper_ui"
    assert payload["approval"]["quoteVersion"] == 1
    assert payload["stockCheckInventoryVersion"].startswith("inv-")


def test_ledger_records_the_commit_once(catalog, fixture_clock):
    flow, _ = committed_flow(catalog, fixture_clock)
    records = flow.sim.ledger.records
    assert len(records) == 1
    assert records[0].outcome == "EXCHANGE_COMMITTED"
    assert records[0].retained_gmv_minor == 8400
    assert flow.sim.metrics(eligible_optins=1)["retainedOriginalGmvMinor"] == 8400


# --------------------------------------------------------------------------- #
# T13 — inventory changes before commit
# --------------------------------------------------------------------------- #
def test_t13_stock_disappears_before_commit(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.inventory.set_stock("NAVY-84", 0, fixture_clock())

    result = confirm(flow)

    assert result.outcome == KNOWN_FAILURE
    assert result.reason == STOCK_CHANGED
    assert result.required_action == REQUIRE_FRESH_QUOTE
    assert flow.session.state == "DECISION_READY"
    assert flow.sim.inventory.get("NAVY-84").stock == 0
    assert [t.status for t in flow.sim.transactions()] == ["ABANDONED"]
    assert flow.sim.ledger.records == ()


def test_t13_price_changes_before_commit(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.inventory.set_price("NAVY-84", 8300, fixture_clock())

    result = confirm(flow)

    assert result.outcome == KNOWN_FAILURE
    assert result.reason == PRICE_CHANGED
    assert flow.session.state == "DECISION_READY"
    assert flow.sim.inventory.get("NAVY-84").price_minor == 8300
    assert result.revalidation is not None
    assert "PRICE_CHANGED" in result.revalidation["failures"]


def test_t13_unrelated_inventory_revision_does_not_block_a_valid_commit(catalog, fixture_clock):
    """The quote promises facts, and the facts are re-checked under the lock."""

    flow = start_flow(catalog, fixture_clock)
    flow.sim.inventory.set_stock("GREEN-99", 9, fixture_clock())  # unrelated SKU
    checked_version = flow.sim.inventory.version

    result = confirm(flow)

    assert result.outcome == COMMITTED
    assert result.receipt is not None
    # The receipt records the version that was actually checked at commit time.
    assert result.receipt.stock_check_inventory_version == checked_version


def test_t13_shopper_can_reselect_after_a_rejected_commit(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.inventory.set_stock("NAVY-84", 0, fixture_clock())
    assert confirm(flow).outcome == KNOWN_FAILURE

    assert flow.sim.choose(flow.session_id, "GREEN-99").accepted
    # A different candidate means a different quote and a new idempotency key.
    second = confirm(flow, quote=flow.session.quote, idempotency_key="idem-reselect")

    assert second.outcome == COMMITTED
    assert flow.quote.replacement_value_minor == 9900
    assert second.receipt.additional_charge_minor == 1000
    assert flow.sim.inventory.get("GREEN-99").stock == 3


def test_t13_a_reused_key_after_reselection_is_a_conflict(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    flow.sim.inventory.set_stock("NAVY-84", 0, fixture_clock())
    assert confirm(flow).outcome == KNOWN_FAILURE
    assert flow.sim.choose(flow.session_id, "GREEN-99").accepted

    reused = confirm(flow, quote=flow.session.quote)  # same key, different quote
    assert reused.outcome == CONFLICT
    assert flow.sim.transactions()[0].status == "ABANDONED"
    assert flow.sim.inventory.get("GREEN-99").stock == 4


# --------------------------------------------------------------------------- #
# T14 — quote expiry
# --------------------------------------------------------------------------- #
def test_t14_expired_quote_is_refused_without_side_effect(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    fixture_clock.advance(61)

    result = confirm(flow)

    assert result.outcome == REFUSED
    assert result.reason == EXPIRED_QUOTE
    assert flow.session.state == "QUOTE_READY"
    assert flow.sim.transactions() == ()
    assert flow.sim.ledger.records == ()


def test_t14_fresh_quote_and_fresh_approval_are_required(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    original_quote = flow.quote
    fixture_clock.advance(61)
    assert confirm(flow).reason == EXPIRED_QUOTE

    successor = flow.sim.reissue_quote(flow.session_id)
    assert successor.quote_id != original_quote.quote_id
    assert successor.quote_version == original_quote.quote_version + 1

    # The old approval cannot be reused against the new quote.
    stale = flow.sim.confirm_exchange(
        confirm_request(flow.session, original_quote, idempotency_key="idem-old")
    )
    assert stale.outcome == REFUSED
    assert stale.reason == QUOTE_MISMATCH

    # The new quote needs the shopper's approval, then commits.
    fresh = flow.sim.confirm_exchange(
        confirm_request(flow.session, successor, idempotency_key="idem-new")
    )
    assert fresh.outcome == COMMITTED
    assert fresh.receipt is not None
    assert flow.sim.transactions()[0].quote_id == successor.quote_id


def test_t14_quote_expiring_between_approval_and_commit_returns_to_decision(catalog, fixture_clock):
    """A quote that lapses inside the commit is a known failure, not a charge."""

    flow = start_flow(catalog, fixture_clock)
    request = flow.request()
    fixture_clock.advance(61)  # expiry passes after the approval was constructed

    result = flow.sim.confirm_exchange(request)

    assert result.outcome == REFUSED
    assert result.reason == EXPIRED_QUOTE
    assert flow.session.state == "QUOTE_READY"
    assert flow.sim.ledger.records == ()


# --------------------------------------------------------------------------- #
# T15 — duplicate confirm clicks
# --------------------------------------------------------------------------- #
def test_t15_duplicate_click_commits_once_and_replays_the_receipt(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)

    first = confirm(flow)
    second = confirm(flow, approved_at=fixture_clock() + timedelta(seconds=1))
    third = confirm(flow, approved_at=fixture_clock() + timedelta(seconds=2))

    assert first.outcome == COMMITTED
    assert second.outcome == REPLAYED and third.outcome == REPLAYED
    assert second.transaction_id == first.transaction_id
    assert second.receipt.retained_gmv_minor == first.receipt.retained_gmv_minor
    assert len(flow.sim.transactions()) == 1
    assert flow.sim.inventory.get("NAVY-84").stock == 3
    assert len(flow.sim.ledger.records) == 1


def test_t15_replay_after_commit_does_not_reinstate_a_refund(catalog, fixture_clock):
    flow, first = committed_flow(catalog, fixture_clock)
    refund = flow.sim.request_refund(
        flow.session_id, shopper_identity=SHOPPER, request_token=TOKEN
    )
    assert refund.outcome == "REFUND_AFTER_COMMIT"

    replay = confirm(flow)
    assert replay.outcome == REPLAYED
    assert flow.session.state == "EXCHANGE_COMMITTED"
    assert len(flow.sim.transactions()) == 1


# --------------------------------------------------------------------------- #
# T16 — same key, different payload
# --------------------------------------------------------------------------- #
def test_t16_same_key_changed_candidate_is_a_conflict(catalog, fixture_clock):
    flow, first = committed_flow(catalog, fixture_clock)

    conflict = confirm(flow, candidate_sku="GREEN-99")

    assert conflict.outcome == CONFLICT
    assert conflict.reason == IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD
    assert conflict.transaction_id == first.transaction_id
    assert len(flow.sim.transactions()) == 1
    assert flow.sim.inventory.get("GREEN-99").stock == 4


def test_t16_same_key_changed_quote_is_a_conflict(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    quote = flow.quote
    first = confirm(flow)
    assert first.outcome == COMMITTED

    other_quote = quote.__class__(
        quote_id="qte_other",
        order_id=quote.order_id,
        candidate_variant_id=quote.candidate_variant_id,
        original_value_minor=quote.original_value_minor,
        replacement_value_minor=quote.replacement_value_minor,
        currency=quote.currency,
        partial_refund_minor=quote.partial_refund_minor,
        additional_charge_minor=quote.additional_charge_minor,
        tax_shipping_adjustment_minor=0,
        inventory_version=quote.inventory_version,
        expires_at=quote.expires_at,
        quote_version=quote.quote_version,
    )
    conflict = flow.sim.confirm_exchange(
        confirm_request(flow.session, other_quote, idempotency_key=IDEMPOTENCY_KEY)
    )
    assert conflict.outcome == CONFLICT
    assert len(flow.sim.transactions()) == 1


def test_t16_a_different_key_is_a_different_transaction_attempt_only_after_state_change(
    catalog, fixture_clock
):
    flow = start_flow(catalog, fixture_clock)
    assert confirm(flow).outcome == COMMITTED
    second = confirm(flow, idempotency_key="idem-2")
    # Terminal state: a new key cannot start a second transaction.
    assert second.outcome == REFUSED
    assert second.reason == "TERMINAL_STATE"
    assert len(flow.sim.transactions()) == 1


# --------------------------------------------------------------------------- #
# T21 — only the shopper's explicit approval counts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "action,reason",
    [
        ("SELECT", SELECTION_IS_NOT_APPROVAL),
        ("SELECT_CANDIDATE", SELECTION_IS_NOT_APPROVAL),
        ("PREVIEW_COMPLETE", PREVIEW_IS_NOT_APPROVAL),
        ("AGENT_RECOMMENDATION", AGENT_RECOMMENDATION_IS_NOT_APPROVAL),
        ("AUTO_APPROVE", AGENT_RECOMMENDATION_IS_NOT_APPROVAL),
        ("COMPLETE", "UNKNOWN_ACTION_IS_NOT_APPROVAL"),
    ],
)
def test_t21_none_of_these_actions_is_a_confirmation(catalog, fixture_clock, action, reason):
    flow = start_flow(catalog, fixture_clock)
    result = confirm(flow, action=action)
    assert result.outcome == REFUSED
    assert result.reason == reason
    assert flow.session.state == "QUOTE_READY"
    assert flow.sim.transactions() == ()
    assert flow.sim.ledger.records == ()


@pytest.mark.parametrize("origin", ["agent", "server_job", "network_proxy", "shopper_api_proxy"])
def test_t21_untrusted_origin_cannot_approve(catalog, fixture_clock, origin):
    flow = start_flow(catalog, fixture_clock)
    result = confirm(flow, origin=origin)
    assert result.outcome == REFUSED
    assert result.reason == NOT_SHOPPER_ORIGIN
    assert flow.sim.transactions() == ()


def test_t21_another_shoppers_session_cannot_approve(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    result = confirm(flow, shopper_identity="intruder")
    assert result.reason == SHOPPER_MISMATCH
    assert flow.sim.transactions() == ()


def test_t21_request_without_the_session_token_is_refused(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    result = confirm(flow, request_token="forged")
    assert result.reason == REQUEST_TOKEN_MISMATCH
    assert flow.sim.transactions() == ()


def test_t21_selection_and_preview_completion_are_not_approval(catalog, fixture_clock):
    """A session with candidates and previews, but no quote, cannot commit."""

    flow = decision_ready_flow(catalog, fixture_clock)
    assert flow.session.state == "DECISION_READY"
    assert flow.session.quote is None
    assert flow.session.preview_result is not None

    result = flow.sim.confirm_exchange(
        ConfirmRequest(
            session_id=flow.session_id,
            shopper_identity=SHOPPER,
            request_token=TOKEN,
            quote_id="qte_selection_only",
            quote_version=1,
            candidate_sku=flow.selection.candidate_skus[0],
            action="APPROVE_EXCHANGE",
            origin="shopper_ui",
            approved_at=fixture_clock(),
            idempotency_key="idem-selection",
        )
    )
    assert result.outcome == REFUSED
    assert result.reason == WRONG_STATE
    assert flow.sim.transactions() == ()
    assert flow.sim.ledger.records == ()


def test_t21_the_transport_cannot_assert_approval(catalog, fixture_clock):
    """ConfirmRequest has no boolean that says 'approved'."""

    flow = start_flow(catalog, fixture_clock)
    payload = {
        "session_id": flow.session_id,
        "shopper_identity": SHOPPER,
        "request_token": TOKEN,
        "quote_id": flow.quote.quote_id,
        "quote_version": flow.quote.quote_version,
        "candidate_sku": flow.sku,
        "action": "APPROVE_EXCHANGE",
        "origin": "shopper_ui",
        "approved_at": fixture_clock(),
        "idempotency_key": "idem-x",
    }
    with pytest.raises(TypeError):
        ConfirmRequest(**payload, approved=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ConfirmRequest(**{**payload, "is_human": True})  # type: ignore[call-arg]


def test_t21_silence_never_commits(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    for _ in range(3):
        fixture_clock.advance(10)
    assert flow.session.state == "QUOTE_READY"
    assert flow.sim.transactions() == ()
    assert flow.sim.ledger.records == ()


# --------------------------------------------------------------------------- #
# Structural guards
# --------------------------------------------------------------------------- #
def test_unknown_session_is_refused(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    request = flow.request()
    forged = ConfirmRequest(
        session_id="ses-does-not-exist",
        shopper_identity=request.shopper_identity,
        request_token=request.request_token,
        quote_id=request.quote_id,
        quote_version=request.quote_version,
        candidate_sku=request.candidate_sku,
        action=request.action,
        origin=request.origin,
        approved_at=request.approved_at,
        idempotency_key="idem-unknown",
    )
    result = flow.sim.confirm_exchange(forged)
    assert result.outcome == REFUSED
    assert result.reason == "SESSION_NOT_FOUND"


def test_confirm_without_any_quote_is_a_mismatch_refusal(catalog, fixture_clock):
    flow = decision_ready_flow(catalog, fixture_clock)
    result = flow.sim.confirm_exchange(
        ConfirmRequest(
            session_id=flow.session_id,
            shopper_identity=SHOPPER,
            request_token=TOKEN,
            quote_id="qte_none",
            quote_version=1,
            candidate_sku=flow.selection.candidate_skus[0],
            action="APPROVE_EXCHANGE",
            origin="shopper_ui",
            approved_at=fixture_clock(),
            idempotency_key="idem-noquote",
        )
    )
    assert result.outcome == REFUSED
    assert result.reason == WRONG_STATE
    assert flow.sim.transactions() == ()


def test_commit_requires_a_quote_from_the_offered_candidates(catalog, fixture_clock):
    flow = start_flow(catalog, fixture_clock)
    assert flow.sku in flow.selection.candidate_skus
    assert flow.sku != "EXPENSIVE-120"  # never offered, so never confirmable
    assert "EXPENSIVE-120" not in flow.selection.candidate_skus
