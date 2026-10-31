"""Quote contract — integer cents, derived refund, TTL, revalidation (T14 basis)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from commerce.quote import (
    CURRENCY_CHANGED,
    EXPIRED,
    INVENTORY_VERSION_CHANGED,
    OUT_OF_STOCK,
    PRICE_CHANGED,
    UNKNOWN_SKU,
    Quote,
    QuoteError,
    QuoteScopeError,
    build_quote,
    reissue,
    revalidate,
)
from specs import policy as load_policy

POLICY = load_policy()


@pytest.fixture
def now(catalog):
    return catalog.clock


def quote_for(now, *, original=8900, replacement=8400, currency="USD", version=1, quote_version=1):
    return build_quote(
        order_id="ORD-1",
        candidate_variant_id="NAVY-84",
        original_value_minor=original,
        replacement_value_minor=replacement,
        currency=currency,
        inventory_version=f"inv-{version}",
        quote_version=quote_version,
        now=now,
    )


# --------------------------------------------------------------------------- #
# Money arithmetic
# --------------------------------------------------------------------------- #
def test_cheaper_replacement_yields_partial_refund(now):
    quote = quote_for(now, original=8900, replacement=8400)
    assert quote.original_value_minor == 8900
    assert quote.replacement_value_minor == 8400
    assert quote.partial_refund_minor == 500
    assert quote.additional_charge_minor == 0
    assert quote.tax_shipping_adjustment_minor == 0


def test_dearer_replacement_yields_additional_charge(now):
    quote = quote_for(now, original=8900, replacement=9900)
    assert quote.partial_refund_minor == 0
    assert quote.additional_charge_minor == 1000


def test_equal_value_has_no_adjustment(now):
    quote = quote_for(now, original=8400, replacement=8400)
    assert quote.partial_refund_minor == 0
    assert quote.additional_charge_minor == 0


def test_amounts_are_integers_only(now):
    with pytest.raises(QuoteError):
        quote_for(now, replacement=8400.0)  # type: ignore[arg-type]
    with pytest.raises(QuoteError):
        quote_for(now, replacement=True)  # type: ignore[arg-type]
    with pytest.raises(QuoteError):
        quote_for(now, replacement=-1)


def test_contract_forbids_a_refund_that_disagrees_with_the_prices(now):
    with pytest.raises(QuoteError):
        Quote(
            quote_id="qte_x",
            order_id="ORD-1",
            candidate_variant_id="NAVY-84",
            original_value_minor=8900,
            replacement_value_minor=8400,
            currency="USD",
            partial_refund_minor=400,  # wrong on purpose
            additional_charge_minor=0,
            tax_shipping_adjustment_minor=0,
            inventory_version="inv-1",
            expires_at=now + timedelta(seconds=60),
            quote_version=1,
        )


def test_non_zero_tax_adjustment_is_out_of_scope(now):
    with pytest.raises(QuoteScopeError):
        Quote(
            quote_id="qte_x",
            order_id="ORD-1",
            candidate_variant_id="NAVY-84",
            original_value_minor=8900,
            replacement_value_minor=8400,
            currency="USD",
            partial_refund_minor=500,
            additional_charge_minor=0,
            tax_shipping_adjustment_minor=100,
            inventory_version="inv-1",
            expires_at=now + timedelta(seconds=60),
            quote_version=1,
        )


def test_non_usd_currency_is_out_of_fixture_scope(now):
    with pytest.raises(QuoteScopeError):
        quote_for(now, currency="EUR")


def test_currency_must_be_an_iso_code(now):
    with pytest.raises(QuoteError):
        quote_for(now, currency="usd")


def test_quote_version_starts_at_one(now):
    with pytest.raises(QuoteError):
        quote_for(now, quote_version=0)


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
def test_quote_carries_every_contract_field(now):
    payload = quote_for(now).as_dict()
    assert set(payload) == {
        "quoteId",
        "orderId",
        "candidateVariantId",
        "originalValueMinor",
        "replacementValueMinor",
        "currency",
        "partialRefundMinor",
        "additionalChargeMinor",
        "taxShippingAdjustmentMinor",
        "inventoryVersion",
        "expiresAt",
        "quoteVersion",
    }
    assert payload["expiresAt"].endswith("Z")


def test_quote_id_depends_on_the_exact_terms(now):
    first = quote_for(now)
    second = quote_for(now, replacement=9900)
    assert first.quote_id != second.quote_id
    assert first.quote_id.startswith("qte_")
    # Deterministic: same terms and instant -> same id.
    assert quote_for(now).quote_id == first.quote_id


# --------------------------------------------------------------------------- #
# TTL
# --------------------------------------------------------------------------- #
def test_ttl_is_the_policy_value(now):
    quote = quote_for(now)
    assert int(POLICY["quoteTtlSeconds"]) == 60
    assert quote.expires_at == now + timedelta(seconds=60)
    assert quote.issued_at == now


def test_expiry_boundary(now):
    quote = quote_for(now)
    assert quote.is_expired(now) is False
    assert quote.is_expired(now + timedelta(seconds=59)) is False
    assert quote.is_expired(now + timedelta(seconds=60)) is True
    assert quote.is_expired(now + timedelta(seconds=61)) is True


def test_seconds_remaining(now):
    quote = quote_for(now)
    assert quote.seconds_remaining(now) == 60
    assert quote.seconds_remaining(now + timedelta(seconds=45)) == 15


def test_reissue_produces_a_new_id_and_version(now):
    quote = quote_for(now)
    successor = reissue(
        quote,
        original_value_minor=quote.original_value_minor,
        replacement_value_minor=quote.replacement_value_minor,
        currency=quote.currency,
        inventory_version="inv-2",
        now=now + timedelta(seconds=61),
    )
    assert successor.quote_id != quote.quote_id
    assert successor.quote_version == quote.quote_version + 1
    assert successor.expires_at == now + timedelta(seconds=121)
    assert successor.inventory_version == "inv-2"


# --------------------------------------------------------------------------- #
# Revalidation
# --------------------------------------------------------------------------- #
def test_revalidation_passes_when_everything_matches(now):
    quote = quote_for(now)
    result = revalidate(
        quote,
        now=now,
        inventory_version="inv-1",
        price_minor=8400,
        currency="USD",
        stock=3,
    )
    assert result.ok is True
    assert result.failures == ()
    assert result.current_price_minor == 8400
    assert result.current_stock == 3


@pytest.mark.parametrize(
    "expected,changes",
    [
        (PRICE_CHANGED, {"price_minor": 8300}),
        (CURRENCY_CHANGED, {"currency": "EUR"}),
        (OUT_OF_STOCK, {"stock": 0}),
        (INVENTORY_VERSION_CHANGED, {"inventory_version": "inv-2"}),
    ],
)
def test_revalidation_detects_each_change(now, expected, changes):
    quote = quote_for(now)
    base = {
        "now": now,
        "inventory_version": "inv-1",
        "price_minor": 8400,
        "currency": "USD",
        "stock": 3,
    }
    result = revalidate(quote, **{**base, **changes})
    assert result.ok is False
    assert expected in result.failures


def test_revalidation_expiry_is_a_failure(now):
    quote = quote_for(now)
    result = revalidate(
        quote,
        now=now + timedelta(seconds=60),
        inventory_version="inv-1",
        price_minor=8400,
        currency="USD",
        stock=3,
    )
    assert result.ok is False
    assert result.failures[0] == EXPIRED


def test_unknown_facts_are_failures(now):
    quote = quote_for(now)
    result = revalidate(
        quote, now=now, inventory_version=None, price_minor=None, currency=None, stock=None
    )
    assert result.ok is False
    # Unknown is reported as unknown, never as a specific change and never as "ok".
    assert result.failures == (UNKNOWN_SKU,)
    assert result.current_price_minor is None


def test_revalidation_reports_every_failure_it_finds(now):
    quote = quote_for(now)
    result = revalidate(
        quote,
        now=now + timedelta(seconds=120),
        inventory_version="inv-9",
        price_minor=1,
        currency="EUR",
        stock=0,
    )
    assert set(result.failures) == {
        EXPIRED,
        INVENTORY_VERSION_CHANGED,
        PRICE_CHANGED,
        CURRENCY_CHANGED,
        OUT_OF_STOCK,
    }
    assert result.as_dict()["ok"] is False
