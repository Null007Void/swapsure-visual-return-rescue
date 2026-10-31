"""Reusable scenario builders: a real session walked to every state.

Tests must not hand-construct half-valid sessions, or they would prove nothing
about the reducer's guards.  Everything here is produced by applying real events
through ``domain.state_machine.transition``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from adapters.mock.fixtures import CatalogFixture
from commerce.quote import Quote, build_quote
from commerce.confirmation import ACTION_APPROVE_EXCHANGE, TRUSTED_ORIGIN, ConfirmRequest
from domain.catalog import CandidateSelection, select_candidates
from domain.eligibility import EligibilityDecision, decide
from domain.preview import PreviewResult
from domain.state_machine import (
    ApproveExchange,
    ChooseCandidate,
    CommitSucceeded,
    CommitUncertain,
    DeclineRescue,
    EvaluateEligibility,
    OptInRescue,
    PrepareCandidates,
    ReceivePreviewResult,
    RecordConsent,
    RecordPhoto,
    SessionState,
    transition,
)

SESSION_ID = "ses-scenario"
ORDER_ID = "ORD-FIXTURE-1"
SHOPPER = "shopper-1"
TOKEN = "tok-1"
CONSENT = "consent-v1"
IDEMPOTENCY_KEY = "idem-1"


@dataclass(frozen=True, slots=True)
class Scenario:
    session: SessionState
    selection: CandidateSelection
    quote: Quote
    decision: EligibilityDecision
    candidate_sku: str
    now: Any


def selection_for(
    fixture: CatalogFixture, *, session_id: str = SESSION_ID, reason_codes: tuple[str, ...] = ("COLOR",)
) -> CandidateSelection:
    return select_candidates(
        fixture.query(session_id=session_id, reason_codes=reason_codes), now=fixture.clock
    )


def _preview_result(selection: CandidateSelection, now) -> PreviewResult:
    return PreviewResult(
        session_id=selection.session_id,
        mode="MOCK",
        status="SUCCEEDED",
        candidate_ids=selection.candidate_skus,
        task_ids=tuple(f"task-{index}" for index in range(len(selection.candidate_skus))),
        created_at=now,
        expires_at=now + timedelta(seconds=60),
        media_handles=tuple(f"media-{index}" for index in range(len(selection.candidate_skus))),
    )


PHOTO_DIGEST = "sha256:fixture-photo"


def build_states(fixture: CatalogFixture) -> dict[str, Scenario]:
    """Walk one session to each of the eleven contract states."""

    now = fixture.clock
    order = fixture.order
    decision = decide(SESSION_ID, "1.0", ["COLOR"], False)
    selection = selection_for(fixture)
    sku = selection.candidate_skus[0]
    quote = build_quote(
        order_id=order.order_id,
        candidate_variant_id=sku,
        original_value_minor=order.original_value_minor,
        replacement_value_minor=next(
            candidate.price_minor for candidate in selection.candidates if candidate.sku == sku
        ),
        currency=order.currency,
        inventory_version=selection.inventory_version,
        quote_version=1,
        now=now,
    )

    def step(session: SessionState, event) -> SessionState:
        result = transition(session, event, now=now)
        assert result.accepted, f"{type(event).__name__} refused: {result.refusal}"
        return result.session

    base = SessionState(
        session_id=SESSION_ID,
        order_id=ORDER_ID,
        shopper_identity=SHOPPER,
        request_token=TOKEN,
    )
    eligibility = step(base, EvaluateEligibility(decision))
    consent = step(eligibility, OptInRescue())
    candidates = step(consent, RecordConsent(CONSENT))
    candidates = step(candidates, RecordPhoto(PHOTO_DIGEST))
    preview = step(
        candidates, PrepareCandidates(selection=selection, estimated_units=0, budget_units=0)
    )
    decision_ready = step(preview, ReceivePreviewResult(result=_preview_result(selection, now)))
    quote_ready = step(decision_ready, ChooseCandidate(candidate_sku=sku, quote=quote))
    commit_pending = step(
        quote_ready,
        ApproveExchange(
            quote_id=quote.quote_id,
            quote_version=quote.quote_version,
            origin=TRUSTED_ORIGIN,
            approved_at=now,
            idempotency_key=IDEMPOTENCY_KEY,
            shopper_identity=SHOPPER,
            request_token=TOKEN,
        ),
    )
    reconciling = step(commit_pending, CommitUncertain(attempt_id="txn-uncertain"))
    committed = step(commit_pending, CommitSucceeded(commit_id="txn-committed"))
    refund = step(eligibility, DeclineRescue(reason="shopper chose refund"))

    scenarios = {
        "RETURN_STARTED": base,
        "ELIGIBILITY_CHECKED": eligibility,
        "CONSENT_PENDING": consent,
        "CANDIDATES_READY": candidates,
        "PREVIEW_PENDING": preview,
        "DECISION_READY": decision_ready,
        "QUOTE_READY": quote_ready,
        "COMMIT_PENDING": commit_pending,
        "RECONCILING": reconciling,
        "EXCHANGE_COMMITTED": committed,
        "REFUND_HANDOFF": refund,
    }
    return {
        state: Scenario(
            session=session,
            selection=selection,
            quote=quote,
            decision=decision,
            candidate_sku=sku,
            now=now,
        )
        for state, session in scenarios.items()
    }


def confirm_request(
    session: SessionState,
    quote: Quote,
    *,
    idempotency_key: str = IDEMPOTENCY_KEY,
    action: str = ACTION_APPROVE_EXCHANGE,
    origin: str = TRUSTED_ORIGIN,
    approved_at=None,
    shopper_identity: str | None = None,
    request_token: str | None = None,
    candidate_sku: str | None = None,
) -> ConfirmRequest:
    return ConfirmRequest(
        session_id=session.session_id,
        shopper_identity=SHOPPER if shopper_identity is None else shopper_identity,
        request_token=TOKEN if request_token is None else request_token,
        quote_id=quote.quote_id,
        quote_version=quote.quote_version,
        candidate_sku=quote.candidate_variant_id if candidate_sku is None else candidate_sku,
        action=action,
        origin=origin,
        approved_at=approved_at if approved_at is not None else quote.issued_at,
        idempotency_key=idempotency_key,
    )
