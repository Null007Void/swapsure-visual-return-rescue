"""Deterministic candidate ordering — `docs/05-decision-contracts.md`.

Sort key (ascending), applied to candidates that already passed every hard
constraint:

1. number of explicit preference matches, **descending**;
2. absolute price difference in minor units, **ascending**;
3. SKU, **lexicographically ascending**.

No demographic, face, body, colour-of-person or "flattering" signal enters the
key.  Those inputs are not parameters of this module, so they cannot be added by
accident.  The SKU component makes the order total, so ties never depend on
iteration order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """An includable candidate plus the exact ranking inputs used for it."""

    sku: str
    merchant: str
    category: str
    size: str
    currency: str
    price_minor: int
    color_family: str
    abs_delta_minor: int
    preference_matches: int
    matched_preference_fields: tuple[str, ...]

    def ranking_inputs(self) -> dict[str, object]:
        """The inputs a reviewer needs to reproduce this candidate's position."""

        return {
            "sku": self.sku,
            "preferenceMatches": self.preference_matches,
            "matchedPreferenceFields": list(self.matched_preference_fields),
            "absDeltaMinor": self.abs_delta_minor,
        }


def ranking_key(candidate: RankedCandidate) -> tuple[int, int, str]:
    """The sort key, exposed so tests and reviewers can assert it directly."""

    return (-candidate.preference_matches, candidate.abs_delta_minor, candidate.sku)


def rank_candidates(candidates: Iterable[RankedCandidate]) -> tuple[RankedCandidate, ...]:
    """Return the candidates in contract order.

    Duplicate SKUs are a specification error: the caller must collapse them before
    ranking so that the order stays total.  Raising is safer than silently
    reordering a shopper-visible list.
    """

    items = list(candidates)
    seen: set[str] = set()
    for item in items:
        if item.sku in seen:
            raise ValueError(f"duplicate SKU reached ranking: {item.sku}")
        seen.add(item.sku)
    return tuple(sorted(items, key=ranking_key))
