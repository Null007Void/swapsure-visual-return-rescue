"""Fixture-backed catalog access for the offline build.

The fixture in ``fixtures/catalog.json`` is the normative synthetic merchant: one
original line, seven candidate variants, and the expected ranking plus the
$89 -> $84 / $89 -> $99 accounting oracle.  Nothing here invents data: rows that
the fixture does not contain simply do not exist.

This module deliberately depends only on the *value* contracts (``Item``,
``OrderView``, ``InventorySnapshot``) so that fixture reading does not drag in the
commerce store or the provider adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from commerce.order import OrderView
from domain.catalog import (
    CandidateQuery,
    ExchangePolicy,
    InventorySnapshot,
    Item,
    ShopperPreferences,
)
from specs import catalog as load_catalog


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class CatalogFixture:
    original: Item
    rows: tuple[Mapping[str, Any], ...]
    scenario: Mapping[str, Any]
    snapshot_version: str
    clock: datetime

    @property
    def order(self) -> OrderView:
        return OrderView(
            order_id="ORD-FIXTURE-1",
            merchant=self.original.merchant,
            category=self.original.category,
            size=self.original.size,
            color_family=self.original.color_family,
            original_sku=self.original.sku,
            original_value_minor=self.original.price_minor,
            currency=self.original.currency,
        )

    def snapshot(self, captured_at: datetime | None = None) -> InventorySnapshot:
        return InventorySnapshot(
            version=self.snapshot_version,
            captured_at=captured_at or self.clock,
            rows=self.rows,
        )

    def query(
        self,
        *,
        session_id: str = "ses_fixture",
        reason_codes: tuple[str, ...] | None = None,
        captured_at: datetime | None = None,
        shopper_size: str | None = None,
        preferences: ShopperPreferences | None = None,
        max_candidates: int | None = None,
        exchange_policy: ExchangePolicy | None = None,
    ) -> CandidateQuery:
        return CandidateQuery(
            session_id=session_id,
            order=self.original,
            shopper_size=shopper_size or self.original.size,
            reason_codes=(
                tuple(reason_codes) if reason_codes is not None else (str(self.scenario["reason"]),)
            ),
            snapshot=self.snapshot(captured_at),
            preferences=preferences or ShopperPreferences(),
            exchange_policy=exchange_policy or ExchangePolicy(),
            max_candidates=max_candidates,
        )


def load_catalog_fixture() -> CatalogFixture:
    raw = load_catalog()
    original, problems = Item.parse(raw["original"])
    if original is None:
        raise ValueError(f"catalog fixture original line is invalid: {problems}")
    return CatalogFixture(
        original=original,
        rows=tuple(raw["candidates"]),
        scenario=raw["scenario"],
        snapshot_version=str(raw["snapshotVersion"]),
        clock=parse_iso(str(raw["clock"])),
    )


def fixture_now() -> datetime:
    """The fixture's own reference instant, in UTC."""

    return load_catalog_fixture().clock.astimezone(timezone.utc)
