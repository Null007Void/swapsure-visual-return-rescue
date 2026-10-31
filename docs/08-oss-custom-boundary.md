# OSS and custom boundary
Keep infrastructure reuse narrow and the return-rescue protocol custom.

| Reuse candidate | Intended role | Approval boundary |
|---|---|---|
| Existing framework and upload library | UI/server foundations | Pin version; inspect license and safe image handling |
| Playwright | Browser journeys and accessibility interactions | Prior research identifies Apache-2.0; verify exact version before adding |
| OpenTelemetry | Metadata-only traces | Optional; disable sensitive payload capture |
| Medusa | Catalog/order patterns if needed | Core/enterprise boundaries require review; no full platform needed offline |
| Meilisearch | Large-catalog search if justified | Community/enterprise license review; local filters suffice initially |
| LangGraph | Only if explicit workflow complexity requires it | Deferred; no agent orchestration needed |
| PostHog | Avoid for MVP | Mixed licenses and media/session-replay risks |
| RecBole | Do not adopt | Prior research flags unresolved license wording |

These are carried-forward candidate assessments, not verified dependency selections. No third-party library is bundled in this package.

Custom work: reason semantics, eligibility precedence, hard constraints, deterministic ranking, budget planning, YouCam boundary, consent/deletion workflow, quote/approval protocol, receipt and outcome accounting. Infrastructure libraries must not change these decisions.

Before importing code, record exact source URL, version/commit, license identifier/text, attribution obligations, modifications and distribution scope. Avoid copying competitor code or demo imagery. Keep a dependency lockfile and third-party notice inventory when implementation begins. Selecting a public project license is an owner decision.
