"""Bundled catalogue data: ten per collection, documented provider categories, valid scenarios."""
from __future__ import annotations

import json

import pytest

from domain import samples
from ui.uploads import parse_upload


def test_ten_unique_references_per_collection():
    for collection, items in samples.COLLECTIONS.items():
        assert len(items) == 10, collection
        skus = [item.sku for item in items]
        assert len(set(skus)) == 10, f"duplicate sku in {collection}"
        for item in items:
            assert item.stock >= 1 and item.price_minor > 0
            assert item.size and item.colour


def test_every_provider_category_is_a_documented_enum_value():
    documented = samples.DOCUMENTED_GARMENT_CATEGORIES
    assert documented == {"full_body", "lower_body", "upper_body", "shoes", "auto", "outer"}
    for item in samples.WOMEN + samples.MEN:
        assert item.provider_category in documented, (item.sku, item.provider_category)
    assert samples.FAMILY_PROVIDER_CATEGORY["outerwear"] == "outer"
    assert samples.FAMILY_PROVIDER_CATEGORY["dress"] == "full_body"
    assert samples.FAMILY_PROVIDER_CATEGORY["bottom"] == "lower_body"


def test_featured_is_five_of_the_ten_and_leads_the_order():
    for collection in samples.COLLECTIONS:
        featured = samples.FEATURED[collection]
        assert len(featured) == 5
        assert set(featured) <= {item.sku for item in samples.COLLECTIONS[collection]}
        order = [item.sku for item in samples.featured(collection)]
        assert order[:5] == list(featured)
        assert len(order) == 10


def test_every_garment_builds_a_scenario_the_upload_validator_accepts():
    for collection, items in samples.COLLECTIONS.items():
        for item in items:
            payload = samples.scenario_payload(collection, item.sku)
            parsed = parse_upload(json.dumps(payload).encode(), content_type="application/json")
            assert parsed is not None
            rows = parsed["rows"] if isinstance(parsed, dict) else parsed.rows
            skus = [(row["sku"] if isinstance(row, dict) else row.sku) for row in rows]
            assert item.sku in skus
            assert len(skus) == 10
            # the original garment is a different colour of the same family
            original = payload["order"]
            assert original["category"] == item.family
            assert original["colorFamily"] != item.colour


def test_scenario_only_reads_collection_and_sku_from_the_browser():
    with pytest.raises(KeyError):
        samples.scenario_payload("women", "NOT-A-SKU")
    with pytest.raises(KeyError):
        samples.scenario_payload("children", "W01-TEAL-WRAP-DRESS")
