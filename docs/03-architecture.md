# Architecture
The proposed baseline is a small web UI, a server-side domain module and a local fixture store. Framework choice is open; use the receiving repository's conventions. No external LLM is required.

## Components and flow
UI → return session → eligibility rules → catalog snapshot → hard filters → deterministic ranking → budget planner → mock or gated YouCam adapter → validated visual evidence → quote → shopper confirmation → commerce adapter → receipt/ledger.

The ordinary return link belongs to the host return flow and is rendered independently of rescue initialization. Rescue availability must never become a prerequisite for that link. If the host itself is unavailable, show its known recovery/contact route and an honest pending state; do not promise completed refunds.

| Component | Authority | Failure behavior |
|---|---|---|
| Eligibility | Classify reason codes only | Ordinary return route |
| Catalog | Read variant stock and policy | No candidate if unknown/stale |
| Ranking | Order already-valid candidates | Ordinary cards or refund |
| Budget planner | Reserve bounded preview allowance | No task if cost unknown or cap exceeded |
| YouCam adapter | Produce visual evidence only | Omit failed preview; preserve cards/refund |
| Quote service | Revalidate price, currency, stock, policy | Expire quote and request a new confirmation |
| Commerce simulator | Atomic local exchange/refund intent | Reconcile unknown status; never double-write |
| Ledger | Record committed outcomes | Transactional outbox/retry; no inflated success count |

## Trust boundaries
The browser cannot set eligibility, price, consent history or approval truth on behalf of the server. Catalog descriptions and provider messages are inert data. Provider callbacks cannot alter transaction state. Credentials eventually stay on the server. Media is separate from metadata, inaccessible to analytics and excluded from logs.

Keep mock and live adapters distinct. The live adapter is not wired into the offline build; missing authorization fails closed for API access. This restriction coexists with fail-open returns. A feature flag or environment variable alone is insufficient authorization.

## Reliability
Use session versioning and atomic compare-and-swap for transitions. Quote and commit share an authoritative inventory check/reservation boundary. Persist idempotency before side effects. On uncertain provider/commerce results, reconcile using the existing task/transaction ID rather than issue a duplicate.

Late preview results are discarded after refund, deletion, consent withdrawal or session expiry. An independent cleanup process must remove expired local media after restart; this is local temporary behavior, not permission to provision a permanent service.
