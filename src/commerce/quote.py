"""Integer-cents quote contract — `docs/05-decision-contracts.md`.

All amounts are integer minor units.  Fixture scope is USD with no tax, discount,
shipping or fee adjustment, so:

    partialRefundMinor   = max(originalValueMinor - replacementValueMinor, 0)
    additionalChargeMinor = max(replacementValueMinor - originalValueMinor, 0)

Both are *derived*, never passed in, so a caller cannot state a refund that
disagrees with the prices.  Real authoritative totals (tax, discounts, shipping,
fees) are a Phase D/E concern; this build refuses to guess them.

A quote is a frozen value.  Any change to price, variant, terms or inventory
version requires a new quote object with a new id, which in turn requires a new
explicit approval — that is the mechanism behind T14.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from specs import policy as load_policy

FIXTURE_SCOPE_CURRENCY = "USD"

# Revalidation failure codes.
EXPIRED = "EXPIRED"
INVENTORY_VERSION_CHANGED = "INVENTORY_VERSION_CHANGED"
PRICE_CHANGED = "PRICE_CHANGED"
CURRENCY_CHANGED = "CURRENCY_CHANGED"
OUT_OF_STOCK = "OUT_OF_STOCK"
UNKNOWN_SKU = "UNKNOWN_SKU"


class QuoteError(ValueError):
    """A quote violates the money contract."""


class QuoteScopeError(QuoteError):
    """The requested quote is outside the offline fixture scope."""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise QuoteError("quote timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def require_minor(value: Any, field: str) -> int:
    """Integer minor units only: no floats, no booleans, no negatives."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise QuoteError(f"{field} must be an integer number of minor units")
    if value < 0:
        raise QuoteError(f"{field} must not be negative")
    return value


def require_currency(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise QuoteError("currency must be a three-letter upper-case ISO code")
    return value


@dataclass(frozen=True, slots=True)
class Quote:
    """Exactly the contract's quote fields, all amounts in integer minor units."""

    quote_id: str
    order_id: str
    candidate_variant_id: str
    original_value_minor: int
    replacement_value_minor: int
    currency: str
    partial_refund_minor: int
    additional_charge_minor: int
    tax_shipping_adjustment_minor: int
    inventory_version: str
    expires_at: datetime
    quote_version: int

    def __post_init__(self) -> None:
        for name in ("quote_id", "order_id", "candidate_variant_id", "inventory_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise QuoteError(f"{name} must be a non-empty string")
        require_minor(self.original_value_minor, "originalValueMinor")
        require_minor(self.replacement_value_minor, "replacementValueMinor")
        require_minor(self.partial_refund_minor, "partialRefundMinor")
        require_minor(self.additional_charge_minor, "additionalChargeMinor")
        require_minor(self.tax_shipping_adjustment_minor, "taxShippingAdjustmentMinor")
        require_currency(self.currency)
        if not isinstance(self.quote_version, int) or isinstance(self.quote_version, bool):
            raise QuoteError("quoteVersion must be an integer")
        if self.quote_version < 1:
            raise QuoteError("quoteVersion starts at 1")
        if self.expires_at.tzinfo is None:
            raise QuoteError("expiresAt must be timezone-aware")

        expected_refund = max(self.original_value_minor - self.replacement_value_minor, 0)
        expected_charge = max(self.replacement_value_minor - self.original_value_minor, 0)
        if self.partial_refund_minor != expected_refund:
            raise QuoteError(
                f"partialRefundMinor {self.partial_refund_minor} != max(original-replacement,0) {expected_refund}"
            )
        if self.additional_charge_minor != expected_charge:
            raise QuoteError(
                f"additionalChargeMinor {self.additional_charge_minor} != "
                f"max(replacement-original,0) {expected_charge}"
            )
        if self.tax_shipping_adjustment_minor != 0:
            raise QuoteScopeError(
                "offline fixture scope assumes no tax/shipping adjustment; "
                "authoritative totals are required before live commerce"
            )
        if self.quote_id in ("", None):  # pragma: no cover - covered above
            raise QuoteError("quoteId is required")

    # -- time ------------------------------------------------------------- #
    @property
    def ttl_seconds(self) -> int:
        return int(load_policy()["quoteTtlSeconds"])

    @property
    def issued_at(self) -> datetime:
        """Derived, not stored: expiry minus the policy TTL.

        The contract fixes the quote's visible fields, and ``expiresAt`` alone is
        enough to recover issuance while the TTL is a frozen contract value.
        """

        return self.expires_at - timedelta(seconds=self.ttl_seconds)

    def is_expired(self, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now >= self.expires_at

    def seconds_remaining(self, now: datetime) -> float:
        return (self.expires_at - now).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        """Wire form with the contract's field names."""

        return {
            "quoteId": self.quote_id,
            "orderId": self.order_id,
            "candidateVariantId": self.candidate_variant_id,
            "originalValueMinor": self.original_value_minor,
            "replacementValueMinor": self.replacement_value_minor,
            "currency": self.currency,
            "partialRefundMinor": self.partial_refund_minor,
            "additionalChargeMinor": self.additional_charge_minor,
            "taxShippingAdjustmentMinor": self.tax_shipping_adjustment_minor,
            "inventoryVersion": self.inventory_version,
            "expiresAt": _iso(self.expires_at),
            "quoteVersion": self.quote_version,
        }


def quote_id_for(
    order_id: str,
    candidate_variant_id: str,
    original_value_minor: int,
    replacement_value_minor: int,
    currency: str,
    inventory_version: str,
    quote_version: int,
    issued_at: datetime,
) -> str:
    """Deterministic, collision-resistant id derived from the exact terms.

    Deterministic ids make the offline build reproducible; any change of terms
    yields a different id, so an approval can never silently apply to new terms.
    """

    payload = "\x1f".join(
        [
            order_id,
            candidate_variant_id,
            str(original_value_minor),
            str(replacement_value_minor),
            currency,
            inventory_version,
            str(quote_version),
            _iso(issued_at),
        ]
    )
    return "qte_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_quote(
    *,
    order_id: str,
    candidate_variant_id: str,
    original_value_minor: int,
    replacement_value_minor: int,
    currency: str,
    inventory_version: str,
    quote_version: int,
    now: datetime,
    policy: Mapping[str, Any] | None = None,
) -> Quote:
    """Mint a fresh quote for the given exact terms."""

    effective = load_policy() if policy is None else policy
    original = require_minor(original_value_minor, "originalValueMinor")
    replacement = require_minor(replacement_value_minor, "replacementValueMinor")
    code = require_currency(currency)
    if code != FIXTURE_SCOPE_CURRENCY:
        raise QuoteScopeError(
            f"offline fixture scope is {FIXTURE_SCOPE_CURRENCY}; {code} needs authoritative totals"
        )
    if now.tzinfo is None:
        raise QuoteError("now must be timezone-aware")
    ttl = timedelta(seconds=float(effective["quoteTtlSeconds"]))
    expires_at = now + ttl
    return Quote(
        quote_id=quote_id_for(
            order_id,
            candidate_variant_id,
            original,
            replacement,
            code,
            inventory_version,
            quote_version,
            now,
        ),
        order_id=order_id,
        candidate_variant_id=candidate_variant_id,
        original_value_minor=original,
        replacement_value_minor=replacement,
        currency=code,
        partial_refund_minor=max(original - replacement, 0),
        additional_charge_minor=max(replacement - original, 0),
        tax_shipping_adjustment_minor=0,
        inventory_version=inventory_version,
        expires_at=expires_at,
        quote_version=quote_version,
    )


def reissue(
    quote: Quote,
    *,
    original_value_minor: int,
    replacement_value_minor: int,
    currency: str,
    inventory_version: str,
    now: datetime,
    policy: Mapping[str, Any] | None = None,
) -> Quote:
    """Mint the successor quote: new id, new version, new TTL, fresh approval.

    Used when a quote expired or when stock/price changed.  The old quote object
    stays in the audit trail and can never authorise anything again.
    """

    return build_quote(
        order_id=quote.order_id,
        candidate_variant_id=quote.candidate_variant_id,
        original_value_minor=original_value_minor,
        replacement_value_minor=replacement_value_minor,
        currency=currency,
        inventory_version=inventory_version,
        quote_version=quote.quote_version + 1,
        now=now,
        policy=policy,
    )


@dataclass(frozen=True, slots=True)
class Revalidation:
    """Result of re-checking a quote against live inventory at commit time."""

    ok: bool
    failures: tuple[str, ...]
    current_price_minor: int | None = None
    current_stock: int | None = None
    current_inventory_version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failures": list(self.failures),
            "currentPriceMinor": self.current_price_minor,
            "currentStock": self.current_stock,
            "currentInventoryVersion": self.current_inventory_version,
        }


def revalidate(
    quote: Quote,
    *,
    now: datetime,
    inventory_version: str | None,
    price_minor: int | None,
    currency: str | None,
    stock: int | None,
) -> Revalidation:
    """Re-check every mutable fact the quote depends on.

    ``None`` means the fact is unknown, and unknown is a failure: a commit may
    never proceed on an assumption about price, currency or stock.
    """

    failures: list[str] = []
    if quote.is_expired(now):
        failures.append(EXPIRED)
    if inventory_version is None:
        failures.append(UNKNOWN_SKU)
    elif inventory_version != quote.inventory_version:
        failures.append(INVENTORY_VERSION_CHANGED)
    if price_minor is None:
        failures.append(UNKNOWN_SKU)
    elif price_minor != quote.replacement_value_minor:
        failures.append(PRICE_CHANGED)
    if currency is None:
        failures.append(UNKNOWN_SKU)
    elif currency != quote.currency:
        failures.append(CURRENCY_CHANGED)
    if stock is None:
        failures.append(UNKNOWN_SKU)
    elif stock < 1:
        failures.append(OUT_OF_STOCK)

    ordered = tuple(code for code in (
        EXPIRED,
        UNKNOWN_SKU,
        INVENTORY_VERSION_CHANGED,
        PRICE_CHANGED,
        CURRENCY_CHANGED,
        OUT_OF_STOCK,
    ) if code in failures)
    return Revalidation(
        ok=not ordered,
        failures=ordered,
        current_price_minor=price_minor,
        current_stock=stock,
        current_inventory_version=inventory_version,
    )
