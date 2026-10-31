"""The authoritative order line a rescue session is about.

A pure value object: it carries no store, no lock and no I/O, so the domain
reducer, the simulator and the fixtures can all agree on one shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class OrderView:
    """The order line being returned, as the commerce layer sees it."""

    order_id: str
    merchant: str
    category: str
    size: str
    color_family: str
    original_sku: str
    original_value_minor: int
    currency: str = "USD"

    def __post_init__(self) -> None:
        for name in (
            "order_id",
            "merchant",
            "category",
            "size",
            "color_family",
            "original_sku",
            "currency",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"order.{name} must be a non-empty string")
        if isinstance(self.original_value_minor, bool) or not isinstance(
            self.original_value_minor, int
        ):
            raise ValueError("order.original_value_minor must be an integer")
        if self.original_value_minor < 0:
            raise ValueError("order.original_value_minor must not be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "orderId": self.order_id,
            "merchant": self.merchant,
            "category": self.category,
            "size": self.size,
            "colorFamily": self.color_family,
            "originalSku": self.original_sku,
            "originalValueMinor": self.original_value_minor,
            "currency": self.currency,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OrderView":
        return cls(
            order_id=str(raw["orderId"]),
            merchant=str(raw["merchant"]),
            category=str(raw["category"]),
            size=str(raw["size"]),
            color_family=str(raw["colorFamily"]),
            original_sku=str(raw["originalSku"]),
            original_value_minor=int(raw["originalValueMinor"]),
            currency=str(raw.get("currency", "USD")),
        )
