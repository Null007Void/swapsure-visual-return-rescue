# Phased bounded engineering task tree
Each phase ends in a STOP/GATE. Passing tests satisfies evidence requirements; it does not expand authority. The receiving agent may work locally in B and C within the current bounded direction.

| Phase | Tasks and artifacts | Exit gate |
|---|---|---|
| A Package | A1 recover source; A2 freeze concept; A3 specify contracts; A4 verify package | G-A: package links/archive/hashes pass; completed by this delivery |
| B Deterministic foundation | B1 reason rules; B2 catalog filters/ranking; B3 state reducer; B4 quote/commerce simulator; B5 receipt/ledger; B6 unit/property/race tests | G-B: domain and commerce P0 tests pass; documented review; STOP until evidence complete |
| C Offline demo | C1 UI/refund host link; C2 consent/deletion; C3 async mock adapter; C4 budget planner; C5 browser failure/accessibility tests; C6 demo reset and scripts | G-C: all offline criteria pass; no network/API usage; honest mode labels; STOP before D |
| D Approved live integration | D1 pin API/cost/terms; D2 owner-provided secret path; D3 restricted transport; D4 approved smoke tests; D5 results/cost/retention report | G-D: explicit owner approval BEFORE D2–D4, with endpoint, images, calls/units/currency cap and expiry; offline D1 research may proceed without calls |
| E Approved deployment | E1 prepare reproducible release locally; E2 owner approves host/access/cost/lifetime; E3 deploy scoped artifact; E4 verify access and cleanup | G-E: separate owner approval BEFORE hosting, provisioning, publishing or DNS changes |
| F Approved submission | F1 prepare description/video/checklist locally; F2 verify claims/licenses/build; F3 owner reviews exact submission; F4 authorized publish/submit | G-F: separate explicit approval of final materials and destination BEFORE publication/submission |

## Cross-cutting STOP rules
Stop the affected action if secrets are needed, an API request may consume units, external writes/publication are requested, infrastructure persists, image rights/consent are missing, or a gate is ambiguous. Stop release for any lost refund path, unapproved financial action, image leak, fit/beauty claim, misleading mock label or unresolved P0.

Do not generate a key to “unblock” D. Do not redeem free credits or call a playground without approval. Live API approval does not authorize deployment, real merchant writes or submission. Deployment approval does not authorize persistent resources beyond its approved lifetime. Submission drafts remain local unless explicitly authorized otherwise.

## Authority evidence
Use [owner checklist](14-owner-checklist.md) and [authorization template](../contracts/authorization-template.json). Approval records identify owner, exact action/scope, artifact hash or commit, endpoint/destination, permitted data, caps, expiry, evidence link and revocation status. Empty fields mean denied. Agents cannot fill approver identity or fabricate an evidence link.

Before each gated action, compare actual scope to approval. If scope changes or approval expires, stop that dependent action. Work already authorized locally can continue. Document outcome and resource cleanup after each approved phase.

## Suggested implementation layout
Use src/domain for pure rules; src/adapters/mock for fixtures; src/adapters/youcam for later gated provider integration; src/commerce for simulator; src/ui for shopper screens; tests/domain, tests/contracts and tests/e2e for evidence. This layout is a proposal, not files claimed to exist in this package.
