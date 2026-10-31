# State machine
The [state graph](../contracts/state-machine.json) defines allowed edges. Guard failures leave state unchanged and retain the ordinary refund route. Server state is authoritative; UI state is only a projection.

| State | Event and guard | Next state |
|---|---|---|
| RETURN_STARTED | Immediate refund intent | REFUND_HANDOFF |
| RETURN_STARTED | Evaluate valid structured reasons | ELIGIBILITY_CHECKED |
| ELIGIBILITY_CHECKED | Ineligible/unknown or decline rescue | REFUND_HANDOFF |
| ELIGIBILITY_CHECKED | Eligible and shopper opts in | CONSENT_PENDING |
| CONSENT_PENDING | Decline or withdraw | REFUND_HANDOFF |
| CONSENT_PENDING | Consent recorded | CANDIDATES_READY |
| CANDIDATES_READY | At least one valid candidate, valid consent and budget | PREVIEW_PENDING |
| CANDIDATES_READY | No candidates | REFUND_HANDOFF |
| PREVIEW_PENDING | Valid result, partial result or bounded failure | DECISION_READY |
| DECISION_READY | Shopper chooses candidate; fresh quote created | QUOTE_READY |
| QUOTE_READY | Shopper explicitly confirms exact unexpired quote | COMMIT_PENDING |
| COMMIT_PENDING | Atomic stock/quote/version check and idempotent commit succeed | EXCHANGE_COMMITTED |
| COMMIT_PENDING | Known failure without side effect | DECISION_READY |
| COMMIT_PENDING | Outcome uncertain | RECONCILING |
| RECONCILING | Prior transaction confirmed committed | EXCHANGE_COMMITTED |
| RECONCILING | Confirmed not committed | REFUND_HANDOFF |
| REFUND_HANDOFF | Shopper uses host refund path | Host owns refund lifecycle |

From every precommit rescue state, Continue refund immediately transitions to REFUND_HANDOFF and invalidates preview/quote work. This action does not wait for cancellation or cleanup.

## Concurrency and finality
A refund click and exchange confirmation race through the same atomic session version lock. If refund wins, exchange is rejected. If commit has already started, the refund request is recorded and ordinary support remains accessible while reconciliation resolves the one transaction; never start a conflicting second payment action. Do not call a refund complete without host confirmation.

EXCHANGE_COMMITTED is terminal for this rescue session, not a waiver of further return rights. Its receipt must retain a normal return/support link. Image deletion is orthogonal to all commerce states and must remain available after completion.

## Evidence lifecycle
Consent version → scoped media upload → preview generation → validated result → expiry/deletion. Revocation cancels future dispatch and discards late results. Deleting imagery does not rewrite the financial audit record; the ledger stores no image bytes or access URLs.
