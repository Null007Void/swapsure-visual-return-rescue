# SwapSure 1.7.15 — release manifest

Published source: v1.7.13 lives at tag v1.7.13 in
https://github.com/Null007Void/swapsure-visual-return-rescue (commit 0b785599011ffa31f629932a2f1caad88f2e74bc).
This 1.7.15 tree is not published yet: it is built and verified locally and awaits an explicit
publication decision. Earlier release identities are preserved and never rewritten.

## What this is

SwapSure turns a disappointing online return into a rescue attempt: the shopper sees what an eligible
alternative would look like on their own photo, then chooses between the exchange and the ordinary
refund. The product runs offline in MOCK mode with no credential, and in LIVE mode against Perfect
Corp's YouCam API when the operator supplies a key.

## Contents

203 tracked files; 202 manifest entries (MANIFEST-PUBLIC.sha256 does not hash itself).

    src/          product code: domain rules, adapters, web UI
    tests/        offline suite, including the hosted-mode and concurrency tests
    assets/       twenty synthetic garment references, their card images and per-file provenance
    contracts/    state machine, policy, eligibility schema and authorization template
    docs/         design, privacy, provider categories, acceptance and hosted-judging plan
    fixtures/     sample catalogue and scenario fixtures
    tools/        specification verifier (strict mode for the private tree, --public for this one)

## Verification

Run under Python 3.13 with Pillow and pytest installed; exact figures are in the release notes of the
tag this archive was built from. The package is produced with `git archive` from that tag, so every
member is the committed byte sequence, and it is compared member by member against the commit.

## Licence

MIT, Copyright (c) 2026 Null007Void | Null Foundry. Third-party components are listed separately in
THIRD-PARTY-NOTICES.md (Pillow, MIT-CMU/HPND, decoding only). The bundled garment references are
synthetic images made for this project; their provenance is recorded under assets/.
