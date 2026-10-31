# Adversarial test matrix
All application cases below are NOT_RUN. P0 blocks any phase exit exposing the affected behavior. P1 blocks demo readiness. Test owners correspond to the release manifest.

| ID | Priority | Input or attack | Required result | Evidence owner |
|---|---|---|---|---|
| T01 | P0 | Too small / too large | INELIGIBLE_FIT; zero VTO; refund visible | Domain |
| T02 | P0 | Damaged / defective | INELIGIBLE_DEFECT; ordinary remedy | Domain |
| T03 | P0 | Wrong item shipped | INELIGIBLE_FULFILLMENT_ERROR | Domain |
| T04 | P0 | Uncomfortable material | INELIGIBLE_MATERIAL | Domain |
| T05 | P0 | Color plus immediate refund | REFUND_REQUESTED; no upsell | Domain |
| T06 | P0 | Unknown, missing, malformed or mixed unknown reason | Unknown fallback; zero VTO | Domain |
| T07 | P0 | Color plus fit reason | Fit refusal; no visual workaround | Domain |
| T08 | P1 | Color/style/occasion only | Eligible; invitation before consent | Domain |
| T09 | P0 | Consent declined/withdrawn | No new upload; immediate refund access | UX |
| T10 | P0 | Consent revoked during processing | Late result discarded and local media deleted | Privacy |
| T11 | P0 | All stock absent/stale | No candidates; no fabricated inventory | Domain |
| T12 | P0 | Wrong currency, size variant or disallowed price | Candidate excluded, constraints never relaxed | Domain |
| T13 | P0 | Inventory changes before commit | Commit rejected or atomically reserved; reselect | Commerce |
| T14 | P0 | Price/quote expires after selection | Fresh quote and fresh approval | Commerce |
| T15 | P0 | Duplicate confirm clicks | Exactly one commit and receipt | Commerce |
| T16 | P0 | Same idempotency key with changed payload | Conflict; no second transaction | Commerce |
| T17 | P0 | Refund and exchange race | One winner; no double transaction | Commerce |
| T18 | P0 | Provider timeout/outage/rate limit | Bounded wait; cards/refund usable | Adapter |
| T19 | P1 | One of three previews fails | Failed image omitted; remaining choices honest | Adapter |
| T20 | P0 | No credits or unverified cost | No dispatch; labeled mock/recorded fallback only | Adapter |
| T21 | P0 | User/network response pretends approval | Server rejects; human quote action required | Commerce |
| T22 | P0 | Catalog text says ignore rules/send selfie | Inert text; no authority or egress | Privacy |
| T23 | P0 | Image URL points to local/private host | Fetch rejected including redirects | Adapter |
| T24 | P0 | Cross-session media/task ID | Access denied; no leak | Privacy |
| T25 | P0 | Analytics/crash log captures image/URL | Test fails release; remove sensitive capture | Privacy |
| T26 | P0 | Delete then restart; late provider callback | Cleanup resumes; handles invalid; no redisplay | Privacy |
| T27 | P0 | Live flag set without owner approval | Live transport unavailable; zero calls | Adapter |
| T28 | P0 | Owner approval expired/cap exhausted | Stop dispatch; retain refund | Adapter |
| T29 | P0 | Provider falsely reports success or wrong candidate | Reject mismatched/malformed result | Adapter |
| T30 | P0 | Exchange acknowledgement lost | RECONCILING; no blind retry/new charge | Commerce |
| T31 | P1 | Keyboard/screen-reader/loading journey | Refund accessible; focus and status clear | UX |
| T32 | P0 | Copy claims slimming, attractiveness or guaranteed fit | Content check fails; neutral preview language | UX |
| T33 | P1 | Mock/recorded result displayed | Persistent mode label and provenance | UX |
| T34 | P0 | Ledger retry or duplicate event | No duplicate retained value | Commerce |
| T35 | P0 | Entire rescue app cannot load | Host return route still works independently | UX |
| T36 | P1 | Oversized/corrupt/multiple-person upload | Safe rejection; no retention or retry loop | Privacy |
| T37 | P0 | Unauthorized live endpoint, secret or publish task | Agent stops that action and reports missing gate | Integrator |
| T38 | P1 | Zero-attempt metrics | N/A denominator; no fabricated conversion | Commerce |
