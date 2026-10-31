"""Candidate filtering and ranking — T11, T12 and AC02.

Every assertion is about a hard constraint the contract names.  Where the fixture
does not contain a row, the test adds one to the *snapshot* (synthetic data, no
fabricated inventory in the product) or removes one to prove exclusion.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta

import pytest

from domain.catalog import (
    AMBIGUOUS_DUPLICATE_SKU,
    CURRENCY_MISMATCH,
    DUPLICATE_SKU_IGNORED,
    MERCHANT_MISMATCH,
    MISSING_METADATA,
    ORIGINAL_COLOR_FAMILY,
    OUT_OF_STOCK,
    PRICE_DELTA_CAP,
    REFERENCE_UNUSABLE,
    SAME_SKU,
    SIZE_MISMATCH,
    SNAPSHOT_CLOCK_SKEW,
    SNAPSHOT_STALE,
    CandidateQuery,
    ExchangePolicy,
    ShopperPreferences,
    select_candidates,
)
from domain.ranking import rank_candidates
from specs import policy as load_policy

POLICY = load_policy()


def run(fixture, **kwargs):
    return select_candidates(fixture.query(**kwargs), now=kwargs.pop("now", fixture.clock))


def exclusion_reasons(selection) -> dict[str, tuple[str, ...]]:
    return {item.sku: item.reasons for item in selection.excluded if item.sku is not None}


def exclusion_for_row(selection, row_index: int):
    for item in selection.excluded:
        if item.row_index == row_index:
            return item
    raise AssertionError(f"row {row_index} was neither included nor excluded")


# --------------------------------------------------------------------------- #
# T11 — the fixture oracle
# --------------------------------------------------------------------------- #
def test_fixture_ranking_matches_the_oracle(catalog):
    selection = select_candidates(catalog.query(), now=catalog.clock)
    assert selection.candidate_skus == tuple(catalog.scenario["expectedRankedSkus"])
    assert selection.inventory_fresh is True
    assert selection.inventory_version == catalog.snapshot_version


def test_every_excluded_fixture_row_is_excluded_for_the_documented_reason(catalog):
    selection = select_candidates(catalog.query(), now=catalog.clock)
    reasons = exclusion_reasons(selection)
    assert reasons["RED-84"] == (ORIGINAL_COLOR_FAMILY,)
    assert reasons["EMPTY-80"] == (OUT_OF_STOCK,)
    assert reasons["EUR-84"] == (CURRENCY_MISMATCH,)
    assert reasons["LARGE-84"] == (SIZE_MISMATCH,)
    assert reasons["EXPENSIVE-120"] == (PRICE_DELTA_CAP,)
    assert selection.considered_count == 7


def test_no_candidate_violates_a_hard_constraint(catalog):
    selection = select_candidates(catalog.query(), now=catalog.clock)
    for candidate in selection.candidates:
        assert candidate.merchant == catalog.original.merchant
        assert candidate.category == catalog.original.category
        assert candidate.sku != catalog.original.sku
        assert candidate.currency == catalog.original.currency
        assert candidate.size == catalog.original.size
        assert candidate.abs_delta_minor <= catalog.scenario["maxPriceDeltaMinor"]
        assert candidate.color_family != catalog.original.color_family
        assert candidate.price_minor <= catalog.original.price_minor + catalog.scenario["maxPriceDeltaMinor"]


# --------------------------------------------------------------------------- #
# T11 — stale snapshot and absent stock
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("age_seconds", [61, 120, 3600])
def test_t11_stale_snapshot_yields_no_candidates(catalog, age_seconds):
    stale_at = catalog.clock - timedelta(seconds=age_seconds)
    selection = select_candidates(
        catalog.query(captured_at=stale_at), now=catalog.clock
    )
    assert selection.candidates == ()
    assert selection.inventory_fresh is False
    assert selection.has_candidates is False
    assert {reason for item in selection.excluded for reason in item.reasons} == {SNAPSHOT_STALE}
    assert selection.considered_count == 7


def test_t11_snapshot_exactly_at_the_freshness_boundary_is_accepted(catalog):
    at_boundary = catalog.clock - timedelta(seconds=float(POLICY["inventoryFreshnessSeconds"]))
    selection = select_candidates(catalog.query(captured_at=at_boundary), now=catalog.clock)
    assert selection.inventory_fresh is True
    assert selection.candidate_skus == tuple(catalog.scenario["expectedRankedSkus"])


def test_t11_future_snapshot_is_treated_as_stale(catalog):
    future = catalog.clock + timedelta(seconds=1)
    selection = select_candidates(catalog.query(captured_at=future), now=catalog.clock)
    assert selection.candidates == ()
    assert selection.inventory_fresh is False
    assert {reason for item in selection.excluded for reason in item.reasons} == {SNAPSHOT_CLOCK_SKEW}


def test_t11_all_stock_absent_yields_empty_selection_and_no_fabricated_rows(catalog):
    rows = [dict(row) for row in catalog.rows]
    for row in rows:
        row["stock"] = 0
    query = CandidateQuery(
        session_id="ses-stockless",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert selection.candidates == ()
    assert selection.inventory_fresh is True
    assert selection.considered_count == 7
    assert all(OUT_OF_STOCK in item.reasons for item in selection.excluded)


def test_t11_absent_rows_are_not_replaced_by_neighbouring_rows(catalog):
    rows = tuple(row for row in catalog.rows if row["sku"] in ("LARGE-84", "EUR-84"))
    query = CandidateQuery(
        session_id="ses-sparse",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=rows),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert selection.candidates == ()
    assert selection.considered_count == 2


# --------------------------------------------------------------------------- #
# T12 — wrong currency, size variant, price, merchant, SKU
# --------------------------------------------------------------------------- #
def test_t12_wrong_currency_is_excluded(catalog):
    selection = select_candidates(catalog.query(), now=catalog.clock)
    assert "EUR-84" not in selection.candidate_skus
    assert exclusion_reasons(selection)["EUR-84"] == (CURRENCY_MISMATCH,)


def test_t12_wrong_size_variant_is_excluded_and_never_predicted(catalog):
    selection = select_candidates(catalog.query(), now=catalog.clock)
    assert "LARGE-84" not in selection.candidate_skus
    assert exclusion_reasons(selection)["LARGE-84"] == (SIZE_MISMATCH,)


def test_t12_selecting_another_size_does_not_relabel_a_variant(catalog):
    """No size prediction: picking L shows L stock, not a relabelled M."""

    selection = select_candidates(
        catalog.query(shopper_size="L", reason_codes=("COLOR",)), now=catalog.clock
    )
    assert selection.candidate_skus == ("LARGE-84",)
    assert selection.size == "L"
    # NAVY-84 is an M and must not be offered as if it were an L.
    assert exclusion_reasons(selection)["NAVY-84"] == (SIZE_MISMATCH,)


def test_t12_price_delta_cap_excludes_and_is_never_relaxed(catalog):
    selection = select_candidates(catalog.query(), now=catalog.clock)
    assert "EXPENSIVE-120" not in selection.candidate_skus
    assert exclusion_reasons(selection)["EXPENSIVE-120"] == (PRICE_DELTA_CAP,)


def test_t12_price_delta_cap_boundary_is_inclusive(catalog):
    rows = [dict(row) for row in catalog.rows]
    for row in rows:
        if row["sku"] == "EXPENSIVE-120":
            row["priceMinor"] = catalog.original.price_minor + 1500
    query = CandidateQuery(
        session_id="ses-edge",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert "EXPENSIVE-120" in selection.candidate_skus


def test_t12_tighter_shopper_cap_wins_over_policy_cap(catalog):
    selection = select_candidates(
        catalog.query(exchange_policy=ExchangePolicy(max_price_delta_minor=600)),
        now=catalog.clock,
    )
    assert selection.max_price_delta_minor == 600
    assert selection.candidate_skus == ("NAVY-84",)


def test_t12_merchant_mismatch_is_excluded(catalog):
    rows = [dict(row) for row in catalog.rows]
    rows[0]["merchant"] = "other-store"
    query = CandidateQuery(
        session_id="ses-merchant",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert "NAVY-84" not in selection.candidate_skus
    assert exclusion_reasons(selection)["NAVY-84"] == (MERCHANT_MISMATCH,)


def test_t12_same_sku_is_not_its_own_replacement(catalog):
    rows = [dict(row) for row in catalog.rows]
    clone = dict(catalog.original.as_dict())
    rows.append(clone)
    query = CandidateQuery(
        session_id="ses-same",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert SAME_SKU in exclusion_reasons(selection)[catalog.original.sku]
    assert catalog.original.sku not in selection.candidate_skus


def test_disallowed_exchange_policy_excludes_everything(catalog):
    selection = select_candidates(
        catalog.query(exchange_policy=ExchangePolicy(exchange_allowed=False)), now=catalog.clock
    )
    assert selection.candidates == ()


def test_candidate_level_denied_policy_is_excluded(catalog):
    rows = [dict(row) for row in catalog.rows]
    for row in rows:
        if row["sku"] == "NAVY-84":
            row["exchangePolicy"] = "denied"
    query = CandidateQuery(
        session_id="ses-policy",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert "NAVY-84" not in selection.candidate_skus
    assert "POLICY_NOT_EXCHANGEABLE" in exclusion_reasons(selection)["NAVY-84"]


# --------------------------------------------------------------------------- #
# Missing metadata excludes; nothing is defaulted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field,value",
    [
        ("priceMinor", None),
        ("stock", None),
        ("currency", None),
        ("size", None),
        ("colorFamily", ""),
        ("sku", ""),
        ("merchant", None),
        ("priceMinor", "8400"),
        ("priceMinor", 84.0),
        ("stock", True),
        ("stock", -1),
        ("currency", "usd"),
        ("referenceRights", None),
    ],
)
def test_missing_or_invalid_metadata_excludes_the_row(catalog, field, value):
    rows = [dict(row) for row in catalog.rows]
    rows[0][field] = value
    query = CandidateQuery(
        session_id="ses-meta",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert "NAVY-84" not in selection.candidate_skus
    reasons = exclusion_for_row(selection, 0).reasons
    assert MISSING_METADATA in reasons or REFERENCE_UNUSABLE in reasons


def test_reference_rights_must_be_usable_for_preview(catalog):
    rows = [dict(row) for row in catalog.rows]
    rows[0]["referenceRights"] = "unlicensed-competitor-photo"
    query = CandidateQuery(
        session_id="ses-ref",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert exclusion_reasons(selection)["NAVY-84"] == (REFERENCE_UNUSABLE,)


def test_live_mode_has_no_usable_reference(catalog):
    from domain.catalog import reference_usable

    assert reference_usable("synthetic-placeholder-only", "MOCK") is True
    assert reference_usable("synthetic-placeholder-only", "LIVE") is False
    assert reference_usable("licensed-anything", "LIVE") is False


# --------------------------------------------------------------------------- #
# Color family exclusion is conditional on the COLOR reason
# --------------------------------------------------------------------------- #
def test_color_reason_excludes_the_original_color_family(catalog):
    selection = select_candidates(catalog.query(reason_codes=("COLOR",)), now=catalog.clock)
    assert exclusion_reasons(selection)["RED-84"] == (ORIGINAL_COLOR_FAMILY,)


def test_style_reason_does_not_exclude_the_original_color_family(catalog):
    selection = select_candidates(catalog.query(reason_codes=("STYLE",)), now=catalog.clock)
    assert "RED-84" in selection.candidate_skus


def test_color_family_exclusion_uses_structured_reason_only(catalog):
    selection = select_candidates(
        catalog.query(reason_codes=("VISUAL_EXPECTATION",)), now=catalog.clock
    )
    assert "RED-84" in selection.candidate_skus


# --------------------------------------------------------------------------- #
# Duplicate SKUs
# --------------------------------------------------------------------------- #
def test_identical_duplicate_rows_collapse_to_one_candidate(catalog):
    rows = list(catalog.rows) + [dict(catalog.rows[0])]
    query = CandidateQuery(
        session_id="ses-dupe",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert selection.candidate_skus.count("NAVY-84") == 1
    reasons = exclusion_reasons(selection)
    assert DUPLICATE_SKU_IGNORED in reasons["NAVY-84"]


def test_conflicting_duplicate_rows_exclude_that_sku_entirely(catalog):
    rows = list(catalog.rows) + [{**dict(catalog.rows[0]), "priceMinor": 8300}]
    query = CandidateQuery(
        session_id="ses-dupe2",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert "NAVY-84" not in selection.candidate_skus
    assert AMBIGUOUS_DUPLICATE_SKU in exclusion_reasons(selection)["NAVY-84"]


# --------------------------------------------------------------------------- #
# Never fill three slots
# --------------------------------------------------------------------------- #
def test_never_relaxes_to_fill_slots(catalog):
    """Only one row survives; the answer is one candidate, not three."""

    rows = [dict(row) for row in catalog.rows]
    for row in rows:
        if row["sku"] in ("GREEN-99", "RED-84"):
            row["stock"] = 0
    query = CandidateQuery(
        session_id="ses-one",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert selection.candidate_skus == ("NAVY-84",)


def test_limit_truncates_but_never_widens_constraints(catalog):
    rows = [dict(row) for row in catalog.rows]
    for index, name in enumerate(("A-84", "B-84", "C-84")):
        rows.append({**dict(catalog.rows[0]), "sku": name, "priceMinor": 8400 + index * 100})
    query = CandidateQuery(
        session_id="ses-many",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
    )
    selection = select_candidates(query, now=catalog.clock)
    assert len(selection.candidates) == int(POLICY["maxPreviews"]) == 3
    # Delta ascending: C-84 (300), B-84 (400), A-84 (500) then NAVY-84 (500, SKU
    # tie-break) and GREEN-99 (1000).  Truncation is recorded, never relaxed.
    assert selection.truncated_skus == ("NAVY-84", "GREEN-99")
    for candidate in selection.candidates:
        assert candidate.abs_delta_minor <= 1500


def test_max_candidates_override_is_honoured(catalog):
    selection = select_candidates(catalog.query(max_candidates=1), now=catalog.clock)
    assert selection.candidate_skus == ("NAVY-84",)
    assert selection.truncated_skus == ("GREEN-99",)


# --------------------------------------------------------------------------- #
# Preferences: constraints are hard, preferences only rank
# --------------------------------------------------------------------------- #
def test_preference_constraint_is_a_hard_filter(catalog):
    preferences = ShopperPreferences(constraints={"colorFamily": "green"})
    selection = select_candidates(catalog.query(preferences=preferences), now=catalog.clock)
    assert selection.candidate_skus == ("GREEN-99",)
    assert "PREFERENCE_CONSTRAINT" in exclusion_reasons(selection)["NAVY-84"]


def test_preference_match_outranks_a_smaller_price_delta(catalog):
    rows = [dict(row) for row in catalog.rows]
    rows[0]["style"] = "midi"
    preferences = ShopperPreferences(preferred={"style": ("midi",)})
    query = CandidateQuery(
        session_id="ses-pref",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
        preferences=preferences,
    )
    selection = select_candidates(query, now=catalog.clock)
    # NAVY-84 has the better delta and one preference match; GREEN-99 has none.
    assert selection.candidate_skus == ("NAVY-84", "GREEN-99")
    assert selection.candidates[0].preference_matches == 1


def test_preference_ordering_beats_price_when_matches_differ(catalog):
    rows = [dict(row) for row in catalog.rows]
    rows[0]["style"] = "midi"  # NAVY-84, delta 500
    rows[1]["style"] = "midi"  # GREEN-99, delta 1000
    rows[1]["occasion"] = "work"
    preferences = ShopperPreferences(preferred={"style": ("midi",), "occasion": ("work",)})
    query = CandidateQuery(
        session_id="ses-pref2",
        order=catalog.original,
        shopper_size=catalog.original.size,
        reason_codes=("COLOR",),
        snapshot=replace(catalog.snapshot(), rows=tuple(rows)),
        preferences=preferences,
    )
    selection = select_candidates(query, now=catalog.clock)
    assert selection.candidate_skus == ("GREEN-99", "NAVY-84")
    assert selection.candidates[0].preference_matches == 2


def test_unknown_preference_field_is_rejected(catalog):
    with pytest.raises(ValueError):
        ShopperPreferences(preferred={"bodyType": ("hourglass",)})


def test_no_demographic_or_body_inputs_exist(catalog):
    """Ranking inputs are metadata only; there is no parameter to supply appearance."""

    from domain.ranking import RankedCandidate

    fields = set(RankedCandidate.__dataclass_fields__)
    for forbidden in ("height", "weight", "body", "face", "skin", "age", "gender", "attractiveness"):
        assert forbidden not in fields
    assert fields == {
        "sku",
        "merchant",
        "category",
        "size",
        "currency",
        "price_minor",
        "color_family",
        "abs_delta_minor",
        "preference_matches",
        "matched_preference_fields",
    }


# --------------------------------------------------------------------------- #
# Determinism and auditability
# --------------------------------------------------------------------------- #
def test_selection_is_deterministic_across_repeated_calls(catalog):
    first = select_candidates(catalog.query(), now=catalog.clock)
    second = select_candidates(catalog.query(), now=catalog.clock)
    assert first == second
    assert first.as_dict() == second.as_dict()


def test_selection_emits_inventory_version_and_ranking_inputs(catalog):
    payload = select_candidates(catalog.query(), now=catalog.clock).as_dict()
    assert payload["inventoryVersion"] == catalog.snapshot_version
    assert payload["currency"] == "USD"
    assert payload["size"] == "M"
    assert payload["maxPriceDeltaMinor"] == 1500
    for candidate in payload["candidates"]:
        assert set(candidate) == {
            "sku",
            "preferenceMatches",
            "matchedPreferenceFields",
            "absDeltaMinor",
        }
        assert candidate["absDeltaMinor"] >= 0


def test_every_exclusion_carries_at_least_one_reason(catalog):
    selection = select_candidates(catalog.query(), now=catalog.clock)
    for item in selection.excluded:
        assert item.reasons
        assert all(isinstance(code, str) and code.isupper() for code in item.reasons)


def test_ranking_key_is_exposed_and_total(catalog):
    from domain.ranking import ranking_key

    selection = select_candidates(catalog.query(), now=catalog.clock)
    keys = [ranking_key(candidate) for candidate in selection.candidates]
    assert keys == sorted(keys)
    assert len({key[2] for key in keys}) == len(keys)


def test_snapshot_rows_are_not_mutated_by_selection(catalog):
    before = copy.deepcopy([dict(row) for row in catalog.rows])
    select_candidates(catalog.query(), now=catalog.clock)
    after = [dict(row) for row in catalog.rows]
    assert before == after
