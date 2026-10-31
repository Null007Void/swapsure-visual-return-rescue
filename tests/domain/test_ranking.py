"""Deterministic ordering — the sort contract of `docs/05-decision-contracts.md`."""

from __future__ import annotations

import itertools

import pytest

from domain.ranking import RankedCandidate, rank_candidates, ranking_key


def make(sku: str, *, price: int = 8400, original: int = 8900, matches: int = 0, fields=()) -> RankedCandidate:
    return RankedCandidate(
        sku=sku,
        merchant="demo",
        category="dress",
        size="M",
        currency="USD",
        price_minor=price,
        color_family="navy",
        abs_delta_minor=abs(price - original),
        preference_matches=matches,
        matched_preference_fields=fields,
    )


def test_preference_matches_descend():
    ranked = rank_candidates([make("A", matches=0), make("B", matches=2), make("C", matches=1)])
    assert [item.sku for item in ranked] == ["B", "C", "A"]


def test_absolute_delta_ascends():
    ranked = rank_candidates(
        [make("A", price=9900), make("B", price=8400), make("C", price=8900)]
    )
    assert [item.sku for item in ranked] == ["C", "B", "A"]


def test_sku_breaks_ties_lexicographically():
    ranked = rank_candidates([make("Z-84"), make("A-84"), make("M-84")])
    assert [item.sku for item in ranked] == ["A-84", "M-84", "Z-84"]


def test_key_order_is_preference_then_delta_then_sku():
    candidate = make("B", price=8400, original=8900, matches=1, fields=("style",))
    assert ranking_key(candidate) == (-1, 500, "B")


def test_negative_and_positive_deltas_compare_by_magnitude():
    cheap = make("CHEAP", price=8000)
    dear = make("DEAR", price=9900)
    ranked = rank_candidates([dear, cheap])
    assert [item.sku for item in ranked] == ["CHEAP", "DEAR"]
    assert ranked[0].abs_delta_minor == 900


def test_ranking_is_order_independent_for_every_permutation():
    items = [make("A"), make("B", price=8500), make("C", matches=1)]
    expected = [item.sku for item in rank_candidates(items)]
    for permutation in itertools.permutations(items):
        assert [item.sku for item in rank_candidates(permutation)] == expected


def test_duplicate_sku_is_rejected_by_ranking():
    with pytest.raises(ValueError):
        rank_candidates([make("A"), make("A")])


def test_empty_input_is_empty_output():
    assert rank_candidates([]) == ()


def test_ranking_inputs_are_exposed_for_review():
    candidate = make("A", matches=2, fields=("occasion", "style"))
    payload = candidate.ranking_inputs()
    assert payload == {
        "sku": "A",
        "preferenceMatches": 2,
        "matchedPreferenceFields": ["occasion", "style"],
        "absDeltaMinor": 500,
    }
