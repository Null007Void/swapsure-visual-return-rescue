# YouCam API boundary
Only YouCam VTO supplies visual evidence. It cannot decide eligibility, rank human appearance or authorize commerce. Offline phases use a mock adapter with no network transport.

## Verified documentation and unresolved details
The [official integration guide](https://docs.perfectcorp.com/reference/ai_clothes/section/overview/integration-guide), checked October 25, describes file registration, upload to a returned URL, garment reference input, task creation and status polling. It shows Bearer authentication, /s2s/v2.0/file and /s2s/v2.0/task/cloth-v4. Results include a task status and image URL.

The same guide's cost table lists V2/V3 at two units but does not establish V4 cost. Its initial full-body photo guidance also differs from its later upper-body requirements. **STOP before live work: pin the endpoint/version, resolve image requirements and verify that version's units.** Do not assume three V4 renders cost six units.

## Adapter contract
Expose prepareMedia, createPreview, pollPreview and discardLocalMedia behind an injected provider interface. Keep provider DTOs separate from domain contracts. Normalize failures into INVALID_INPUT, AUTH_FAILURE, RATE_LIMIT, BUDGET_EXHAUSTED, TIMEOUT, PROVIDER_FAILURE and INVALID_RESULT. None changes a commerce decision.

Local design defaults: maximum three tasks/session, two concurrent tasks, total preview deadline 60 seconds; bounded polling with jitter. These are proposed app limits, not provider guarantees. Verify provider rate limits before live authorization. Retry polling only inside deadline. Never blindly retry task creation after uncertain acknowledgement; reconcile or stop to avoid duplicate charges.

Validate result ownership, candidate/task association, status and media type/size. Allowlist approved upload/download destinations; reject arbitrary catalog URLs, redirects to private networks and cross-session handles. Sanitize provider messages before display.

## Live gate evidence
Require owner authorization reference, secret-handling plan, exact endpoint and cost snapshot, approved image rights and consent, retention/deletion terms, allowed hosts, call/unit/currency caps and expiry. Free credits also require approval. No account creation, secret retrieval or playground calls in offline phases.

Provider retention and deletion guarantees remain unverified. Do not infer retention from example URL paths, promise instant remote erasure, or fabricate a deletion endpoint. Before D, document supported deletion/cancellation behavior and residual retention in consent language. If that cannot meet approved privacy requirements, remain MOCK.
