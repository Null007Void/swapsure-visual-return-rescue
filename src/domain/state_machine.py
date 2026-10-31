"""Authoritative session state reducer — `contracts/state-machine.json` v1.0.

The reducer is the server's truth: UI state is only a projection.  Three
properties are structural rather than conventional:

* the accepted edge set is *exactly* the contract's edge set (asserted by
  ``tests/domain/test_state_machine.py`` by exhaustive enumeration);
* a guard failure returns the **same** session object — state and version are
  untouched, so a failed attempt cannot half-advance a shopper;
* every precommit state has an immediate refund exit that invalidates preview and
  quote work without waiting for cancellation or cleanup, and the refund route is
  reported available in every state including the terminal ones.

The module depends on the quote *value contract* only; it never talks to the
simulator.
"""

from __future__ import annotations

import os

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from commerce.quote import Quote
from domain.catalog import CandidateSelection
from domain.eligibility import (
    ALL_STATUSES,
    ELIGIBLE_VISUAL_RESCUE,
    EligibilityDecision,
)
from domain.preview import PreviewPlan, PreviewResult
from specs import policy as load_policy
from specs import state_machine as load_state_machine

STATES: tuple[str, ...] = tuple(load_state_machine()["states"])
TERMINAL_STATES: tuple[str, ...] = tuple(load_state_machine()["terminal"])
PRECOMMIT_STATES: tuple[str, ...] = tuple(load_state_machine()["precommitStates"])
INITIAL_STATE: str = str(load_state_machine()["initial"])
ALLOWED_EDGES: frozenset[tuple[str, str]] = frozenset(
    (edge["from"], edge["to"]) for edge in load_state_machine()["edges"]
)

TRUSTED_APPROVAL_ORIGIN = "shopper_ui"

# Refusal codes.  A refusal is a first-class result, not an exception: the
# shopper's session must survive a rejected attempt untouched.
WRONG_STATE = "WRONG_STATE"
STALE_PHOTO_BINDING = "STALE_PHOTO_BINDING"
ALREADY_GENERATED = "ALREADY_GENERATED"
TERMINAL_STATE = "TERMINAL_STATE"
VERSION_CONFLICT = "VERSION_CONFLICT"
UNSUPPORTED_EVENT = "UNSUPPORTED_EVENT"
GUARD_FAILED = "GUARD_FAILED"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
MISSING_DECISION = "MISSING_DECISION"
MISSING_CONSENT = "MISSING_CONSENT"
NO_CANDIDATES = "NO_CANDIDATES"
CANDIDATES_PRESENT = "CANDIDATES_PRESENT"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
SELECTION_SESSION_MISMATCH = "SELECTION_SESSION_MISMATCH"
STALE_INVENTORY = "STALE_INVENTORY"
CANDIDATE_NOT_AVAILABLE = "CANDIDATE_NOT_AVAILABLE"
QUOTE_MISMATCH = "QUOTE_MISMATCH"
QUOTE_EXPIRED = "QUOTE_EXPIRED"
NOT_SHOPPER_ORIGIN = "NOT_SHOPPER_ORIGIN"
SHOPPER_IDENTITY_MISMATCH = "SHOPPER_IDENTITY_MISMATCH"
REQUEST_TOKEN_MISMATCH = "REQUEST_TOKEN_MISMATCH"
MALFORMED_EVENT = "MALFORMED_EVENT"
PREVIEW_CROSS_SESSION = "PREVIEW_CROSS_SESSION"
PREVIEW_MODE_UNAVAILABLE = "PREVIEW_MODE_UNAVAILABLE"
REFUND_RECORDED_PENDING_COMMIT = "REFUND_RECORDED_PENDING_COMMIT"


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def is_precommit(state: str) -> bool:
    return state in PRECOMMIT_STATES


@dataclass(frozen=True, slots=True)
class Approval:
    """The shopper's explicit exchange approval for one exact quote version.

    Constructed from untrusted request data on purpose: the reducer, not the
    constructor, decides whether an approval is acceptable.  Selection, preview
    completion, silence and agent recommendation are different event types and can
    never produce this object through the API surface.
    """

    quote_id: str
    quote_version: int
    origin: str
    approved_at: datetime
    idempotency_key: str
    request_token: str

    def __post_init__(self) -> None:
        for name in ("quote_id", "origin", "idempotency_key", "request_token"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"approval.{name} must be a non-empty string")
        if not isinstance(self.quote_version, int) or isinstance(self.quote_version, bool):
            raise ValueError("approval.quote_version must be an integer")
        if self.approved_at.tzinfo is None:
            raise ValueError("approval.approved_at must be timezone-aware")

    def as_dict(self) -> dict[str, Any]:
        return {
            "quoteId": self.quote_id,
            "quoteVersion": self.quote_version,
            "origin": self.origin,
            "approvedAt": self.approved_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "idempotencyKey": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class SessionState:
    """Complete server-side session.  Immutable; transitions return a new value."""

    session_id: str
    order_id: str
    shopper_identity: str
    request_token: str
    state: str = INITIAL_STATE
    version: int = 0
    decision: EligibilityDecision | None = None
    consent_version: str | None = None
    selection: CandidateSelection | None = None
    preview_plan: PreviewPlan | None = None
    preview_result: PreviewResult | None = None
    # Content digest of the shopper photo the plan and result are bound to.
    photo_version: str | None = None
    # "<photo digest>:<sku>" for every pair that already produced a preview.
    generated: tuple[str, ...] = ()
    quote: Quote | None = None
    approval: Approval | None = None
    transaction_id: str | None = None
    commit_id: str | None = None
    receipt: Mapping[str, Any] | None = None
    refund_recorded: bool = False
    refund_recorded_at: datetime | None = None
    invalidated_preview_work: bool = False
    invalidated_quote_work: bool = False
    history: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown session state {self.state!r}")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 0:
            raise ValueError("version must be a non-negative integer")
        for name in ("session_id", "order_id", "shopper_identity", "request_token"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

    # -- invariants ------------------------------------------------------- #
    @property
    def refund_route_available(self) -> bool:
        """Frozen rule: the ordinary return path is always reachable."""

        return True

    @property
    def refund_from_this_state_allowed(self) -> bool:
        return is_precommit(self.state)

    @property
    def committed(self) -> bool:
        return self.state == "EXCHANGE_COMMITTED"

    def candidate_skus(self) -> tuple[str, ...]:
        return self.selection.candidate_skus if self.selection is not None else ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "orderId": self.order_id,
            "state": self.state,
            "version": self.version,
            "consentVersion": self.consent_version,
            "quoteId": self.quote.quote_id if self.quote else None,
            "quoteVersion": self.quote.quote_version if self.quote else None,
            "commitId": self.commit_id,
            "transactionId": self.transaction_id,
            "refundRecorded": self.refund_recorded,
            "invalidatedPreviewWork": self.invalidated_preview_work,
            "photoVersion": self.photo_version,
            "generated": list(self.generated),
            "invalidatedQuoteWork": self.invalidated_quote_work,
            "history": list(self.history),
        }


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Event:
    """Marker base class for session events."""


@dataclass(frozen=True, slots=True)
class EvaluateEligibility(Event):
    decision: EligibilityDecision


@dataclass(frozen=True, slots=True)
class OptInRescue(Event):
    pass


@dataclass(frozen=True, slots=True)
class DeclineRescue(Event):
    reason: str


@dataclass(frozen=True, slots=True)
class RecordConsent(Event):
    consent_version: str


@dataclass(frozen=True, slots=True)
class WithdrawConsent(Event):
    reason: str


@dataclass(frozen=True, slots=True)
class PrepareCandidates(Event):
    selection: CandidateSelection
    estimated_units: int
    budget_units: int
    # Phase D: defaults preserve the offline path exactly; a LIVE plan needs an authorization ref.
    mode: str = "MOCK"
    authorization_ref: str | None = None
    provider_version: str | None = None


@dataclass(frozen=True, slots=True)
class ReportNoCandidates(Event):
    selection: CandidateSelection


@dataclass(frozen=True, slots=True)
class RecordPhoto(Event):
    """The shopper's current photo, identified by its content digest."""

    photo_version: str


@dataclass(frozen=True, slots=True)
class TryAnotherGarment(Event):
    """Same photo, same consent, a different garment."""

    candidate_sku: str = ""


@dataclass(frozen=True, slots=True)
class ChangePhoto(Event):
    """A different shopper photo: the previous binding must not survive."""

    reason: str = "SHOPPER_CHANGED_PHOTO"


@dataclass(frozen=True, slots=True)
class ReceivePreviewResult(Event):
    result: PreviewResult


@dataclass(frozen=True, slots=True)
class ChooseCandidate(Event):
    candidate_sku: str
    quote: Quote


@dataclass(frozen=True, slots=True)
class ApproveExchange(Event):
    quote_id: str
    quote_version: int
    origin: str
    approved_at: datetime
    idempotency_key: str
    shopper_identity: str
    request_token: str


@dataclass(frozen=True, slots=True)
class CommitSucceeded(Event):
    commit_id: str
    transaction_id: str | None = None
    receipt: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CommitFailedKnown(Event):
    reason: str


@dataclass(frozen=True, slots=True)
class CommitUncertain(Event):
    attempt_id: str


@dataclass(frozen=True, slots=True)
class ReconcileCommitted(Event):
    commit_id: str
    transaction_id: str | None = None
    receipt: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReconcileNotCommitted(Event):
    reason: str


@dataclass(frozen=True, slots=True)
class ContinueRefund(Event):
    trigger: str


EVENT_TYPES: tuple[type[Event], ...] = (
    EvaluateEligibility,
    RecordPhoto,
    TryAnotherGarment,
    ChangePhoto,
    OptInRescue,
    DeclineRescue,
    RecordConsent,
    WithdrawConsent,
    PrepareCandidates,
    ReportNoCandidates,
    ReceivePreviewResult,
    ChooseCandidate,
    ApproveExchange,
    CommitSucceeded,
    CommitFailedKnown,
    CommitUncertain,
    ReconcileCommitted,
    ReconcileNotCommitted,
    ContinueRefund,
)


@dataclass(frozen=True, slots=True)
class Transition:
    session: SessionState
    accepted: bool
    edge: tuple[str, str] | None = None
    refusal: str | None = None
    note: str | None = None

    @property
    def state_changed(self) -> bool:
        return self.accepted and self.edge is not None and self.edge[0] != self.edge[1]


Handler = Callable[[SessionState, Any, datetime, Mapping[str, Any]], Transition]


def _refuse(session: SessionState, code: str, note: str | None = None) -> Transition:
    return Transition(session=session, accepted=False, refusal=code, note=note)


def _accept(
    session: SessionState,
    event: Event,
    *,
    new_state: str,
    note: str | None = None,
    **changes: Any,
) -> Transition:
    edge = (session.state, new_state)
    if edge not in ALLOWED_EDGES:  # pragma: no cover - programming error guard
        raise AssertionError(f"reducer tried an edge outside the contract: {edge}")
    history = session.history + (f"{session.version}:{type(event).__name__}->{new_state}",)
    updated = replace(
        session,
        state=new_state,
        version=session.version + 1,
        history=history,
        **changes,
    )
    return Transition(session=updated, accepted=True, edge=edge, note=note)


def _wait_seconds() -> float:
    """How long the product keeps asking the provider before offering reconciliation.

    The provider's published integration guide documents the create/poll contract but no
    turnaround time, so this is a bounded operator choice, not a claim about their latency.
    """
    raw = os.environ.get("SWAPSURE_LIVE_WAIT_SECONDS", "").strip()
    try:
        value = float(raw) if raw else 300.0
    except ValueError:
        value = 300.0
    return min(max(value, 30.0), 900.0)


def _quote_ttl(policy: Mapping[str, Any]) -> timedelta:
    return timedelta(seconds=float(policy["quoteTtlSeconds"]))


def _handle_evaluate(session, event: EvaluateEligibility, now, policy) -> Transition:
    if session.state != "RETURN_STARTED":
        return _refuse(session, WRONG_STATE)
    decision = event.decision
    if not isinstance(decision, EligibilityDecision):
        return _refuse(session, MALFORMED_EVENT, "decision must be an EligibilityDecision")
    if decision.status not in ALL_STATUSES:  # pragma: no cover - constructor guards
        return _refuse(session, MALFORMED_EVENT, f"unknown status {decision.status!r}")
    if decision.policy_version != policy["version"]:
        return _refuse(session, MALFORMED_EVENT, "decision policy version is not the active version")
    return _accept(session, event, new_state="ELIGIBILITY_CHECKED", decision=decision)


def _handle_opt_in(session, event: OptInRescue, now, policy) -> Transition:
    if session.state != "ELIGIBILITY_CHECKED":
        return _refuse(session, WRONG_STATE)
    if session.decision is None:
        return _refuse(session, MISSING_DECISION)
    if not session.decision.eligible or session.decision.status != ELIGIBLE_VISUAL_RESCUE:
        return _refuse(session, NOT_ELIGIBLE, session.decision.status)
    return _accept(session, event, new_state="CONSENT_PENDING")


def _handle_decline(session, event: DeclineRescue, now, policy) -> Transition:
    if session.state != "ELIGIBILITY_CHECKED":
        return _refuse(session, WRONG_STATE)
    if not isinstance(event.reason, str) or not event.reason.strip():
        return _refuse(session, MALFORMED_EVENT, "a decline reason is required for the audit trail")
    return _accept(session, event, new_state="REFUND_HANDOFF", note=event.reason.strip())


def _handle_consent(session, event: RecordConsent, now, policy) -> Transition:
    if session.state != "CONSENT_PENDING":
        return _refuse(session, WRONG_STATE)
    if not isinstance(event.consent_version, str) or not event.consent_version.strip():
        return _refuse(session, MALFORMED_EVENT, "consent_version is required")
    return _accept(
        session,
        event,
        new_state="CANDIDATES_READY",
        consent_version=event.consent_version.strip(),
    )


def _handle_withdraw(session, event: WithdrawConsent, now, policy) -> Transition:
    if session.state != "CONSENT_PENDING":
        return _refuse(session, WRONG_STATE)
    if not isinstance(event.reason, str) or not event.reason.strip():
        return _refuse(session, MALFORMED_EVENT, "a withdrawal reason is required")
    return _accept(
        session,
        event,
        new_state="REFUND_HANDOFF",
        consent_version=None,
        note=event.reason.strip(),
    )


def _selection_is_usable(session: SessionState, selection: CandidateSelection) -> str | None:
    if selection.session_id != session.session_id:
        return SELECTION_SESSION_MISMATCH
    if not selection.inventory_fresh:
        return STALE_INVENTORY
    return None


def _handle_record_photo(session, event: RecordPhoto, now, policy) -> Transition:
    if session.state != "CANDIDATES_READY":
        return _refuse(session, WRONG_STATE)
    if not session.consent_version:
        return _refuse(session, MISSING_CONSENT)
    version = event.photo_version
    if not isinstance(version, str) or not version.strip():
        return _refuse(session, MALFORMED_EVENT, "a photo digest is required")
    # A new photo invalidates any plan or result built for the previous one.
    return _accept(session, event, new_state="CANDIDATES_READY",
                   photo_version=version.strip(), preview_plan=None, preview_result=None)


def _handle_try_another(session, event: TryAnotherGarment, now, policy) -> Transition:
    if session.state != "DECISION_READY":
        return _refuse(session, WRONG_STATE)
    if not session.consent_version:
        return _refuse(session, MISSING_CONSENT)
    if not session.photo_version:
        return _refuse(session, GUARD_FAILED, "no photo is bound to this session")
    # The photo and its consent stay; only the choice of garment is re-opened. Whether the new
    # choice may be spent is decided at plan time against the same budget and the generated pairs.
    # The stored selection is the *chosen* one from the last plan; clearing it makes the full
    # eligible set available again for the new choice.
    return _accept(session, event, new_state="CANDIDATES_READY", selection=None,
                   preview_plan=None, preview_result=None)


def _handle_change_photo(session, event: ChangePhoto, now, policy) -> Transition:
    if session.state not in ("CANDIDATES_READY", "DECISION_READY"):
        return _refuse(session, WRONG_STATE)
    if not isinstance(event.reason, str) or not event.reason.strip():
        return _refuse(session, MALFORMED_EVENT, "a change-photo reason is required")
    # Fresh consent for the new photo: the old grant authorised the old photo only.
    return _accept(session, event, new_state="CONSENT_PENDING", photo_version=None,
                   consent_version=None, selection=None, preview_plan=None,
                   preview_result=None, invalidated_preview_work=True)


def _handle_prepare(session, event: PrepareCandidates, now, policy) -> Transition:
    if session.state != "CANDIDATES_READY":
        return _refuse(session, WRONG_STATE)
    if not session.consent_version:
        return _refuse(session, MISSING_CONSENT)
    selection = event.selection
    if not isinstance(selection, CandidateSelection):
        return _refuse(session, MALFORMED_EVENT, "selection is required")
    problem = _selection_is_usable(session, selection)
    if problem is not None:
        return _refuse(session, problem)
    if not selection.has_candidates:
        return _refuse(session, NO_CANDIDATES)
    for value in (event.estimated_units, event.budget_units):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return _refuse(session, MALFORMED_EVENT, "budget values must be non-negative integers")
    if event.estimated_units > event.budget_units:
        return _refuse(session, BUDGET_EXCEEDED)

    plan = PreviewPlan(
        session_id=session.session_id,
        consent_version=session.consent_version,
        candidate_ids=selection.candidate_skus,
        provider_version=event.provider_version or ("cloth-v4" if event.mode == "LIVE" else "mock-v1"),
        estimated_units=event.estimated_units,
        authorization_ref=event.authorization_ref,
        deadline=now + timedelta(seconds=_wait_seconds()),
        mode=event.mode,
    )
    return _accept(
        session,
        event,
        new_state="PREVIEW_PENDING",
        selection=selection,
        preview_plan=plan,
    )


def _handle_no_candidates(session, event: ReportNoCandidates, now, policy) -> Transition:
    if session.state != "CANDIDATES_READY":
        return _refuse(session, WRONG_STATE)
    selection = event.selection
    if not isinstance(selection, CandidateSelection):
        return _refuse(session, MALFORMED_EVENT, "selection is required")
    problem = _selection_is_usable(session, selection)
    if problem is not None:
        return _refuse(session, problem)
    if selection.has_candidates:
        return _refuse(session, CANDIDATES_PRESENT)
    return _accept(session, event, new_state="REFUND_HANDOFF", selection=selection)


def _handle_preview_result(session, event: ReceivePreviewResult, now, policy) -> Transition:
    if session.state != "PREVIEW_PENDING":
        return _refuse(session, WRONG_STATE)
    result = event.result
    if not isinstance(result, PreviewResult):
        return _refuse(session, MALFORMED_EVENT, "result is required")
    if result.session_id != session.session_id:
        return _refuse(session, PREVIEW_CROSS_SESSION)
    if result.mode not in ("MOCK", "LIVE"):
        return _refuse(session, PREVIEW_MODE_UNAVAILABLE)
    if result.mode == "LIVE":
        # A live result may only enter a session whose plan was authorised as live, and a live
        # success must carry provider media for every candidate it claims (no placeholders).
        plan = session.preview_plan
        if plan is None or plan.mode != "LIVE":
            return _refuse(session, PREVIEW_MODE_UNAVAILABLE)
        handles = tuple(result.media_handles)
        if result.status == "SUCCEEDED" and any(h is None for h in handles):
            return _refuse(session, MALFORMED_EVENT, "a live success must carry media for every candidate")
        if result.status == "PARTIAL":
            if not any(h is not None for h in handles):
                return _refuse(session, MALFORMED_EVENT, "a partial result must carry at least one image")
        if result.status == "FAILED" and any(h is not None for h in handles):
            return _refuse(session, MALFORMED_EVENT, "a failed live result cannot carry images")
        if result.status == "UNRESOLVED" and any(h is not None for h in handles):
            return _refuse(session, MALFORMED_EVENT, "an unanswered live result cannot carry images")
    bound = getattr(result, "photo_version", None)
    if session.photo_version and bound and bound != session.photo_version:
        # The photo changed while this preview was in flight; it belongs to the previous photo.
        return _refuse(session, STALE_PHOTO_BINDING)
    unknown = set(result.candidate_ids) - set(session.candidate_skus())
    if unknown:
        return _refuse(session, CANDIDATE_NOT_AVAILABLE, ",".join(sorted(unknown)))
    if now >= result.expires_at:
        return _refuse(session, GUARD_FAILED, "late preview result discarded")
    # A partial or failed run is still a decision point; cards and refund remain.
    photo = bound or session.photo_version
    produced = tuple(f"{photo}:{cid}" for cid in result.candidate_ids) if photo else ()
    return _accept(session, event, new_state="DECISION_READY", preview_result=result,
                   generated=tuple(dict.fromkeys(session.generated + produced)))


def _handle_choose(session, event: ChooseCandidate, now, policy) -> Transition:
    if session.state != "DECISION_READY":
        return _refuse(session, WRONG_STATE)
    if session.selection is None:
        return _refuse(session, NO_CANDIDATES)
    if event.candidate_sku not in session.candidate_skus():
        return _refuse(session, CANDIDATE_NOT_AVAILABLE, event.candidate_sku)
    quote = event.quote
    if not isinstance(quote, Quote):
        return _refuse(session, MALFORMED_EVENT, "quote is required")
    expected_version = session.selection.inventory_version
    if quote.inventory_version != expected_version:
        return _refuse(session, STALE_INVENTORY, "quote was priced on another inventory version")
    if quote.candidate_variant_id != event.candidate_sku:
        return _refuse(session, QUOTE_MISMATCH, "quote is for a different candidate")
    if quote.order_id != session.order_id:
        return _refuse(session, QUOTE_MISMATCH, "quote is for a different order")
    if quote.currency != session.selection.currency:
        return _refuse(session, QUOTE_MISMATCH, "quote currency differs from the order")
    if quote.is_expired(now):
        return _refuse(session, QUOTE_EXPIRED)
    return _accept(session, event, new_state="QUOTE_READY", quote=quote)


def _handle_approve(session, event: ApproveExchange, now, policy) -> Transition:
    if session.state != "QUOTE_READY":
        return _refuse(session, WRONG_STATE)
    quote = session.quote
    if quote is None:
        return _refuse(session, QUOTE_MISMATCH, "no quote to approve")
    if not isinstance(event.origin, str) or event.origin != TRUSTED_APPROVAL_ORIGIN:
        return _refuse(session, NOT_SHOPPER_ORIGIN, "only a shopper UI action can approve")
    if event.shopper_identity != session.shopper_identity:
        return _refuse(session, SHOPPER_IDENTITY_MISMATCH)
    if event.request_token != session.request_token:
        return _refuse(session, REQUEST_TOKEN_MISMATCH)
    if not isinstance(event.idempotency_key, str) or not event.idempotency_key.strip():
        return _refuse(session, MALFORMED_EVENT, "idempotency key is required")
    if event.quote_id != quote.quote_id or event.quote_version != quote.quote_version:
        return _refuse(session, QUOTE_MISMATCH, "approval does not name the current quote version")
    if event.approved_at.tzinfo is None:
        return _refuse(session, MALFORMED_EVENT, "approval timestamp must be timezone-aware")
    if quote.is_expired(event.approved_at) or quote.is_expired(now):
        return _refuse(session, QUOTE_EXPIRED)
    if event.approved_at < quote.issued_at or event.approved_at > now + timedelta(seconds=5):
        return _refuse(session, MALFORMED_EVENT, "approval timestamp is outside the quote window")

    approval = Approval(
        quote_id=quote.quote_id,
        quote_version=quote.quote_version,
        origin=event.origin,
        approved_at=event.approved_at,
        idempotency_key=event.idempotency_key.strip(),
        request_token=event.request_token,
    )
    return _accept(session, event, new_state="COMMIT_PENDING", approval=approval)


def _handle_commit_success(session, event: CommitSucceeded, now, policy) -> Transition:
    if session.state != "COMMIT_PENDING":
        return _refuse(session, WRONG_STATE)
    if not isinstance(event.commit_id, str) or not event.commit_id.strip():
        return _refuse(session, MALFORMED_EVENT, "commit_id is required")
    return _accept(
        session,
        event,
        new_state="EXCHANGE_COMMITTED",
        commit_id=event.commit_id,
        transaction_id=event.transaction_id or event.commit_id,
        receipt=event.receipt,
    )


def _handle_commit_failure(session, event: CommitFailedKnown, now, policy) -> Transition:
    if session.state != "COMMIT_PENDING":
        return _refuse(session, WRONG_STATE)
    if not isinstance(event.reason, str) or not event.reason.strip():
        return _refuse(session, MALFORMED_EVENT, "a failure reason is required")
    return _accept(
        session,
        event,
        new_state="DECISION_READY",
        quote=None,
        approval=None,
        invalidated_quote_work=True,
        note=event.reason.strip(),
    )


def _handle_commit_uncertain(session, event: CommitUncertain, now, policy) -> Transition:
    if session.state != "COMMIT_PENDING":
        return _refuse(session, WRONG_STATE)
    if not isinstance(event.attempt_id, str) or not event.attempt_id.strip():
        return _refuse(session, MALFORMED_EVENT, "attempt_id is required")
    return _accept(session, event, new_state="RECONCILING", note=event.attempt_id.strip())


def _handle_reconcile_committed(session, event: ReconcileCommitted, now, policy) -> Transition:
    if session.state != "RECONCILING":
        return _refuse(session, WRONG_STATE)
    if not isinstance(event.commit_id, str) or not event.commit_id.strip():
        return _refuse(session, MALFORMED_EVENT, "commit_id is required")
    return _accept(
        session,
        event,
        new_state="EXCHANGE_COMMITTED",
        commit_id=event.commit_id,
        transaction_id=event.transaction_id or event.commit_id,
        receipt=event.receipt,
    )


def _handle_reconcile_not_committed(session, event: ReconcileNotCommitted, now, policy) -> Transition:
    if session.state != "RECONCILING":
        return _refuse(session, WRONG_STATE)
    if not isinstance(event.reason, str) or not event.reason.strip():
        return _refuse(session, MALFORMED_EVENT, "a reconciliation reason is required")
    return _accept(
        session,
        event,
        new_state="REFUND_HANDOFF",
        quote=None,
        approval=None,
        invalidated_quote_work=True,
        note=event.reason.strip(),
    )


def _handle_continue_refund(session, event: ContinueRefund, now, policy) -> Transition:
    if not isinstance(event.trigger, str) or not event.trigger.strip():
        return _refuse(session, MALFORMED_EVENT, "a refund trigger is required for the audit trail")
    if is_terminal(session.state):
        # The host owns the refund lifecycle after handoff; this reducer has no
        # edge out of a terminal state and inventing one would break the contract.
        return _refuse(session, TERMINAL_STATE, "host owns the refund lifecycle after handoff")
    if not is_precommit(session.state):
        # Commit has started: record the request, keep ordinary support access,
        # and let reconciliation resolve the single in-flight transaction.
        recorded = replace(
            session,
            refund_recorded=True,
            refund_recorded_at=now,
        )
        return Transition(
            session=recorded,
            accepted=False,
            refusal=REFUND_RECORDED_PENDING_COMMIT,
            note="refund recorded; no second payment action started",
        )
    return _accept(
        session,
        event,
        new_state="REFUND_HANDOFF",
        quote=None,
        approval=None,
        preview_result=None,
        invalidated_preview_work=True,
        invalidated_quote_work=True,
        note=event.trigger.strip(),
    )


_HANDLERS: dict[type[Event], Handler] = {
    EvaluateEligibility: _handle_evaluate,
    OptInRescue: _handle_opt_in,
    DeclineRescue: _handle_decline,
    RecordConsent: _handle_consent,
    WithdrawConsent: _handle_withdraw,
    RecordPhoto: _handle_record_photo,
    TryAnotherGarment: _handle_try_another,
    ChangePhoto: _handle_change_photo,
    PrepareCandidates: _handle_prepare,
    ReportNoCandidates: _handle_no_candidates,
    ReceivePreviewResult: _handle_preview_result,
    ChooseCandidate: _handle_choose,
    ApproveExchange: _handle_approve,
    CommitSucceeded: _handle_commit_success,
    CommitFailedKnown: _handle_commit_failure,
    CommitUncertain: _handle_commit_uncertain,
    ReconcileCommitted: _handle_reconcile_committed,
    ReconcileNotCommitted: _handle_reconcile_not_committed,
    ContinueRefund: _handle_continue_refund,
}


def initial_session(
    session_id: str,
    order_id: str,
    shopper_identity: str,
    request_token: str,
) -> SessionState:
    return SessionState(
        session_id=session_id,
        order_id=order_id,
        shopper_identity=shopper_identity,
        request_token=request_token,
    )


def transition(
    session: SessionState,
    event: Event,
    *,
    now: datetime,
    expected_version: int | None = None,
    policy: Mapping[str, Any] | None = None,
) -> Transition:
    """Apply one event.  Refusals leave the session object untouched.

    ``expected_version`` implements the optimistic-concurrency check the commerce
    layer relies on for the refund/exchange race.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    effective_policy = load_policy() if policy is None else policy

    if expected_version is not None and expected_version != session.version:
        return _refuse(
            session,
            VERSION_CONFLICT,
            f"expected version {expected_version}, session is at {session.version}",
        )

    handler = _HANDLERS.get(type(event))
    if handler is None:
        return _refuse(session, UNSUPPORTED_EVENT, type(event).__name__)

    return handler(session, event, now, effective_policy)


def edges_to(state: str) -> tuple[str, ...]:
    """Contract edges leaving ``state``, in contract order."""

    return tuple(to for source, to in ALLOWED_EDGES if source == state)


def guard_summary() -> dict[str, dict[str, str]]:
    """Documentation helper: which event may leave each state."""

    summary: dict[str, dict[str, str]] = {state: {} for state in STATES}
    summary["RETURN_STARTED"] = {
        "EvaluateEligibility": "valid structured decision, active policy version",
        "ContinueRefund": "always available",
    }
    summary["ELIGIBILITY_CHECKED"] = {
        "OptInRescue": "status is ELIGIBLE_VISUAL_RESCUE",
        "DeclineRescue": "shopper declines rescue",
        "ContinueRefund": "always available",
    }
    summary["CONSENT_PENDING"] = {
        "RecordConsent": "non-empty consent version",
        "WithdrawConsent": "shopper withdraws consent",
        "ContinueRefund": "always available",
    }
    summary["CANDIDATES_READY"] = {
        "PrepareCandidates": ">=1 valid candidate, consent, budget",
        "ReportNoCandidates": "selection is empty",
        "ContinueRefund": "always available",
    }
    summary["PREVIEW_PENDING"] = {
        "ReceivePreviewResult": "same session, same candidates, not expired",
        "ContinueRefund": "always available",
    }
    summary["DECISION_READY"] = {
        "ChooseCandidate": "candidate visible, quote matches inventory version",
        "ContinueRefund": "always available",
    }
    summary["QUOTE_READY"] = {
        "ApproveExchange": "explicit shopper approval of the exact unexpired quote",
        "ContinueRefund": "always available",
    }
    summary["COMMIT_PENDING"] = {
        "CommitSucceeded": "atomic commit acknowledged",
        "CommitFailedKnown": "known failure, no side effect",
        "CommitUncertain": "outcome unknown",
    }
    summary["RECONCILING"] = {
        "ReconcileCommitted": "prior transaction confirmed committed",
        "ReconcileNotCommitted": "confirmed not committed",
    }
    return summary
