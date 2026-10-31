# Third-party notices

Kept separate from `LICENSE` (MIT, original code only). Nothing here is relicensed by the project.

## Runtime

| Component | Version | Licence | Use |
|---|---|---|---|
| Pillow | >= 10, < 13 | MIT-CMU (HPND) | decoding and validating provider result images (PNG and JPEG) |

Pillow is used only to decode bytes the provider returned. It is not used to encode, to accept an
upload, or to decide whether a preview may be shown: that decision stays with the bounded checks in
`src/adapters/youcam_live.py`. No vendored SDK, no provider client library, and no bundled image,
font, logo or product photography.

Licence text: Pillow is distributed under the MIT-CMU / HPND licence; the full text ships with the
installed package (`PIL/LICENSE`). Nothing here is relicensed by this project.

## Test-only (not required to run the product)

| Component | Version | Licence | Use |
|---|---|---|---|
| pytest | >= 8 | MIT | running the offline suite |
| jsonschema | >= 4 | MIT | validating decision output against the normative schema |

Neither is imported by `src/`.

## Services

**YouCam AI Clothes (Perfect Corp).** Used over its public HTTP API only when the operator supplies
their own credentials and explicit authorization. No code, model, weight or media from Perfect Corp is
distributed here. The vendor's terms, quota and unit pricing govern use. The project makes no
representation about the provider's retention or deletion behaviour. "Perfect Corp" and "YouCam" are
used descriptively; no endorsement or trademark right is implied.

## Assets

All catalogue items, garment references and preview cards are synthetic or locally generated. No
third-party product photography, no brand imagery, and no person's likeness is included.

## Attribution

Original project code: **Null007Void / Null Foundry**. Retain the `LICENSE` copyright line in
redistributions and in any derivative interface.
