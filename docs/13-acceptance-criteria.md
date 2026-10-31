# Acceptance criteria
Every criterion needs executable evidence from the future app. Package verification alone meets AC00 only.

| ID | Acceptance condition | Test mapping | Gate |
|---|---|---|---|
| AC00 | Complete package, valid local links, JSON and hashes; ZIP matches folder | tools/verify_package.py | A |
| AC01 | Exact reason mapping and fail-open unknowns; no inference from free text | T01–T08 | B |
| AC02 | Every candidate satisfies hard constraints; stable tie-break order | T11–T14 | B |
| AC03 | Explicit shopper confirmation and idempotent atomic commit | T13–T17, T21, T30 | B |
| AC04 | Ledger uses committed outcomes and correct cents; zero-denominator safe | T34, T38 | B |
| AC05 | Continue refund works from all precommit states and when rescue cannot load | T05, T09, T18, T31, T35 | C |
| AC06 | No image dispatch before consent; revocation/deletion enforced | T09, T10, T24–T26, T36 | C |
| AC07 | No unauthorized API calls, credentials or media egress | T20, T22, T23, T25, T27, T28, T37 | C and D |
| AC08 | Bounded async handling; no stale/cross-candidate result | T18–T20, T29 | C and D |
| AC09 | No fit/beauty claims; mode and limitations visible | T32, T33 | C |
| AC10 | Both demo cases and failure path run reproducibly | T01, T08, T18, T31 | C |
| AC11 | Authorized actual YouCam evidence, verified cost/version and privacy terms | Scoped live test report | D |
| AC12 | Approved deployment reachable with defined lifetime/cost and cleanup | Deployment report | E |
| AC13 | Owner-approved final materials, eligibility and rights review complete | Submission checklist | F |

Release requires zero unresolved P0 failures. Record P1 remediation before judge demo. Review actual UI and network behavior, not just unit tests. No agent may waive AC03, AC05, AC06 or AC07 for a demo deadline.

G-B review can be performed by the receiving agent with evidence. G-C readiness is not owner authorization for D. All D/E/F approvals remain pending.
