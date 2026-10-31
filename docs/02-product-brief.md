# Product brief and north star
**SwapSure — Visual Return Rescue.** See the better swap before you take the refund.

SwapSure identifies which initiated apparel returns can benefit from visual evidence, finds valid in-stock replacements under merchant and shopper constraints, and uses YouCam VTO to help the shopper evaluate those alternatives. The shopper explicitly chooses exchange or refund.

The workflow is purchase → dissatisfaction → return intent → deterministic eligibility → constrained alternatives → optional visual evidence → human decision → exchange or ordinary refund. The decision system is the product, not a generic VTO storefront.

## Frozen rules
1. Refund preservation: an obvious, accessible Continue refund route exists throughout rescue. No guilt, countdown, hidden control, forced upload or waiting for VTO.
2. Human authority: selecting a candidate is not exchange approval. Final item, price difference and terms must be confirmed by the shopper.
3. Deterministic eligibility: structured reason codes drive a versioned rule table. Unknown or mixed ambiguous reasons default to ordinary returns.
4. No fit/sizing claims: every preview says “Visual preview only — not a fit or sizing guarantee.” No inference about comfort, material feel or manufacturing quality.
5. No beauty-value judgments: no attractiveness score, preferred skin tone, “best on your body” or algorithmic beauty compatibility.
6. Fail open: rescue failure releases the shopper to the ordinary return route. It does not automatically issue a refund or bypass merchant return policy.
7. No hidden image propagation: only an explicitly consented YouCam path after owner approval may receive imagery; unrelated AI/LLM services receive none.
8. Owner boundaries: no deployment, submission, secrets, API consumption or permanent infrastructure without scoped authorization.

## MVP
One synthetic merchant; apparel only; structured reasons; deterministic hard filters and ranking; at most three previews; explicit consent; mock inventory/commerce; revalidation; idempotent exchange simulation; Rescue Receipt; outcome ledger; session image deletion UX.

Defer live merchant writes, autonomous purchasing, skin/face analysis, beauty scoring, personalized fit advice, LLM concierge, social posting, multilingual expansion and complex infrastructure. Earlier optional facial-color ranking is superseded by the tested concept's metadata-only MVP.

A convincing preview never proves product accuracy. Show garment source cards and let shoppers reject previews. Success means an understandable, voluntary decision, including a refund.
