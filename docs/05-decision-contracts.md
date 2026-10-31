# Decision contracts
These v1 engineering contracts operationalize the approved concept; they are new specifications, not recovered source code. [Policy](../contracts/policy.json), [schema](../contracts/eligibility.schema.json) and [fixtures](../fixtures/eligibility-cases.json) are normative for the offline baseline.

## Eligibility
Input: session ID, policy version, structured reasonCodes array, immediateRefund boolean. Free text never enables rescue. If the shopper cannot select a recognized reason, return UNKNOWN_REQUIRES_REFUND_PATH.

Precedence: immediate refund; malformed/empty/unknown input; defect; fulfillment error; fit; material; then eligible visual-only reasons. A mixture of color and too-small remains INELIGIBLE_FIT. Unknown mixed with color remains UNKNOWN_REQUIRES_REFUND_PATH. Multiple known ineligible reasons use that fixed precedence.

Output: status, eligible boolean, matchedReasons, policyVersion, explanationCode, refundAvailable=true. eligible is true only for ELIGIBLE_VISUAL_RESCUE. No image is needed for this decision.

## Candidate selection
Input: authoritative order line and variant, currency, category, shopper-selected variant size, color/style/occasion preferences, allowed merchant policy, current inventory snapshot/version/time.

Hard filters: merchant match; category match; distinct SKU; selected size variant actually in stock; same currency; allowed exchange policy; fresh snapshot (offline default 60 seconds); absolute price delta within the shopper-approved cap (fixture default 1500 cents); explicit preference constraints; usable licensed garment reference for preview. For color dissatisfaction, exclude the original color family. Missing stock, price, currency or required metadata excludes the candidate. No size prediction is performed.

Sort valid candidates by number of explicit preference matches descending, absolute price difference ascending, then SKU lexicographically ascending. Never relax a hard constraint to fill three slots. Emit per-candidate inclusion/exclusion reasons, inventory version and ranking inputs. No demographic, face or body features enter ranking.

## Preview plan
At most three candidates. Plan carries sessionId, consentVersion, candidateIds, providerVersion, estimatedUnits, authorizationRef and deadline. MOCK has zero provider units. LIVE requires a verified cost and an owner-approved cumulative cap covering failures and retries. Missing cost or permission means no dispatch.

Result carries candidateId, taskId, mode (MOCK/LIVE/RECORDED), status, createdAt, expiresAt and opaque mediaHandle. No signed URLs in business logs. A recorded asset must have a provenance record. All mock and recorded results are visibly labeled.

## Quote and confirmation
Quote: quoteId, orderId, candidateVariantId, originalValueMinor, replacementValueMinor, currency, partialRefundMinor, additionalChargeMinor, taxShippingAdjustmentMinor, inventoryVersion, expiresAt, quoteVersion.

All amounts are integer minor units. Fixture scope assumes USD, no tax/shipping adjustment: refund=max(original−replacement,0); additional charge=max(replacement−original,0). For real commerce obtain authoritative totals including tax, discounts, shipping and fees; never infer them from sticker prices.

Confirmation: shopper session identity, quoteId/version, explicit action=APPROVE_EXCHANGE, approval timestamp, idempotencyKey. Selection, preview completion, silence or an agent recommendation is not confirmation. Default quote TTL is 60 seconds offline. Any changed price/variant/terms requires a new visible quote and new approval.

Commit enforces session ownership, CSRF protection where applicable, state version, unexpired quote, fresh stock, policy and idempotency. Same key/same payload returns the previous result; same key/different payload is rejected. Uncertain outcomes enter RECONCILING.

## Receipt and ledger
Receipt: return reason, eligibility rationale, policy version, original/replacement SKU and values, stock check time/version, number eligible/previewed, preview mode, shopper approval event, commit ID, partial refund/extra charge, outcome, timestamp.

Record exchange success only after commit acknowledgement. For the $89 → $84 fixture, retained original GMV is $84 and partial refund is $5. For a $99 replacement, retained original GMV is $89 and incremental sales is $10; do not count $99 as original value rescued. Zero retained GMV for failed/pending/refund outcomes. Dedupe by committed transaction ID.
