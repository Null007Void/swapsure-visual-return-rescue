"""Constrained candidate selection — `docs/05-decision-contracts.md`.

Every hard filter is applied to every row, all failures are recorded, and nothing
is ever relaxed to fill the three-slot preview plan.  A row with missing or
malformed metadata is excluded rather than guessed at; in particular no size is
predicted, no price is defaulted to zero and no stock is assumed.

Filter order is fixed and every applicable reason is emitted (sorted), so an
exclusion is reviewable rather than a single opaque "no match".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from domain.ranking import RankedCandidate, rank_candidates
from specs import policy as load_policy

# --------------------------------------------------------------------------- #
# Reason codes for exclusion (machine-readable, stable, UI-translatable)
# --------------------------------------------------------------------------- #
MERCHANT_MISMATCH = "MERCHANT_MISMATCH"
CATEGORY_MISMATCH = "CATEGORY_MISMATCH"
SAME_SKU = "SAME_SKU"
CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
SIZE_MISMATCH = "SIZE_MISMATCH"
OUT_OF_STOCK = "OUT_OF_STOCK"
POLICY_NOT_EXCHANGEABLE = "POLICY_NOT_EXCHANGEABLE"
PRICE_DELTA_CAP = "PRICE_DELTA_CAP"
PREFERENCE_CONSTRAINT = "PREFERENCE_CONSTRAINT"
REFERENCE_UNUSABLE = "REFERENCE_UNUSABLE"
ORIGINAL_COLOR_FAMILY = "ORIGINAL_COLOR_FAMILY"
MISSING_METADATA = "MISSING_METADATA"
AMBIGUOUS_DUPLICATE_SKU = "AMBIGUOUS_DUPLICATE_SKU"
DUPLICATE_SKU_IGNORED = "DUPLICATE_SKU_IGNORED"
SNAPSHOT_STALE = "SNAPSHOT_STALE"
SNAPSHOT_CLOCK_SKEW = "SNAPSHOT_CLOCK_SKEW"

ALL_EXCLUSION_REASONS: tuple[str, ...] = (
    MERCHANT_MISMATCH,
    CATEGORY_MISMATCH,
    SAME_SKU,
    CURRENCY_MISMATCH,
    SIZE_MISMATCH,
    OUT_OF_STOCK,
    POLICY_NOT_EXCHANGEABLE,
    PRICE_DELTA_CAP,
    PREFERENCE_CONSTRAINT,
    REFERENCE_UNUSABLE,
    ORIGINAL_COLOR_FAMILY,
    MISSING_METADATA,
    AMBIGUOUS_DUPLICATE_SKU,
    DUPLICATE_SKU_IGNORED,
    SNAPSHOT_STALE,
    SNAPSHOT_CLOCK_SKEW,
)

# Reference material that may be shown as a preview in MOCK mode.  A live
# dispatch would require an owner-verified licence for the exact asset, which
# does not exist in this build, so LIVE has no usable references at all.
MOCK_REFERENCE_VALUES: frozenset[str] = frozenset({"synthetic-placeholder-only"})
LICENSED_REFERENCE_VALUES: frozenset[str] = frozenset()

PREFERENCE_FIELDS: tuple[str, ...] = ("colorFamily", "style", "occasion")

_REQUIRED_TEXT = ("sku", "merchant", "category", "size", "currency", "colorFamily")


def reference_usable(value: Any, mode: str = "MOCK") -> bool:
    """Is this reference usable at all for the given mode?  Fail closed."""

    if mode == "MOCK":
        return isinstance(value, str) and value in MOCK_REFERENCE_VALUES
    return isinstance(value, str) and value in LICENSED_REFERENCE_VALUES


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_currency(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 3 and value.isalpha() and value.isupper()


@dataclass(frozen=True, slots=True)
class Item:
    """A validated order line or catalog variant."""

    sku: str
    merchant: str
    category: str
    size: str
    currency: str
    price_minor: int
    stock: int
    color_family: str
    reference_rights: str
    style: str | None = None
    occasion: str | None = None
    exchange_policy: str | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> tuple["Item | None", tuple[str, ...]]:
        """Parse strictly.  Returns (item, problems); problems name the fields."""

        if not isinstance(raw, Mapping):
            return None, ("<row is not an object>",)

        problems: list[str] = []
        for name in _REQUIRED_TEXT:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                problems.append(name)
        currency = raw.get("currency")
        if isinstance(currency, str) and currency.strip() and not _is_currency(currency):
            problems.append("currency")
        price = raw.get("priceMinor")
        if not _is_int(price) or price < 0:
            problems.append("priceMinor")
        stock = raw.get("stock")
        if not _is_int(stock) or stock < 0:
            problems.append("stock")
        exchange = raw.get("exchangePolicy")
        if exchange is not None and exchange not in ("allowed", "denied"):
            problems.append("exchangePolicy")
        for optional in ("style", "occasion"):
            value = raw.get(optional)
            if value is not None and not isinstance(value, str):
                problems.append(optional)

        if problems:
            return None, tuple(sorted(set(problems)))

        return (
            cls(
                sku=str(raw["sku"]),
                merchant=str(raw["merchant"]),
                category=str(raw["category"]),
                size=str(raw["size"]),
                currency=str(raw["currency"]),
                price_minor=int(raw["priceMinor"]),
                stock=int(raw["stock"]),
                color_family=str(raw["colorFamily"]),
                reference_rights=raw.get("referenceRights") if isinstance(raw.get("referenceRights"), str) else "",
                style=raw.get("style"),
                occasion=raw.get("occasion"),
                exchange_policy=exchange,
            ),
            (),
        )

    def field_value(self, name: str) -> str | None:
        return {
            "colorFamily": self.color_family,
            "style": self.style,
            "occasion": self.occasion,
        }[name]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "merchant": self.merchant,
            "category": self.category,
            "size": self.size,
            "currency": self.currency,
            "priceMinor": self.price_minor,
            "stock": self.stock,
            "colorFamily": self.color_family,
            "referenceRights": self.reference_rights,
            "exchangePolicy": self.exchange_policy,
        }


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """An immutable read of stock/price at a version and capture time."""

    version: str
    captured_at: datetime
    rows: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("inventory snapshot version is required")
        if self.captured_at.tzinfo is None:
            raise ValueError("inventory snapshot captured_at must be timezone-aware")

    @property
    def captured_at_iso(self) -> str:
        return self.captured_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ShopperPreferences:
    """Hard constraints versus ranking preferences — deliberately separate.

    ``constraints`` are equality requirements the shopper made explicit; failing
    one excludes the candidate.  ``preferred`` values only order candidates that
    already passed every filter.
    """

    constraints: Mapping[str, str] = field(default_factory=dict)
    preferred: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in self.constraints:
            if name not in PREFERENCE_FIELDS:
                raise ValueError(f"unknown preference constraint {name!r}")
        for name, values in self.preferred.items():
            if name not in PREFERENCE_FIELDS:
                raise ValueError(f"unknown preference {name!r}")
            if isinstance(values, str) or not values:
                raise ValueError(f"preference {name!r} needs a non-empty tuple of values")

    @property
    def is_empty(self) -> bool:
        return not self.constraints and not self.preferred


@dataclass(frozen=True, slots=True)
class ExchangePolicy:
    """Merchant-side preconditions for an exchange, supplied by the order view."""

    exchange_allowed: bool = True
    max_price_delta_minor: int = 0  # 0 means "use the policy cap only"

    def __post_init__(self) -> None:
        if not _is_int(self.max_price_delta_minor) or self.max_price_delta_minor < 0:
            raise ValueError("max_price_delta_minor must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class Exclusion:
    """Why one row is not a candidate.  Retained for auditability."""

    row_index: int
    sku: str | None
    reasons: tuple[str, ...]
    detail: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "rowIndex": self.row_index,
            "sku": self.sku,
            "reasons": list(self.reasons),
            "detail": list(self.detail),
        }


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    """Result of one deterministic filtering pass."""

    session_id: str
    inventory_version: str
    snapshot_captured_at: str
    snapshot_age_seconds: float
    inventory_fresh: bool
    currency: str
    size: str
    max_price_delta_minor: int
    max_candidates: int
    considered_count: int
    candidates: tuple[RankedCandidate, ...]
    excluded: tuple[Exclusion, ...]
    truncated_skus: tuple[str, ...]

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)

    @property
    def candidate_skus(self) -> tuple[str, ...]:
        return tuple(candidate.sku for candidate in self.candidates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "inventoryVersion": self.inventory_version,
            "snapshotCapturedAt": self.snapshot_captured_at,
            "snapshotAgeSeconds": round(self.snapshot_age_seconds, 3),
            "inventoryFresh": self.inventory_fresh,
            "currency": self.currency,
            "size": self.size,
            "maxPriceDeltaMinor": self.max_price_delta_minor,
            "maxCandidates": self.max_candidates,
            "consideredCount": self.considered_count,
            "candidates": [candidate.ranking_inputs() for candidate in self.candidates],
            "excluded": [item.as_dict() for item in self.excluded],
            "truncatedSkus": list(self.truncated_skus),
        }


@dataclass(frozen=True, slots=True)
class CandidateQuery:
    """Everything a filtering pass depends on, explicit and serialisable."""

    session_id: str
    order: Item
    shopper_size: str
    reason_codes: tuple[str, ...]
    snapshot: InventorySnapshot
    preferences: ShopperPreferences = field(default_factory=ShopperPreferences)
    exchange_policy: ExchangePolicy = field(default_factory=ExchangePolicy)
    max_candidates: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id is required")
        if not isinstance(self.reason_codes, tuple):
            raise ValueError("reason_codes must be a tuple of structured codes")
        if self.max_candidates is not None and (
            not _is_int(self.max_candidates) or self.max_candidates < 0
        ):
            raise ValueError("max_candidates must be a non-negative integer")


def effective_price_cap(query: CandidateQuery, policy: Mapping[str, Any]) -> int:
    """The shopper-approved cap and the policy cap, whichever is tighter."""

    policy_cap = int(policy["fixturePriceDeltaCapMinor"])
    if query.exchange_policy.max_price_delta_minor == 0:
        return policy_cap
    return min(query.exchange_policy.max_price_delta_minor, policy_cap)


def _canonical_value(value: Any) -> Any:
    """Normalise any row value into a deterministic JSON-compatible form.

    Snapshot rows may arrive as read-only mapping views (from the frozen spec
    loader) or as plain dicts; both must canonicalise identically or a duplicated
    row would look like a conflict.
    """

    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return repr(value)


def _canonical(raw: Any) -> str:
    return json.dumps(_canonical_value(raw), sort_keys=True, separators=(",", ":"))


def _snapshot_age_seconds(snapshot: InventorySnapshot, now: datetime) -> float:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return (now - snapshot.captured_at).total_seconds()


def _row_reasons(
    item: Item,
    query: CandidateQuery,
    cap_minor: int,
    exclude_original_color_family: bool,
) -> list[str]:
    """All hard-filter failures for one already-parsed row.  Never relaxes."""

    reasons: list[str] = []
    order = query.order

    if item.merchant != order.merchant:
        reasons.append(MERCHANT_MISMATCH)
    if item.category != order.category:
        reasons.append(CATEGORY_MISMATCH)
    if item.sku == order.sku:
        reasons.append(SAME_SKU)
    if item.currency != order.currency:
        reasons.append(CURRENCY_MISMATCH)
    if item.size != query.shopper_size:
        reasons.append(SIZE_MISMATCH)
    if item.stock < 1:
        reasons.append(OUT_OF_STOCK)

    policy_allows = query.exchange_policy.exchange_allowed and item.exchange_policy != "denied"
    if not policy_allows:
        reasons.append(POLICY_NOT_EXCHANGEABLE)

    if abs(item.price_minor - order.price_minor) > cap_minor:
        reasons.append(PRICE_DELTA_CAP)

    for name, required in query.preferences.constraints.items():
        if item.field_value(name) != required:
            reasons.append(PREFERENCE_CONSTRAINT)
            break

    if not reference_usable(item.reference_rights, "MOCK"):
        reasons.append(REFERENCE_UNUSABLE)

    if exclude_original_color_family and item.color_family == order.color_family:
        reasons.append(ORIGINAL_COLOR_FAMILY)

    return reasons


def _preference_matches(item: Item, preferences: ShopperPreferences) -> tuple[str, ...]:
    matched: list[str] = []
    for name in sorted(preferences.preferred):
        value = item.field_value(name)
        if value is not None and value in preferences.preferred[name]:
            matched.append(name)
    return tuple(matched)


def select_candidates(
    query: CandidateQuery,
    *,
    now: datetime,
    policy: Mapping[str, Any] | None = None,
) -> CandidateSelection:
    """Apply every hard filter, then rank.  Deterministic for a given query/now."""

    effective_policy = load_policy() if policy is None else policy
    freshness_seconds = float(effective_policy["inventoryFreshnessSeconds"])
    cap_minor = effective_price_cap(query, effective_policy)
    limit = int(effective_policy["maxPreviews"]) if query.max_candidates is None else query.max_candidates
    exclude_original_color = "COLOR" in query.reason_codes

    age = _snapshot_age_seconds(query.snapshot, now)
    if age < 0:
        stale_reason: str | None = SNAPSHOT_CLOCK_SKEW
    elif age > freshness_seconds:
        stale_reason = SNAPSHOT_STALE
    else:
        stale_reason = None

    rows = query.snapshot.rows

    if stale_reason is not None:
        # A stale or clock-skewed snapshot is not a partial truth: no candidate
        # may be emitted from it, and no inventory is invented as a substitute.
        excluded = tuple(
            Exclusion(
                row_index=index,
                sku=raw.get("sku") if isinstance(raw, Mapping) else None,
                reasons=(stale_reason,),
                detail=(f"snapshotAgeSeconds={age:.3f}",),
            )
            for index, raw in enumerate(rows)
        )
        return CandidateSelection(
            session_id=query.session_id,
            inventory_version=query.snapshot.version,
            snapshot_captured_at=query.snapshot.captured_at_iso,
            snapshot_age_seconds=age,
            inventory_fresh=False,
            currency=query.order.currency,
            size=query.shopper_size,
            max_price_delta_minor=cap_minor,
            max_candidates=limit,
            considered_count=len(rows),
            candidates=(),
            excluded=excluded,
            truncated_skus=(),
        )

    parsed: list[tuple[int, Item | None, tuple[str, ...], str | None]] = []
    sku_groups: dict[str, list[int]] = {}
    for index, raw in enumerate(rows):
        item, problems = Item.parse(raw)
        sku = item.sku if item is not None else None
        parsed.append((index, item, problems, sku))
        if sku is not None:
            sku_groups.setdefault(sku, []).append(index)

    # A SKU appearing twice is only tolerable when the payloads are identical; a
    # conflicting duplicate makes stock and price unknowable for that SKU, so all
    # of its rows are excluded rather than one being picked.
    conflicting: set[str] = set()
    duplicate_indexes: dict[int, str] = {}
    for sku, indexes in sku_groups.items():
        if len(indexes) < 2:
            continue
        payloads = {_canonical(rows[index]) for index in indexes}
        if len(payloads) > 1:
            conflicting.add(sku)
        else:
            for index in indexes[1:]:
                duplicate_indexes[index] = sku

    included: list[RankedCandidate] = []
    excluded: list[Exclusion] = []
    for index, item, problems, sku in parsed:
        if item is None:
            excluded.append(
                Exclusion(
                    row_index=index,
                    sku=sku,
                    reasons=(MISSING_METADATA,),
                    detail=problems,
                )
            )
            continue
        if sku in conflicting:
            excluded.append(
                Exclusion(
                    row_index=index,
                    sku=sku,
                    reasons=(AMBIGUOUS_DUPLICATE_SKU,),
                    detail=("conflicting payloads for one SKU",),
                )
            )
            continue
        if index in duplicate_indexes:
            excluded.append(
                Exclusion(
                    row_index=index,
                    sku=sku,
                    reasons=(DUPLICATE_SKU_IGNORED,),
                    detail=("identical duplicate row",),
                )
            )
            continue

        reasons = _row_reasons(item, query, cap_minor, exclude_original_color)
        if reasons:
            excluded.append(
                Exclusion(row_index=index, sku=sku, reasons=tuple(sorted(set(reasons))))
            )
            continue

        matched_fields = _preference_matches(item, query.preferences)
        included.append(
            RankedCandidate(
                sku=item.sku,
                merchant=item.merchant,
                category=item.category,
                size=item.size,
                currency=item.currency,
                price_minor=item.price_minor,
                color_family=item.color_family,
                abs_delta_minor=abs(item.price_minor - query.order.price_minor),
                preference_matches=len(matched_fields),
                matched_preference_fields=matched_fields,
            )
        )

    ranked = rank_candidates(included)
    visible = ranked[:limit]
    truncated = tuple(candidate.sku for candidate in ranked[limit:])

    return CandidateSelection(
        session_id=query.session_id,
        inventory_version=query.snapshot.version,
        snapshot_captured_at=query.snapshot.captured_at_iso,
        snapshot_age_seconds=age,
        inventory_fresh=True,
        currency=query.order.currency,
        size=query.shopper_size,
        max_price_delta_minor=cap_minor,
        max_candidates=limit,
        considered_count=len(rows),
        candidates=visible,
        excluded=tuple(sorted(excluded, key=lambda item: item.row_index)),
        truncated_skus=truncated,
    )


def candidate_by_sku(selection: CandidateSelection, sku: str) -> RankedCandidate | None:
    for candidate in selection.candidates:
        if candidate.sku == sku:
            return candidate
    return None


def snapshot_from_rows(
    version: str, captured_at: datetime, rows: Iterable[Mapping[str, Any]]
) -> InventorySnapshot:
    return InventorySnapshot(version=version, captured_at=captured_at, rows=tuple(rows))


def order_item_from_fixture(raw: Mapping[str, Any]) -> Item:
    item, problems = Item.parse(raw)
    if item is None:
        raise ValueError(f"order fixture is not valid: {problems}")
    return item
