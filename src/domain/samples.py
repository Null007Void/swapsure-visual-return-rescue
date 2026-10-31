"""Bundled sample catalog for the recording walkthrough.

Not merchant inventory: every garment is a synthetic reference staged from the owner's archives,
and every price/stock value is sample data. The provider category is a *descriptive mapping*
(see docs/CATALOG-PROVIDER-CATEGORIES.md); it is not a live qualification of the image.

Garment selection here is not consent, not generation and not exchange approval. It only chooses
which alternative the shopper wants to try on, and it is bound on the server: the browser never
supplies a path, a URL or a category.
"""
from __future__ import annotations

from dataclasses import dataclass

# Families exist because the eligibility rules match on category. A shopper returning a top is
# offered tops; a shopper returning a dress is offered dresses. Descriptive garment types stay on
# the card for the human reader.
FAMILY_LABELS = {
    "dress": "Dresses",
    "top": "Tops",
    "outerwear": "Outerwear",
    "bottom": "Trousers and skirts",
}

# Verified 2026-10-31 against the provider's official Cloth V4 reference for
# POST /s2s/v2.0/task/cloth-v4 (Perfect Corp developer documentation; the exact page is cited in
# docs/CATALOG-PROVIDER-CATEGORIES.md, not as a network destination in product code):
#   garment_category is required when a reference image is sent, and its enum is
#   "full_body" | "lower_body" | "upper_body" | "shoes" | "auto" | "outer".
# Choosing "outer" for jackets, blazers and coats is our reading of that enum; the parameter is
# documented, but no image here has been live-qualified, so each reference still carries
# api_category_verified: false in its rights file.
DOCUMENTED_GARMENT_CATEGORIES = frozenset(
    {"full_body", "lower_body", "upper_body", "shoes", "auto", "outer"}
)
FAMILY_PROVIDER_CATEGORY = {
    "dress": "full_body",
    "top": "upper_body",
    "outerwear": "outer",
    "bottom": "lower_body",
}


@dataclass(frozen=True, slots=True)
class Item:
    sku: str
    name: str
    garment_type: str
    family: str
    price_minor: int
    stock: int
    size: str = "M"
    colour: str = "navy"

    @property
    def provider_category(self) -> str:
        return FAMILY_PROVIDER_CATEGORY[self.family]

    @property
    def selectable(self) -> bool:
        return self.stock > 0


WOMEN: tuple[Item, ...] = (
    Item("W01-TEAL-WRAP-DRESS", "Teal wrap midi dress", "dress", "dress", 8400, 3, colour="teal"),
    Item("W02-IVORY-LACE-BLOUSE", "Ivory lace blouse", "blouse", "top", 6200, 2, colour="ivory"),
    Item("W03-BLACK-PINSTRIPE-BLAZER", "Black pinstripe blazer", "blazer", "outerwear", 11800, 1, colour="black"),
    Item("W04-RUST-WIDELEG-TROUSERS", "Rust wide-leg trousers", "trousers", "bottom", 7400, 2, colour="rust"),
    Item("W05-LAVENDER-PLEATED-SKIRT", "Lavender pleated skirt", "skirt", "bottom", 6800, 2, colour="lavender"),
    Item("W06-RED-FLORAL-MAXIDRESS", "Red floral maxi dress", "dress", "dress", 9600, 1, colour="red"),
    Item("W07-MUSTARD-CABLE-CARDIGAN", "Mustard cable cardigan", "cardigan", "top", 5800, 3, colour="mustard"),
    Item("W08-BLUE-GEOMETRIC-SHIRT", "Blue geometric shirt", "shirt", "top", 5400, 2, colour="blue"),
    Item("W09-OLIVE-UTILITY-JACKET", "Olive utility jacket", "jacket", "outerwear", 10200, 1, colour="olive"),
    Item("W10-CORAL-RUFFLED-TOP", "Coral ruffled top", "top", "top", 4900, 3, colour="coral"),
)

MEN: tuple[Item, ...] = (
    Item("M01-ASYMMETRIC-KNIT-POLO", "Ink-plum knit polo", "polo", "top", 7800, 2, colour="plum"),
    Item("M02-COLORBLOCK-OVERSHIRT", "Colour-block overshirt", "overshirt", "top", 8900, 2, colour="multi"),
    Item("M03-TECHNICAL-JACKET", "Technical jacket", "jacket", "outerwear", 12800, 1, colour="graphite"),
    Item("M04-TEXTURED-CAMP-SHIRT", "Textured camp shirt", "shirt", "top", 5900, 3, colour="sand"),
    Item("M05-TAILORED-UTILITY-TROUSERS", "Tailored utility trousers", "trousers", "bottom", 8400, 2, colour="stone"),
    Item("M06-OLIVE-FIELD-JACKET", "Olive field jacket", "jacket", "outerwear", 11200, 1, colour="olive"),
    Item("M07-BLUE-DENIM-OVERSHIRT", "Blue denim overshirt", "overshirt", "top", 8200, 2, colour="denim"),
    Item("M08-TAN-TRENCH-COAT", "Tan trench coat", "coat", "outerwear", 14900, 1, colour="tan"),
    Item("M09-RUST-CHECK-FLANNEL", "Rust check flannel", "shirt", "top", 6400, 2, colour="rust"),
    Item("M10-BLACK-BOMBER-JACKET", "Black bomber jacket", "jacket", "outerwear", 10400, 3, colour="black"),
)

COLLECTIONS: dict[str, tuple[Item, ...]] = {"women": WOMEN, "men": MEN}
COLLECTION_LABELS = {"women": "Women's collection", "men": "Men's collection"}

# Featured first: the five per collection with the clearest single-garment presentation, best
# variety of family, and stock available. Order is editorial, not a compatibility claim.
FEATURED: dict[str, tuple[str, ...]] = {
    "women": ("W01-TEAL-WRAP-DRESS", "W02-IVORY-LACE-BLOUSE", "W03-BLACK-PINSTRIPE-BLAZER",
              "W05-LAVENDER-PLEATED-SKIRT", "W04-RUST-WIDELEG-TROUSERS"),
    "men": ("M01-ASYMMETRIC-KNIT-POLO", "M02-COLORBLOCK-OVERSHIRT", "M03-TECHNICAL-JACKET",
            "M05-TAILORED-UTILITY-TROUSERS", "M04-TEXTURED-CAMP-SHIRT"),
}

# The item the shopper says they are returning. One per family per collection, so the selected
# alternative is always comparable and the return/alternative distinction stays concrete.
_ORIGINALS = {
    ("women", "dress"): ("W-ORIG-BURGUNDY-DRESS", "Burgundy wrap dress", 8900, "M", "burgundy"),
    ("women", "top"): ("W-ORIG-NAVY-SHIRT", "Navy poplin shirt", 5600, "M", "navy"),
    ("women", "outerwear"): ("W-ORIG-CHARCOAL-BLAZER", "Charcoal blazer", 12400, "M", "charcoal"),
    ("women", "bottom"): ("W-ORIG-BLACK-TROUSERS", "Black straight trousers", 7600, "M", "black"),
    ("men", "top"): ("M-ORIG-GREY-SHIRT", "Grey Oxford shirt", 6500, "M", "grey"),
    ("men", "outerwear"): ("M-ORIG-NAVY-JACKET", "Navy chore jacket", 11600, "M", "navy"),
    ("men", "bottom"): ("M-ORIG-BLACK-CHINOS", "Black chinos", 7800, "M", "black"),
}


def provider_category_for(sku: str) -> str | None:
    """The verified provider category for a bundled garment, or None if it is not ours."""
    for items in COLLECTIONS.values():
        for item in items:
            if item.sku == sku:
                return item.provider_category
    return None


def collection_for(sku: str) -> str | None:
    """Which bundled collection a sku belongs to, or None if it is not ours."""
    for name, items in COLLECTIONS.items():
        for item in items:
            if item.sku == sku:
                return name
    return None


def item_for(collection: str, sku: str) -> Item:
    for item in COLLECTIONS[collection]:
        if item.sku == sku:
            return item
    raise KeyError(sku)


def featured(collection: str) -> tuple[Item, ...]:
    order = {sku: index for index, sku in enumerate(FEATURED[collection])}
    return tuple(sorted(COLLECTIONS[collection], key=lambda i: (order.get(i.sku, 99), i.sku)))


def _row(item: Item, *, merchant: str = "demo") -> dict[str, object]:
    return {
        "sku": item.sku, "merchant": merchant, "category": item.family, "size": item.size,
        "currency": "USD", "priceMinor": item.price_minor, "stock": item.stock,
        "colorFamily": item.colour, "referenceRights": "synthetic-placeholder-only",
        "style": item.garment_type, "occasion": "everyday",
    }


def scenario_payload(collection: str, sku: str) -> dict[str, object]:
    """Build the sample return for one selected garment.

    The order is the shopper's own garment (a different colour of the same family), the catalog is
    the bundled collection, and the selected sku is present in it. Nothing here is browser-supplied
    except which collection and which sku were clicked, and both are checked against this table.
    """
    chosen = item_for(collection, sku)
    original = _ORIGINALS[(collection, chosen.family)]
    order = {
        "orderId": f"ORD-{collection.upper()}-{chosen.family.upper()}-SAMPLE",
        "merchant": "demo",
        "category": chosen.family,
        "size": original[3],
        "currency": "USD",
        "priceMinor": original[2],
        "colorFamily": original[4],
        "sku": original[0],
    }
    return {
        "order": order,
        "catalog": [_row(item) for item in COLLECTIONS[collection]],
        "reasonCodes": ["COLOR"],
        "preferences": {},
    }
