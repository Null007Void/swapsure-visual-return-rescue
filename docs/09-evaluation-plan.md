# Evaluation plan
Separate package verification, future offline implementation tests, approved live integration tests and eventual user studies. The conceptual “PASS” statements in the source are design expectations.

## Offline test suite
Build table-driven tests for every eligibility fixture, property tests for unknown reasons and deterministic ordering, contract tests for adapters, and browser tests for the two demo journeys plus failure/refund paths. Use a controllable clock and fake provider/commerce adapters. Block outbound network at the test harness; assert zero requests in MOCK.

For every adversarial row record test ID, commit, fixture, expected/observed result, mode, pass/fail, log reference and reviewer. All critical safety/authority cases must pass. Unexecuted cases remain NOT_RUN. Mutation checks should prove that removing refund controls, admitting fit reasons or skipping confirmation makes tests fail.

## Measures
| Metric | Definition and interpretation |
|---|---|
| Eligibility accuracy | Exact expected status / all labeled cases; target 100% deterministic fixtures |
| Unsafe preview dispatches | Ineligible, no-consent or unauthorized requests; target zero |
| Candidate validity | Candidates meeting every hard constraint / emitted candidates; target 100% |
| Rescue conversion | Committed exchanges / eligible sessions that opt into rescue; count failures and abandonment in denominator |
| Opt-in rate | Rescue opt-ins / eligible sessions offered rescue |
| Retained original GMV | Sum min(original eligible value, replacement value) for committed exchanges |
| Partial refunds / extra charges | Report separately; no net-profit claim |
| Decision time | From opt-in to terminal decision; report censored abandonment separately |
| Provider reliability | Successes / dispatched tasks, error distribution and p50/p95 latency |
| Units | Planned, reserved and actual units including failures/retries |
| Refund reachability | Every precommit rescue state permits direct host handoff |
| Deletion | No local media/handles survive TTL or completed deletion; remote status separate |

Use the [catalog fixture](../fixtures/catalog.json) for $89/$84/$99 calculations. Do not reproduce the earlier dashboard's example percentages as measured results.

## Live evaluation after authorization
Run a small owner-approved image set across supported garment categories and varied presentations; obtain rights before testing. Assess garment/color fidelity, unwanted identity/background changes and visual artifacts with human reviewers. Record limitations, sample size and failures, not beauty or fit scores. No live tests are scheduled or authorized by this plan.

A user study would require a separate protocol. Until then claim demonstrated workflow and simulated accounting only; no causal return reduction, revenue uplift or market-winning predictions.
