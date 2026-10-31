"""State reducer conformance — `contracts/state-machine.json` v1.0.

The strongest assertion in this file is the isomorphism check: applying every
event to every state produces *exactly* the contract's edge set and nothing else.
That makes contract drift a test failure rather than a code review question.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from datetime import timedelta

import pytest

from domain.eligibility import decide
from domain.state_machine import (
    ALLOWED_EDGES,
    EVENT_TYPES,
    INITIAL_STATE,
    PRECOMMIT_STATES,
    STATES,
    TERMINAL_STATES,
    ApproveExchange,
    CommitFailedKnown,
    CommitSucceeded,
    CommitUncertain,
    ContinueRefund,
    DeclineRescue,
    EvaluateEligibility,
    OptInRescue,
    PrepareCandidates,
    ReceivePreviewResult,
    RecordConsent,
    RecordPhoto,
    STALE_PHOTO_BINDING,
    ChangePhoto,
    TryAnotherGarment,
    ReconcileCommitted,
    ReconcileNotCommitted,
    ReportNoCandidates,
    WithdrawConsent,
    ChooseCandidate,
    guard_summary,
    initial_session,
    is_precommit,
    is_terminal,
    transition,
)
from harness.scenarios import CONSENT, IDEMPOTENCY_KEY, SHOPPER, TOKEN, build_states
from specs import state_machine as load_state_machine


@pytest.fixture
def scenarios(catalog):
    return build_states(catalog)


def canonical_events(scenarios) -> dict[str, object]:
    """One valid instance of every event type, built from real artefacts."""

    quote_ready = scenarios["QUOTE_READY"]
    return {
        "EvaluateEligibility": EvaluateEligibility(scenarios["ELIGIBILITY_CHECKED"].decision),
        "OptInRescue": OptInRescue(),
        "DeclineRescue": DeclineRescue(reason="shopper chose refund"),
        "RecordConsent": RecordConsent(consent_version=CONSENT),
        "WithdrawConsent": WithdrawConsent(reason="shopper withdrew consent"),
        "RecordPhoto": RecordPhoto(photo_version="sha256:canonical-photo"),
        "TryAnotherGarment": TryAnotherGarment(),
        "ChangePhoto": ChangePhoto(reason="canonical change"),
        "PrepareCandidates": PrepareCandidates(
            selection=quote_ready.selection, estimated_units=0, budget_units=0
        ),
        "ReportNoCandidates": ReportNoCandidates(
            selection=dataclasses.replace(quote_ready.selection, candidates=())
        ),
        "ReceivePreviewResult": ReceivePreviewResult(
            result=scenarios["PREVIEW_PENDING"].session.preview_plan
            and scenarios["DECISION_READY"].session.preview_result
        ),
        "ChooseCandidate": ChooseCandidate(
            candidate_sku=quote_ready.candidate_sku, quote=quote_ready.quote
        ),
        "ApproveExchange": ApproveExchange(
            quote_id=quote_ready.quote.quote_id,
            quote_version=quote_ready.quote.quote_version,
            origin="shopper_ui",
            approved_at=quote_ready.now,
            idempotency_key=IDEMPOTENCY_KEY,
            shopper_identity=SHOPPER,
            request_token=TOKEN,
        ),
        "CommitSucceeded": CommitSucceeded(commit_id="txn-1"),
        "CommitFailedKnown": CommitFailedKnown(reason="known failure"),
        "CommitUncertain": CommitUncertain(attempt_id="txn-unc"),
        "ReconcileCommitted": ReconcileCommitted(commit_id="txn-1"),
        "ReconcileNotCommitted": ReconcileNotCommitted(reason="not committed"),
        "ContinueRefund": ContinueRefund(trigger="continue refund"),
    }


# --------------------------------------------------------------------------- #
# Contract shape
# --------------------------------------------------------------------------- #
def test_contract_state_lists_match_the_json():
    contract = load_state_machine()
    assert STATES == tuple(contract["states"]) and len(STATES) == 11
    assert TERMINAL_STATES == tuple(contract["terminal"]) == (
        "EXCHANGE_COMMITTED",
        "REFUND_HANDOFF",
    )
    assert INITIAL_STATE == "RETURN_STARTED"
    assert PRECOMMIT_STATES == tuple(contract["precommitStates"])
    assert len(PRECOMMIT_STATES) == 7


def test_commit_pending_has_exactly_three_exits():
    exits = {to for source, to in ALLOWED_EDGES if source == "COMMIT_PENDING"}
    assert exits == {"EXCHANGE_COMMITTED", "DECISION_READY", "RECONCILING"}


def test_every_precommit_state_has_a_direct_refund_edge():
    for state in PRECOMMIT_STATES:
        assert (state, "REFUND_HANDOFF") in ALLOWED_EDGES


def test_reconciling_has_two_exits():
    exits = {to for source, to in ALLOWED_EDGES if source == "RECONCILING"}
    assert exits == {"EXCHANGE_COMMITTED", "REFUND_HANDOFF"}


def test_all_states_are_reachable_from_the_initial_state():
    reachable = {INITIAL_STATE}
    queue = deque([INITIAL_STATE])
    while queue:
        state = queue.popleft()
        for source, target in ALLOWED_EDGES:
            if source == state and target not in reachable:
                reachable.add(target)
                queue.append(target)
    assert reachable == set(STATES)


# --------------------------------------------------------------------------- #
# Isomorphism: reducer edges == contract edges, exactly
# --------------------------------------------------------------------------- #
def test_reducer_produces_exactly_the_contract_edges(scenarios):
    events = canonical_events(scenarios)
    produced: dict[tuple[str, str], list[str]] = {}
    for state, scenario in scenarios.items():
        for name, event in events.items():
            result = transition(scenario.session, event, now=scenario.now)
            if not result.accepted:
                continue
            assert result.edge is not None
            assert result.edge in ALLOWED_EDGES, f"edge outside contract: {result.edge}"
            produced.setdefault(result.edge, []).append(name)
            assert result.session.state == result.edge[1]
            assert result.session.version == scenario.session.version + 1

    missing = ALLOWED_EDGES - set(produced)
    assert not missing, f"contract edges no event can produce: {sorted(missing)}"
    extra = set(produced) - ALLOWED_EDGES
    assert not extra, f"reducer produced edges absent from the contract: {sorted(extra)}"
    assert set(produced) == ALLOWED_EDGES


def test_reducer_never_leaves_a_terminal_state(scenarios):
    events = canonical_events(scenarios)
    for state in TERMINAL_STATES:
        session = scenarios[state].session
        for name, event in events.items():
            result = transition(session, event, now=scenarios[state].now)
            assert not result.accepted, f"{state} accepted {name}"
            assert result.session is session
            assert result.session.refund_route_available is True


# --------------------------------------------------------------------------- #
# Guard failures are inert
# --------------------------------------------------------------------------- #
def test_guard_failure_leaves_state_version_and_object_untouched(scenarios):
    session = scenarios["RETURN_STARTED"].session
    result = transition(session, CommitSucceeded(commit_id="txn-x"), now=scenarios["RETURN_STARTED"].now)
    assert result.accepted is False
    assert result.refusal == "WRONG_STATE"
    assert result.session is session
    assert result.session.version == session.version
    assert result.session.state == "RETURN_STARTED"


def test_optimistic_version_conflict_is_refused(scenarios):
    session = scenarios["QUOTE_READY"].session
    result = transition(
        session,
        ContinueRefund(trigger="refund"),
        now=scenarios["QUOTE_READY"].now,
        expected_version=session.version + 3,
    )
    assert result.accepted is False
    assert result.refusal == "VERSION_CONFLICT"
    assert result.session is session


def test_stale_version_conflict_does_not_advance(scenarios):
    stale = scenarios["QUOTE_READY"].session
    accepted = transition(stale, ContinueRefund(trigger="refund"), now=scenarios["QUOTE_READY"].now)
    assert accepted.accepted
    replay = transition(
        stale,
        ContinueRefund(trigger="refund"),
        now=scenarios["QUOTE_READY"].now,
        expected_version=stale.version,
    )
    # The object is immutable: applying twice from the same object yields the same
    # successor, never a double increment.
    assert replay.session.version == accepted.session.version


# --------------------------------------------------------------------------- #
# Refund from every precommit state
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state", PRECOMMIT_STATES)
def test_continue_refund_from_every_precommit_state(scenarios, state):
    scenario = scenarios[state]
    result = transition(scenario.session, ContinueRefund(trigger="refund"), now=scenario.now)
    assert result.accepted
    assert result.edge == (state, "REFUND_HANDOFF")
    session = result.session
    assert session.state == "REFUND_HANDOFF"
    assert session.invalidated_preview_work is True
    assert session.invalidated_quote_work is True
    assert session.quote is None
    assert session.approval is None
    assert session.preview_result is None
    assert session.refund_route_available is True


def test_refund_during_commit_is_recorded_not_transitioned(scenarios):
    scenario = scenarios["COMMIT_PENDING"]
    result = transition(scenario.session, ContinueRefund(trigger="refund"), now=scenario.now)
    assert result.accepted is False
    assert result.refusal == "REFUND_RECORDED_PENDING_COMMIT"
    assert result.session.state == "COMMIT_PENDING"
    assert result.session.version == scenario.session.version
    assert result.session.refund_recorded is True
    assert result.session.refund_recorded_at == scenario.now


def test_second_refund_request_after_handoff_is_refused(scenarios):
    scenario = scenarios["REFUND_HANDOFF"]
    result = transition(scenario.session, ContinueRefund(trigger="refund"), now=scenario.now)
    assert result.accepted is False
    assert result.refusal == "TERMINAL_STATE"


# --------------------------------------------------------------------------- #
# Commit sequence guards
# --------------------------------------------------------------------------- #
def test_commit_cannot_start_without_quote_approval(scenarios):
    scenario = scenarios["QUOTE_READY"]
    result = transition(
        scenario.session, CommitSucceeded(commit_id="txn-x"), now=scenario.now
    )
    assert result.accepted is False
    assert result.refusal == "WRONG_STATE"


def test_no_blind_second_transaction_from_commit_pending(scenarios):
    scenario = scenarios["COMMIT_PENDING"]
    result = transition(
        scenario.session,
        ApproveExchange(
            quote_id=scenario.quote.quote_id,
            quote_version=scenario.quote.quote_version,
            origin="shopper_ui",
            approved_at=scenario.now,
            idempotency_key="idem-2",
            shopper_identity=SHOPPER,
            request_token=TOKEN,
        ),
        now=scenario.now,
    )
    assert result.accepted is False
    assert result.refusal == "WRONG_STATE"


def test_reconciling_resolves_only_by_transaction_evidence(scenarios):
    scenario = scenarios["RECONCILING"]
    committed = transition(scenario.session, ReconcileCommitted(commit_id="txn-1"), now=scenario.now)
    assert committed.accepted and committed.session.state == "EXCHANGE_COMMITTED"
    refunded = transition(
        scenario.session, ReconcileNotCommitted(reason="not committed"), now=scenario.now
    )
    assert refunded.accepted and refunded.session.state == "REFUND_HANDOFF"


def test_reconciling_refuses_approval_and_commit_events(scenarios):
    scenario = scenarios["RECONCILING"]
    for event in (CommitSucceeded(commit_id="x"), CommitUncertain(attempt_id="y")):
        result = transition(scenario.session, event, now=scenario.now)
        assert result.accepted is False


def test_known_failure_returns_to_decision_ready_and_clears_the_quote(scenarios):
    scenario = scenarios["COMMIT_PENDING"]
    result = transition(
        scenario.session, CommitFailedKnown(reason="stock changed"), now=scenario.now
    )
    assert result.accepted
    assert result.session.state == "DECISION_READY"
    assert result.session.quote is None
    assert result.session.approval is None
    assert result.session.invalidated_quote_work is True
    # Candidates survive so the shopper can choose again.
    assert result.session.candidate_skus() == scenario.selection.candidate_skus


# --------------------------------------------------------------------------- #
# Approval is the shopper's alone
# --------------------------------------------------------------------------- #
def test_approval_from_another_origin_is_refused(scenarios):
    scenario = scenarios["QUOTE_READY"]
    event = ApproveExchange(
        quote_id=scenario.quote.quote_id,
        quote_version=scenario.quote.quote_version,
        origin="agent_auto",
        approved_at=scenario.now,
        idempotency_key=IDEMPOTENCY_KEY,
        shopper_identity=SHOPPER,
        request_token=TOKEN,
    )
    result = transition(scenario.session, event, now=scenario.now)
    assert result.accepted is False
    assert result.refusal == "NOT_SHOPPER_ORIGIN"


def test_approval_for_another_shopper_is_refused(scenarios):
    scenario = scenarios["QUOTE_READY"]
    event = ApproveExchange(
        quote_id=scenario.quote.quote_id,
        quote_version=scenario.quote.quote_version,
        origin="shopper_ui",
        approved_at=scenario.now,
        idempotency_key=IDEMPOTENCY_KEY,
        shopper_identity="someone-else",
        request_token=TOKEN,
    )
    assert transition(scenario.session, event, now=scenario.now).refusal == "SHOPPER_IDENTITY_MISMATCH"


def test_approval_without_the_session_token_is_refused(scenarios):
    scenario = scenarios["QUOTE_READY"]
    event = ApproveExchange(
        quote_id=scenario.quote.quote_id,
        quote_version=scenario.quote.quote_version,
        origin="shopper_ui",
        approved_at=scenario.now,
        idempotency_key=IDEMPOTENCY_KEY,
        shopper_identity=SHOPPER,
        request_token="wrong",
    )
    assert transition(scenario.session, event, now=scenario.now).refusal == "REQUEST_TOKEN_MISMATCH"


def test_approval_of_a_stale_quote_version_is_refused(scenarios):
    scenario = scenarios["QUOTE_READY"]
    event = ApproveExchange(
        quote_id=scenario.quote.quote_id,
        quote_version=scenario.quote.quote_version + 1,
        origin="shopper_ui",
        approved_at=scenario.now,
        idempotency_key=IDEMPOTENCY_KEY,
        shopper_identity=SHOPPER,
        request_token=TOKEN,
    )
    assert transition(scenario.session, event, now=scenario.now).refusal == "QUOTE_MISMATCH"


def test_approval_after_expiry_is_refused(scenarios):
    scenario = scenarios["QUOTE_READY"]
    later = scenario.quote.expires_at + timedelta(seconds=1)
    event = ApproveExchange(
        quote_id=scenario.quote.quote_id,
        quote_version=scenario.quote.quote_version,
        origin="shopper_ui",
        approved_at=later,
        idempotency_key=IDEMPOTENCY_KEY,
        shopper_identity=SHOPPER,
        request_token=TOKEN,
    )
    result = transition(scenario.session, event, now=later)
    assert result.accepted is False
    assert result.refusal == "QUOTE_EXPIRED"


def test_approval_timestamp_in_the_future_is_refused(scenarios):
    scenario = scenarios["QUOTE_READY"]
    event = ApproveExchange(
        quote_id=scenario.quote.quote_id,
        quote_version=scenario.quote.quote_version,
        origin="shopper_ui",
        approved_at=scenario.now + timedelta(seconds=30),
        idempotency_key=IDEMPOTENCY_KEY,
        shopper_identity=SHOPPER,
        request_token=TOKEN,
    )
    assert transition(scenario.session, event, now=scenario.now).refusal == "MALFORMED_EVENT"


# --------------------------------------------------------------------------- #
# Eligibility, consent, candidates and preview guards
# --------------------------------------------------------------------------- #
def test_ineligible_decision_cannot_opt_in(scenarios):
    session = scenarios["RETURN_STARTED"].session
    ineligible = decide(session.session_id, "1.0", ["TOO_SMALL"], False)
    stepped = transition(session, EvaluateEligibility(ineligible), now=scenarios["RETURN_STARTED"].now)
    assert stepped.accepted
    refused = transition(stepped.session, OptInRescue(), now=scenarios["RETURN_STARTED"].now)
    assert refused.accepted is False
    assert refused.refusal == "NOT_ELIGIBLE"


def test_decision_with_another_policy_version_is_refused(scenarios):
    session = scenarios["RETURN_STARTED"].session
    decision = decide(session.session_id, "1.0", ["COLOR"], False)
    stale = dataclasses.replace(decision, policy_version="0.9")
    result = transition(session, EvaluateEligibility(stale), now=scenarios["RETURN_STARTED"].now)
    assert result.accepted is False
    assert result.refusal == "MALFORMED_EVENT"


def test_empty_selection_cannot_enter_preview_pending(scenarios):
    scenario = scenarios["CANDIDATES_READY"]
    empty = dataclasses.replace(scenario.selection, candidates=())
    result = transition(
        scenario.session,
        PrepareCandidates(selection=empty, estimated_units=0, budget_units=0),
        now=scenario.now,
    )
    assert result.accepted is False
    assert result.refusal == "NO_CANDIDATES"


def test_stale_selection_cannot_enter_preview_pending(scenarios):
    scenario = scenarios["CANDIDATES_READY"]
    stale = dataclasses.replace(scenario.selection, inventory_fresh=False)
    result = transition(
        scenario.session,
        PrepareCandidates(selection=stale, estimated_units=0, budget_units=0),
        now=scenario.now,
    )
    assert result.accepted is False
    assert result.refusal == "STALE_INVENTORY"


def test_cross_session_selection_is_refused(scenarios):
    scenario = scenarios["CANDIDATES_READY"]
    other = dataclasses.replace(scenario.selection, session_id="someone-else")
    result = transition(
        scenario.session,
        PrepareCandidates(selection=other, estimated_units=0, budget_units=0),
        now=scenario.now,
    )
    assert result.refusal == "SELECTION_SESSION_MISMATCH"


def test_budget_must_cover_estimated_units(scenarios):
    scenario = scenarios["CANDIDATES_READY"]
    result = transition(
        scenario.session,
        PrepareCandidates(selection=scenario.selection, estimated_units=2, budget_units=1),
        now=scenario.now,
    )
    assert result.refusal == "BUDGET_EXCEEDED"


def test_no_candidates_event_requires_an_empty_selection(scenarios):
    scenario = scenarios["CANDIDATES_READY"]
    result = transition(
        scenario.session, ReportNoCandidates(selection=scenario.selection), now=scenario.now
    )
    assert result.refusal == "CANDIDATES_PRESENT"


def test_no_candidates_event_goes_to_refund(scenarios):
    scenario = scenarios["CANDIDATES_READY"]
    empty = dataclasses.replace(scenario.selection, candidates=())
    result = transition(scenario.session, ReportNoCandidates(selection=empty), now=scenario.now)
    assert result.accepted and result.session.state == "REFUND_HANDOFF"


def test_preview_result_from_another_session_is_refused(scenarios):
    scenario = scenarios["PREVIEW_PENDING"]
    result = dataclasses.replace(scenarios["DECISION_READY"].session.preview_result, session_id="other")
    refused = transition(scenario.session, ReceivePreviewResult(result=result), now=scenario.now)
    assert refused.refusal == "PREVIEW_CROSS_SESSION"


def test_preview_result_for_an_unplanned_candidate_is_refused(scenarios):
    scenario = scenarios["PREVIEW_PENDING"]
    base = scenarios["DECISION_READY"].session.preview_result
    result = dataclasses.replace(
        base,
        candidate_ids=("NOT-A-CANDIDATE",),
        task_ids=("t",),
        media_handles=("m",),
    )
    refused = transition(scenario.session, ReceivePreviewResult(result=result), now=scenario.now)
    assert refused.refusal == "CANDIDATE_NOT_AVAILABLE"


def test_late_preview_result_is_discarded(scenarios):
    scenario = scenarios["PREVIEW_PENDING"]
    result = scenarios["DECISION_READY"].session.preview_result
    late = result.expires_at + timedelta(seconds=1)
    refused = transition(scenario.session, ReceivePreviewResult(result=result), now=late)
    assert refused.accepted is False
    assert refused.refusal == "GUARD_FAILED"
    assert "late" in (refused.note or "")


def test_failed_preview_still_advances_to_decision_ready(scenarios):
    scenario = scenarios["PREVIEW_PENDING"]
    failed = dataclasses.replace(
        scenarios["DECISION_READY"].session.preview_result,
        status="FAILED",
        media_handles=(),
        failure_code="PROVIDER_FAILURE",
    )
    result = transition(scenario.session, ReceivePreviewResult(result=failed), now=scenario.now)
    assert result.accepted
    assert result.session.state == "DECISION_READY"
    assert result.session.refund_route_available is True


def test_choose_rejects_a_quote_from_another_inventory_version(scenarios):
    scenario = scenarios["DECISION_READY"]
    other = dataclasses.replace(scenario.quote, inventory_version="inv-99")
    result = transition(
        scenario.session,
        ChooseCandidate(candidate_sku=scenario.candidate_sku, quote=other),
        now=scenario.now,
    )
    assert result.refusal == "STALE_INVENTORY"


def test_choose_rejects_an_expired_quote(scenarios):
    scenario = scenarios["DECISION_READY"]
    later = scenario.quote.expires_at + timedelta(seconds=1)
    result = transition(
        scenario.session,
        ChooseCandidate(candidate_sku=scenario.candidate_sku, quote=scenario.quote),
        now=later,
    )
    assert result.refusal == "QUOTE_EXPIRED"


def test_choose_rejects_a_candidate_that_is_not_offered(scenarios):
    scenario = scenarios["DECISION_READY"]
    result = transition(
        scenario.session,
        ChooseCandidate(candidate_sku="NOT-OFFERED", quote=scenario.quote),
        now=scenario.now,
    )
    assert result.refusal == "CANDIDATE_NOT_AVAILABLE"


# --------------------------------------------------------------------------- #
# Invariants that must hold in every reachable situation
# --------------------------------------------------------------------------- #
def test_refund_route_is_available_in_every_state(scenarios):
    for state, scenario in scenarios.items():
        assert scenario.session.refund_route_available is True
        assert scenario.session.state == state


def test_precommit_predicate_matches_the_contract(scenarios):
    for state, scenario in scenarios.items():
        assert is_precommit(scenario.session.state) is (state in PRECOMMIT_STATES)
        assert is_terminal(scenario.session.state) is (state in TERMINAL_STATES)


def test_terminal_commit_is_not_a_waiver_of_return_rights(scenarios):
    session = scenarios["EXCHANGE_COMMITTED"].session
    assert session.state == "EXCHANGE_COMMITTED"
    assert session.refund_route_available is True
    # Media deletion and host support are orthogonal to commerce state.
    assert session.committed is True


def test_initial_session_starts_in_the_contract_initial_state():
    session = initial_session("ses-x", "ORD-1", "shopper-x", "token-x")
    assert session.state == INITIAL_STATE
    assert session.version == 0
    assert session.history == ()


def test_result_bound_to_a_different_photo_is_refused(scenarios):
    """A preview produced for the previous photo never lands on the new one."""
    pending = scenarios["PREVIEW_PENDING"]
    known = scenarios["DECISION_READY"].session.preview_result
    assert pending.session.photo_version

    stale = dataclasses.replace(known, photo_version="sha256:previous-photo")
    refused = transition(pending.session, ReceivePreviewResult(result=stale), now=pending.now)
    assert not refused.accepted
    assert refused.refusal == STALE_PHOTO_BINDING
    assert refused.session.state == "PREVIEW_PENDING"

    fresh = dataclasses.replace(known, photo_version=pending.session.photo_version)
    accepted = transition(pending.session, ReceivePreviewResult(result=fresh), now=pending.now)
    assert accepted.accepted and accepted.session.state == "DECISION_READY"
    assert f"{pending.session.photo_version}:{fresh.candidate_ids[0]}" in accepted.session.generated


def test_history_records_every_accepted_transition(scenarios):
    session = scenarios["QUOTE_READY"].session
    # RETURN_STARTED -> ELIGIBILITY_CHECKED -> CONSENT_PENDING -> CANDIDATES_READY
    # -> CANDIDATES_READY (photo bound) -> PREVIEW_PENDING -> DECISION_READY -> QUOTE_READY:
    # seven accepted transitions, one per recorded step.
    assert len(session.history) == 7
    assert session.history[0].startswith("0:")
    assert session.history[-1].endswith("->QUOTE_READY")
    assert all("->" in entry for entry in session.history)


def test_guard_summary_covers_the_contract_states_and_events():
    summary = guard_summary()
    event_names = {event.__name__ for event in EVENT_TYPES}
    assert set(summary) == set(STATES)
    for state, mapping in summary.items():
        if state in TERMINAL_STATES:
            continue
        assert mapping, f"no documented guard for {state}"
        for event_name in mapping:
            assert event_name in event_names
