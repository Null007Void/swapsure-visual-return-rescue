"""Authoritative local inventory for the offline simulator.

Stock and price are the only mutable commerce facts.  Every mutation bumps a
revision string, so a quote priced against revision N can be shown to have been
priced against revision N.  Nothing here reads a network or a remote catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable, Mapping

from domain.catalog import InventorySnapshot


class InventoryError(ValueError):
    """Invalid inventory mutation."""


def _require_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InventoryError(f"{field} must be an integer")
    if value < minimum:
        raise InventoryError(f"{field} must be >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class StockRow:
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

    def __post_init__(self) -> None:
        for name in ("sku", "merchant", "category", "size", "currency", "color_family"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InventoryError(f"{name} must be a non-empty string")
        _require_int(self.price_minor, "price_minor")
        _require_int(self.stock, "stock")

    def as_row(self) -> dict[str, Any]:
        """The shape `domain.catalog.Item.parse` consumes."""

        row: dict[str, Any] = {
            "sku": self.sku,
            "merchant": self.merchant,
            "category": self.category,
            "size": self.size,
            "currency": self.currency,
            "priceMinor": self.price_minor,
            "stock": self.stock,
            "colorFamily": self.color_family,
            "referenceRights": self.reference_rights,
        }
        if self.style is not None:
            row["style"] = self.style
        if self.occasion is not None:
            row["occasion"] = self.occasion
        return row

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
            "style": self.style,
            "occasion": self.occasion,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StockRow":
        return cls(
            sku=str(raw["sku"]),
            merchant=str(raw["merchant"]),
            category=str(raw["category"]),
            size=str(raw["size"]),
            currency=str(raw["currency"]),
            price_minor=int(raw["priceMinor"]),
            stock=int(raw["stock"]),
            color_family=str(raw["colorFamily"]),
            reference_rights=str(raw.get("referenceRights", "")),
            style=None if raw.get("style") is None else str(raw["style"]),
            occasion=None if raw.get("occasion") is None else str(raw["occasion"]),
        )


class Inventory:
    """Mutable stock/price table protected by the caller's session lock."""

    def __init__(self, rows: Iterable[StockRow] = (), revision: int = 1) -> None:
        self._rows: dict[str, StockRow] = {}
        self._revision = _require_int(revision, "revision", minimum=1)
        for row in rows:
            if row.sku in self._rows:
                raise InventoryError(f"duplicate SKU in inventory: {row.sku}")
            self._rows[row.sku] = row

    # -- reads ------------------------------------------------------------ #
    @property
    def version(self) -> str:
        return f"inv-{self._revision}"

    @property
    def revision(self) -> int:
        return self._revision

    def get(self, sku: str) -> StockRow | None:
        return self._rows.get(sku)

    def rows(self) -> tuple[StockRow, ...]:
        return tuple(self._rows[sku] for sku in sorted(self._rows))

    def snapshot(self, now: datetime, *, version: str | None = None) -> InventorySnapshot:
        """A read at this instant.  ``version`` may be overridden by a test clock."""

        if now.tzinfo is None:
            raise InventoryError("now must be timezone-aware")
        return InventorySnapshot(
            version=version or self.version,
            captured_at=now,
            rows=tuple(row.as_row() for row in self.rows()),
        )

    # -- writes ----------------------------------------------------------- #
    def add(self, row: StockRow) -> None:
        if row.sku in self._rows:
            raise InventoryError(f"SKU already exists: {row.sku}")
        self._rows[row.sku] = row
        self._revision += 1

    def set_stock(self, sku: str, stock: int, now: datetime) -> str:
        self._require_aware(now)
        row = self._require_row(sku)
        self._rows[sku] = replace(row, stock=_require_int(stock, "stock"))
        self._revision += 1
        return self.version

    def set_price(self, sku: str, price_minor: int, now: datetime) -> str:
        self._require_aware(now)
        row = self._require_row(sku)
        self._rows[sku] = replace(row, price_minor=_require_int(price_minor, "price_minor"))
        self._revision += 1
        return self.version

    def decrement_stock(self, sku: str, now: datetime) -> str:
        """Decrement one unit, refusing when none remain."""

        return self.set_stock(sku, self._require_row(sku).stock - 1, now)

    def _require_row(self, sku: str) -> StockRow:
        row = self._rows.get(sku)
        if row is None:
            raise InventoryError(f"unknown SKU: {sku}")
        return row

    @staticmethod
    def _require_aware(now: datetime) -> None:
        if now.tzinfo is None:
            raise InventoryError("now must be timezone-aware")

    # -- persistence ------------------------------------------------------ #
    def to_state(self) -> dict[str, Any]:
        return {"revision": self._revision, "rows": [row.as_dict() for row in self.rows()]}

    @classmethod
    def from_state(cls, raw: Mapping[str, Any]) -> "Inventory":
        return cls(
            rows=[StockRow.from_dict(item) for item in raw.get("rows", [])],
            revision=int(raw.get("revision", 1)),
        )
