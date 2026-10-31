# Catalogue garment categories — what is documented and what is not

## The documented parameter

The try-on task endpoint used by this product is `POST /s2s/v2.0/task/cloth-v4` on the provider's
official origin. In the provider's own API reference for that endpoint
(`docs.perfectcorp.com`, section `reference/ai_clothes/v4.0`, page for
`/s2s/v2.0/task/cloth-v4`), the request field `garment_category` is documented as:

* **required** when a reference image is supplied;
* an enum of exactly: `full_body`, `lower_body`, `upper_body`, `shoes`, `auto`, `outer`;
* `auto` lets the engine classify the garment itself;
* the `change_shoes` field only has an effect when the category is `full_body` or `lower_body`.

Checked on 2026-10-31. Quoted here rather than linked from product code, because the product
source is not allowed to contain network destinations.

## The mapping this product uses

| Sample family | Garments | `garment_category` sent | Basis |
| --- | --- | --- | --- |
| dress | dresses, maxidress | `full_body` | documented enum value; a dress covers the body |
| top | blouse, shirt, top, cardigan, polo, overshirt | `upper_body` | documented enum value |
| outerwear | blazer, jacket, coat | `outer` | documented enum value, chosen for garments worn over another layer |
| bottom | trousers, skirt | `lower_body` | documented enum value |

The mapping lives in `src/domain/samples.py` (`FAMILY_PROVIDER_CATEGORY`) and is asserted against
the documented enum by `tests/domain/test_catalog_samples.py`.

## What is *not* established

1. **No image here has been live-qualified.** Every bundled reference carries
   `api_category_verified: false` in its rights file. The parameter is documented; whether the
   provider's engine accepts a given reference image at a given category can only be established by
   spending a real task on it.
2. **`outer` is an interpretation.** The enum lists it; the provider's reference does not spell out
   in the extracted text which garments it is meant for. Jackets, blazers and coats are mapped to it
   because that is the plain reading, and this is the mapping most likely to need revision after the
   first live garment of that family.
3. **One garment per task.** This endpoint takes a single reference image. Nothing in this product
   claims a multi-item outfit is transformed; the collection pages say so explicitly.
