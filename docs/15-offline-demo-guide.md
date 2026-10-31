# Offline demo guide (Phase C)

Scope: local, offline, synthetic. Nothing here authorises a live YouCam call, a
real merchant write, deployment, publication or submission.

## Start

```
python -m ui.server --port 8000 --data-dir data
```

The server binds `127.0.0.1` only. It serves no static directory, makes no outbound
request and needs no credential. Mode is MOCK: zero provider units.

## Two demo cases

1. **Refusal first (≈20 s).** Start a return, choose **TOO_SMALL**, continue.
   The decision page shows `INELIGIBLE_FIT` and no preview invitation; the refund
   control is visible in the header and in the page body. This is the decision
   system working, not a failure.
2. **Rescue (≈60 s).** New return, choose **COLOR**. The decision is
   `ELIGIBLE_VISUAL_RESCUE`. Opt in, read the consent disclosures, tick both
   consent controls, then generate previews. Two labelled placeholders appear.
   Select `NAVY-84`, read the quote ($89 → $84, $5 partial refund), press the
   explicit approval button, and read the Rescue Receipt: retained original GMV
   $84.00, partial refund $5.00, mode `SIMULATED EXCHANGE - MOCK PREVIEW`.

## Own input file

The home page also accepts a UTF-8 JSON file (64 KiB max). Review the displayed
SHA-256 and byte count, then start the return with it. A malformed, oversized or
schema-violating file is refused with a typed error and nothing is retained from
it. Example: `fixtures/web-scenario.example.json`.

## Honest narration for a recording

- Say "simulated exchange" and "mock preview" out loud; the UI labels both.
- Never imply a placeholder is a YouCam render, and never imply the merchant
  ledger is a real merchant system.
- Show the refund route at least once from a rescue page (header link) so the
  preserved refund path is visible, not merely claimed.
- Do not edit the source, the JSON fixtures or the database to make a take work;
  use `python -m ui.demo reset --data-dir data` instead.
- If a preview fails on camera, leave the failure in: the alternatives and the
  refund stay usable, and that is the fail-open behaviour being demonstrated.
- A later live recording must be labelled differently from this one; a recorded
  fallback is not a fresh live call.

## Reset and inspect

```
python -m ui.demo reset --data-dir data     # wipe cases, media, uploads, journal
python -m ui.demo status --data-dir data    # journal mode, ledger totals, rows
```

`/ledger` shows the local outcome ledger: rows deduplicated by committed
transaction id, zero retained GMV for refunds and failures, and `N/A` conversion
when there is no denominator.

## Known limits

- No photo upload: the offline build never processes shopper imagery, so the
  consent screens describe a future approved flow and the previews are synthetic.
- Placeholder cards are static SVG; they contain no garment rendering.
- One local inventory and one ledger per data directory. An uploaded scenario may
  not reuse an existing SKU or order id with different terms; the app refuses the
  conflict instead of silently rewriting another session's data.
- Local deletion is confirmed separately from provider deletion, which is not
  applicable in MOCK and remains unverified for any future live phase.
