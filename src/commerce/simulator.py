"""Atomic local exchange simulator — Phase B commerce contract.

This module simulates an exchange *locally*.  It never contacts a merchant, order
or payment system; there is no transport, no credential and no network import.

Guarantees implemented (each has a test):

* **Explicit human approval only.** A commit requires ``action=APPROVE_EXCHANGE``
  from the shopper UI origin, for the shopper's own session, with the session's
  request token, naming the exact unexpired quote id *and* version, plus an
  idempotency key.  Selection, preview completion, silence and agent
  recommendation are different actions and are refused as confirmation.
* **One atomic boundary.** Session state, inventory revalidation, the transaction
  record, the stock decrement and the ledger write happen under a single
  re-entrant lock; a refund request and an exchange approval therefore cannot
  interleave (T17).
* **Revalidation at commit.** Price, currency and stock are re-read under the lock.
  A change produces a known failure with no side effect and returns the session to
  ``DECISION_READY`` for a fresh quote and fresh approval (T13, T14).
* **Idempotency.** Same key + same commerce payload returns the recorded outcome
  (never a second transaction); same key + changed payload is a conflict (T15,
  T16).
* **No blind retry.** A lost acknowledgement parks the session in ``RECONCILING``
  and the only forward path is reconciling by existing transaction id (T30).
* **Durability.** Every mutation is written to an optional JSON store; a restart
  reproduces the same state, and an in-doubt commit is resolved, not repeated.

Retained GMV is never computed here: the ledger owns that definition.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from commerce import codec
from commerce.confirmation import ACTION_APPROVE_EXCHANGE, TRUSTED_ORIGIN, ConfirmRequest
from commerce.inventory import Inventory, StockRow
from commerce.order import OrderView
from commerce.ledger import (
    OUTCOME_EXCHANGE_COMMITTED,
    OUTCOME_PENDING,
    OUTCOME_RECONCILING,
    OUTCOME_REFUND_HANDOFF,
    OutcomeLedger,
    OutcomeRecord,
    committed_exchange,
    non_committed_outcome,
)
from commerce.quote import Quote, build_quote, revalidate
from domain.catalog import CandidateSelection
from domain.eligibility import ELIGIBLE_VISUAL_RESCUE, EligibilityDecision
from domain.state_machine import (
    Approval,
    ApproveExchange,
    ChooseCandidate,
    CommitFailedKnown,
    CommitSucceeded,
    CommitUncertain,
    ContinueRefund,
    EvaluateEligibility,
    Event,
    OptInRescue,
    PrepareCandidates,
    RecordPhoto,
    TryAnotherGarment,
    ChangePhoto,
    ReceivePreviewResult,
    RecordConsent,
    ReconcileCommitted,
    ReconcileNotCommitted,
    ReportNoCandidates,
    SessionState,
    Transition,
    WithdrawConsent,
    initial_session,
    transition,
)
from specs import policy as load_policy

Clock = Callable[[], datetime]

# Outcomes of a confirmation attempt.
COMMITTED = "COMMITTED"
REPLAYED = "REPLAYED"
CONFLICT = "CONFLICT"
REFUSED = "REFUSED"
KNOWN_FAILURE = "KNOWN_FAILURE"
UNCERTAIN = "UNCERTAIN"

# Refusal / failure reasons.
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
SELECTION_IS_NOT_APPROVAL = "SELECTION_IS_NOT_APPROVAL"
PREVIEW_IS_NOT_APPROVAL = "PREVIEW_IS_NOT_APPROVAL"
AGENT_RECOMMENDATION_IS_NOT_APPROVAL = "AGENT_RECOMMENDATION_IS_NOT_APPROVAL"
UNKNOWN_ACTION_IS_NOT_APPROVAL = "UNKNOWN_ACTION_IS_NOT_APPROVAL"
NOT_SHOPPER_ORIGIN = "NOT_SHOPPER_ORIGIN"
SHOPPER_MISMATCH = "SHOPPER_MISMATCH"
REQUEST_TOKEN_MISMATCH = "REQUEST_TOKEN_MISMATCH"
QUOTE_MISMATCH = "QUOTE_MISMATCH"
EXPIRED_QUOTE = "EXPIRED_QUOTE"
REFUND_WON = "REFUND_WON"
IN_DOUBT_RECONCILE_FIRST = "IN_DOUBT_RECONCILE_FIRST"
TERMINAL_STATE = "TERMINAL_STATE"
WRONG_STATE = "WRONG_STATE"
STOCK_CHANGED = "STOCK_CHANGED"
PRICE_CHANGED = "PRICE_CHANGED"
CURRENCY_CHANGED = "CURRENCY_CHANGED"
UNKNOWN_SKU = "UNKNOWN_SKU"
SIMULATED_STORE_FAILURE = "SIMULATED_STORE_FAILURE"
IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD = "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"
MALFORMED_REQUEST = "MALFORMED_REQUEST"
IN_DOUBT = "IN_DOUBT"

# Refund outcomes.
REFUND_HANDOFF_STARTED = "REFUND_HANDOFF_STARTED"
REFUND_ALREADY_STARTED = "REFUND_ALREADY_STARTED"
REFUND_RECORDED = "REFUND_RECORDED"
REFUND_AFTER_COMMIT = "REFUND_AFTER_COMMIT"
REFUND_REFUSED = "REFUND_REFUSED"

REQUIRE_FRESH_QUOTE = "REQUEST_FRESH_QUOTE_AND_NEW_APPROVAL"
RECONCILE_REQUIRED = "RECONCILE_BY_TRANSACTION_ID"


class StoreError(RuntimeError):
    """Simulator-level failure."""


class StoreCrashed(StoreError):
    """Simulated process death after a durable write but before any acknowledgement."""

    def __init__(self, transaction_id: str) -> None:
        super().__init__(f"simulated crash after durable write of {transaction_id}")
        self.transaction_id = transaction_id


class FaultKind(str, Enum):
    """Injected faults at precise points of the commit sequence."""

    FAIL_BEFORE_WRITE = "FAIL_BEFORE_WRITE"
    FAIL_AFTER_WRITE = "FAIL_AFTER_WRITE"
    PERSIST_THEN_CRASH = "PERSIST_THEN_CRASH"


class FaultQueue:
    """Deterministic fault programme: one entry consumed per commit attempt."""

    def __init__(self, kinds: Iterable[FaultKind | str] = ()) -> None:
        self._queue: list[FaultKind] = [FaultKind(kind) for kind in kinds]

    def push(self, kind: FaultKind | str) -> None:
        self._queue.append(FaultKind(kind))

    def pop(self) -> FaultKind | None:
        return self._queue.pop(0) if self._queue else None

    @property
    def pending(self) -> int:
        return len(self._queue)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """The auditable part of an approval.  Deliberately excludes the request token.

    A receipt is shown to the shopper and may be exported for support; the session
    request token is an anti-CSRF secret and has no business in it.
    """

    quote_id: str
    quote_version: int
    origin: str
    approved_at: datetime
    idempotency_key: str

    @classmethod
    def from_approval(cls, approval: Approval) -> "ApprovalRecord":
        return cls(
            quote_id=approval.quote_id,
            quote_version=approval.quote_version,
            origin=approval.origin,
            approved_at=approval.approved_at,
            idempotency_key=approval.idempotency_key,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "quoteId": self.quote_id,
            "quoteVersion": self.quote_version,
            "origin": self.origin,
            "approvedAt": self.approved_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "idempotencyKey": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApprovalRecord":
        return cls(
            quote_id=str(raw["quoteId"]),
            quote_version=int(raw["quoteVersion"]),
            origin=str(raw["origin"]),
            approved_at=_parse_iso(str(raw["approvedAt"])),
            idempotency_key=str(raw["idempotencyKey"]),
        )


@dataclass(frozen=True, slots=True)
class Receipt:
    """Rescue Receipt.  No image bytes, handles or provider URLs."""

    commit_id: str
    session_id: str
    order_id: str
    return_reason_codes: tuple[str, ...]
    eligibility_status: str
    eligibility_rationale: str
    policy_version: str
    original_sku: str
    replacement_sku: str
    original_value_minor: int
    replacement_value_minor: int
    currency: str
    stock_checked_at: datetime
    stock_check_inventory_version: str
    candidates_eligible: int
    previews_delivered: int
    preview_mode: str
    approval: ApprovalRecord
    partial_refund_minor: int
    additional_charge_minor: int
    retained_gmv_minor: int
    outcome: str
    committed_at: datetime
    return_support_ref: str
    mode_label: str = "SIMULATED EXCHANGE - MOCK PREVIEW"

    def as_dict(self) -> dict[str, Any]:
        return {
            "commitId": self.commit_id,
            "sessionId": self.session_id,
            "orderId": self.order_id,
            "returnReasonCodes": list(self.return_reason_codes),
            "eligibilityStatus": self.eligibility_status,
            "eligibilityRationale": self.eligibility_rationale,
            "policyVersion": self.policy_version,
            "originalSku": self.original_sku,
            "replacementSku": self.replacement_sku,
            "originalValueMinor": self.original_value_minor,
            "replacementValueMinor": self.replacement_value_minor,
            "currency": self.currency,
            "stockCheckedAt": self.stock_checked_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "stockCheckInventoryVersion": self.stock_check_inventory_version,
            "candidatesEligible": self.candidates_eligible,
            "previewsDelivered": self.previews_delivered,
            "previewMode": self.preview_mode,
            "approval": self.approval.as_dict(),
            "partialRefundMinor": self.partial_refund_minor,
            "additionalChargeMinor": self.additional_charge_minor,
            "retainedGmvMinor": self.retained_gmv_minor,
            "outcome": self.outcome,
            "committedAt": self.committed_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "returnSupportRef": self.return_support_ref,
            "modeLabel": self.mode_label,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Receipt":
        return cls(
            commit_id=str(raw["commitId"]),
            session_id=str(raw["sessionId"]),
            order_id=str(raw["orderId"]),
            return_reason_codes=tuple(str(item) for item in raw.get("returnReasonCodes", [])),
            eligibility_status=str(raw["eligibilityStatus"]),
            eligibility_rationale=str(raw["eligibilityRationale"]),
            policy_version=str(raw["policyVersion"]),
            original_sku=str(raw["originalSku"]),
            replacement_sku=str(raw["replacementSku"]),
            original_value_minor=int(raw["originalValueMinor"]),
            replacement_value_minor=int(raw["replacementValueMinor"]),
            currency=str(raw["currency"]),
            stock_checked_at=_parse_iso(str(raw["stockCheckedAt"])),
            stock_check_inventory_version=str(raw["stockCheckInventoryVersion"]),
            candidates_eligible=int(raw["candidatesEligible"]),
            previews_delivered=int(raw["previewsDelivered"]),
            preview_mode=str(raw["previewMode"]),
            approval=ApprovalRecord.from_dict(raw["approval"]),  # type: ignore[arg-type]
            partial_refund_minor=int(raw["partialRefundMinor"]),
            additional_charge_minor=int(raw["additionalChargeMinor"]),
            retained_gmv_minor=int(raw["retainedGmvMinor"]),
            outcome=str(raw["outcome"]),
            committed_at=_parse_iso(str(raw["committedAt"])),
            return_support_ref=str(raw["returnSupportRef"]),
            mode_label=str(raw.get("modeLabel", "SIMULATED EXCHANGE - MOCK PREVIEW")),
        )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    outcome: str
    reason: str | None
    session_state: str
    session_version: int
    transaction_id: str | None = None
    receipt: Receipt | None = None
    required_action: str | None = None
    revalidation: Mapping[str, Any] | None = None

    @property
    def committed(self) -> bool:
        return self.outcome in (COMMITTED, REPLAYED)


@dataclass(frozen=True, slots=True)
class RefundResult:
    outcome: str
    reason: str | None
    session_state: str
    session_version: int
    transaction_id: str | None = None
    refund_route_available: bool = True


@dataclass(frozen=True, slots=True)
class Transaction:
    transaction_id: str
    session_id: str
    order_id: str
    status: str
    idempotency_key: str
    payload_digest: str
    quote_id: str
    candidate_sku: str
    receipt: Receipt | None = None
    committed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "transactionId": self.transaction_id,
            "sessionId": self.session_id,
            "orderId": self.order_id,
            "status": self.status,
            "idempotencyKey": self.idempotency_key,
            "payloadDigest": self.payload_digest,
            "quoteId": self.quote_id,
            "candidateSku": self.candidate_sku,
            "receipt": None if self.receipt is None else self.receipt.as_dict(),
            "committedAt": None
            if self.committed_at is None
            else self.committed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Transaction":
        receipt_raw = raw.get("receipt")
        committed = raw.get("committedAt")
        return cls(
            transaction_id=str(raw["transactionId"]),
            session_id=str(raw["sessionId"]),
            order_id=str(raw["orderId"]),
            status=str(raw["status"]),
            idempotency_key=str(raw["idempotencyKey"]),
            payload_digest=str(raw["payloadDigest"]),
            quote_id=str(raw["quoteId"]),
            candidate_sku=str(raw["candidateSku"]),
            receipt=None if receipt_raw is None else Receipt.from_dict(receipt_raw),
            committed_at=None if committed is None else _parse_iso(str(committed)),
        )


TXN_IN_FLIGHT = "IN_FLIGHT"
TXN_COMMITTED = "COMMITTED"
TXN_ABANDONED = "ABANDONED"


class CommerceSimulator:
    """One locked, durable, local commerce store."""

    def __init__(
        self,
        inventory: Inventory,
        *,
        clock: Clock = utc_now,
        store_path: str | os.PathLike[str] | None = None,
        ledger_sink: Callable[[OutcomeRecord], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._inventory = inventory
        self._sessions: dict[str, SessionState] = {}
        self._orders: dict[str, OrderView] = {}
        self._transactions: dict[str, Transaction] = {}
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._ledger = OutcomeLedger()
        self._sink = ledger_sink
        self._store_path = pathlib.Path(store_path) if store_path is not None else None
        self.faults = FaultQueue()
        self._dead = False
        self.last_flush: Any = None

        if self._store_path is not None and self._store_path.exists():
            self._load_locked()

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def from_fixture_rows(
        cls,
        rows: Iterable[Mapping[str, Any]],
        *,
        clock: Clock = utc_now,
        store_path: str | os.PathLike[str] | None = None,
        ledger_sink: Callable[[OutcomeRecord], None] | None = None,
    ) -> "CommerceSimulator":
        inventory = Inventory(
            StockRow(
                sku=str(row["sku"]),
                merchant=str(row["merchant"]),
                category=str(row["category"]),
                size=str(row["size"]),
                currency=str(row["currency"]),
                price_minor=int(row["priceMinor"]),
                stock=int(row["stock"]),
                color_family=str(row["colorFamily"]),
                reference_rights=str(row.get("referenceRights", "")),
                style=row.get("style"),
                occasion=row.get("occasion"),
            )
            for row in rows
        )
        return cls(inventory, clock=clock, store_path=store_path, ledger_sink=ledger_sink)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    @property
    def inventory(self) -> Inventory:
        return self._inventory

    @property
    def ledger(self) -> OutcomeLedger:
        return self._ledger

    @property
    def alive(self) -> bool:
        return not self._dead

    def session(self, session_id: str) -> SessionState:
        with self._lock:
            self._require_alive()
            session = self._sessions.get(session_id)
            if session is None:
                raise StoreError(f"unknown session: {session_id}")
            return session

    def session_or_none(self, session_id: str) -> SessionState | None:
        with self._lock:
            self._require_alive()
            return self._sessions.get(session_id)

    def transactions(self) -> tuple[Transaction, ...]:
        with self._lock:
            return tuple(self._transactions[key] for key in sorted(self._transactions))

    def transaction_for_session(self, session_id: str) -> Transaction | None:
        for transaction in self.transactions():
            if transaction.session_id == session_id:
                return transaction
        return None

    def idempotency_record(self, key: str) -> Mapping[str, Any] | None:
        with self._lock:
            record = self._idempotency.get(key)
            return None if record is None else dict(record)

    def metrics(self, *, eligible_optins: int = 0) -> dict[str, Any]:
        with self._lock:
            return self._ledger.metrics(eligible_optins=eligible_optins).as_dict()

    def flush_ledger(self) -> Any:
        with self._lock:
            if self._sink is None:
                self.last_flush = None
                return None
            result = self._ledger.flush(self._sink)
            self.last_flush = result
            self._persist_locked()
            return result

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #
    def open_session(
        self,
        order: OrderView,
        *,
        shopper_identity: str,
        request_token: str,
        session_id: str | None = None,
    ) -> SessionState:
        with self._lock:
            self._require_alive()
            identifier = session_id or f"ses_{order.order_id}"
            if identifier in self._sessions:
                raise StoreError(f"session already exists: {identifier}")
            self._orders[order.order_id] = order
            session = initial_session(identifier, order.order_id, shopper_identity, request_token)
            self._sessions[identifier] = session
            self._persist_locked()
            return session

    def apply_event(self, session_id: str, event: Event, *, expected_version: int | None = None) -> Transition:
        """Drive the reducer for a non-commerce event, persisting the result."""

        with self._lock:
            self._require_alive()
            session = self._require_session(session_id)
            result = transition(
                session,
                event,
                now=self._clock(),
                expected_version=session.version if expected_version is None else expected_version,
                policy=load_policy(),
            )
            if result.accepted:
                self._sessions[session_id] = result.session
                self._persist_locked()
            return result

    def evaluate(self, session_id: str, decision: EligibilityDecision) -> Transition:
        return self.apply_event(session_id, EvaluateEligibility(decision))

    def opt_in(self, session_id: str) -> Transition:
        return self.apply_event(session_id, OptInRescue())

    def record_consent(self, session_id: str, consent_version: str) -> Transition:
        return self.apply_event(session_id, RecordConsent(consent_version))

    def prepare_candidates(
        self, session_id: str, selection: CandidateSelection, *, budget_units: int = 0,
        mode: str = "MOCK", authorization_ref: str | None = None, unit_cost: int = 0,
    ) -> Transition:
        units = unit_cost * len(selection.candidate_skus) if mode == "LIVE" else 0
        return self.apply_event(
            session_id,
            PrepareCandidates(selection=selection, estimated_units=units, budget_units=budget_units,
                              mode=mode, authorization_ref=authorization_ref),
        )

    def report_no_candidates(self, session_id: str, selection: CandidateSelection) -> Transition:
        return self.apply_event(session_id, ReportNoCandidates(selection=selection))

    def record_photo(self, session_id: str, photo_version: str) -> Transition:
        return self.apply_event(session_id, RecordPhoto(photo_version=photo_version))

    def try_another(self, session_id: str, candidate_sku: str = "") -> Transition:
        return self.apply_event(session_id, TryAnotherGarment(candidate_sku=candidate_sku))

    def change_photo(self, session_id: str, reason: str = "SHOPPER_CHANGED_PHOTO") -> Transition:
        return self.apply_event(session_id, ChangePhoto(reason=reason))

    def receive_preview_result(self, session_id: str, result: Any) -> Transition:
        return self.apply_event(session_id, ReceivePreviewResult(result=result))

    # ------------------------------------------------------------------ #
    # Quote
    # ------------------------------------------------------------------ #
    def build_quote_for(self, session_id: str, candidate_sku: str, *, quote_version: int = 1) -> Quote:
        """Price one candidate against the inventory the shopper is looking at."""

        with self._lock:
            now = self._clock()
            session = self._require_session(session_id)
            order = self._require_order(session.order_id)
            row = self._inventory.get(candidate_sku)
            if row is None:
                raise StoreError(f"unknown candidate SKU: {candidate_sku}")
            inventory_version = (
                session.selection.inventory_version if session.selection is not None else self._inventory.version
            )
            return build_quote(
                order_id=order.order_id,
                candidate_variant_id=candidate_sku,
                original_value_minor=order.original_value_minor,
                replacement_value_minor=row.price_minor,
                currency=order.currency,
                inventory_version=inventory_version,
                quote_version=quote_version,
                now=now,
            )

    def choose(self, session_id: str, candidate_sku: str, *, quote_version: int = 1) -> Transition:
        with self._lock:
            quote = self.build_quote_for(session_id, candidate_sku, quote_version=quote_version)
            return self.apply_event(session_id, ChooseCandidate(candidate_sku=candidate_sku, quote=quote))

    def reissue_quote(self, session_id: str, *, quote_version: int | None = None) -> Quote:
        """Mint a successor quote without changing session state.

        Used when a quote expired: the state stays ``QUOTE_READY`` but the old quote
        id can no longer authorise anything, so a fresh approval is mandatory.
        """

        with self._lock:
            session = self._require_session(session_id)
            quote = session.quote
            if quote is None:
                raise StoreError("session has no quote to reissue")
            row = self._inventory.get(quote.candidate_variant_id)
            if row is None:
                raise StoreError("candidate no longer exists")
            successor = build_quote(
                order_id=quote.order_id,
                candidate_variant_id=quote.candidate_variant_id,
                original_value_minor=quote.original_value_minor,
                replacement_value_minor=row.price_minor,
                currency=quote.currency,
                inventory_version=self._inventory.version,
                quote_version=quote.quote_version + 1 if quote_version is None else quote_version,
                now=self._clock(),
            )
            updated = replace(session, quote=successor, approval=None, invalidated_quote_work=True)
            self._sessions[session_id] = updated
            self._persist_locked()
            return successor

    # ------------------------------------------------------------------ #
    # Confirmation and commit
    # ------------------------------------------------------------------ #
    def confirm_exchange(self, request: ConfirmRequest) -> ConfirmResult:
        with self._lock:
            self._require_alive()
            now = self._clock()
            session = self._sessions.get(request.session_id)
            if session is None:
                return ConfirmResult(
                    outcome=REFUSED,
                    reason=SESSION_NOT_FOUND,
                    session_state="UNKNOWN",
                    session_version=0,
                )

            # 1. Idempotency is consulted before any state or approval reasoning, so
            #    a duplicate click can never be reinterpreted as a new intent.
            prior = self._idempotency.get(request.idempotency_key)
            if prior is not None:
                return self._replay_locked(session, prior, request)

            # 2. Only an explicit shopper approval is a confirmation.
            action_refusal = _action_refusal(request.action)
            if action_refusal is not None:
                return self._refuse_locked(session, action_refusal)

            # 3. The reducer is the authority for the transition and its guards.
            result = transition(
                session,
                ApproveExchange(
                    quote_id=request.quote_id,
                    quote_version=request.quote_version,
                    origin=request.origin,
                    approved_at=request.approved_at,
                    idempotency_key=request.idempotency_key,
                    shopper_identity=request.shopper_identity,
                    request_token=request.request_token,
                ),
                now=now,
                expected_version=session.version,
                policy=load_policy(),
            )
            if not result.accepted:
                return self._refuse_locked(session, _map_refusal(result), result.refusal)

            approved = result.session
            self._sessions[session.session_id] = approved
            self._persist_locked()
            return self._commit_locked(approved, request, now)

    def _replay_locked(
        self, session: SessionState, prior: Mapping[str, Any], request: ConfirmRequest
    ) -> ConfirmResult:
        if prior.get("digest") != request.payload_digest():
            return ConfirmResult(
                outcome=CONFLICT,
                reason=IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD,
                session_state=session.state,
                session_version=session.version,
                transaction_id=prior.get("transactionId"),
            )

        transaction = self._transactions.get(str(prior.get("transactionId")))
        if transaction is None:
            return ConfirmResult(
                outcome=UNCERTAIN,
                reason=IN_DOUBT,
                session_state=session.state,
                session_version=session.version,
                required_action=RECONCILE_REQUIRED,
            )

        if transaction.status == TXN_COMMITTED:
            # Verification, not a new transaction: move the session to the state
            # that matches the durable record.
            resolved = self._resolve_committed_locked(session, transaction)
            return ConfirmResult(
                outcome=REPLAYED,
                reason=None,
                session_state=resolved.state,
                session_version=resolved.version,
                transaction_id=transaction.transaction_id,
                receipt=transaction.receipt,
            )

        if transaction.status == TXN_ABANDONED:
            return ConfirmResult(
                outcome=KNOWN_FAILURE,
                reason=SIMULATED_STORE_FAILURE,
                session_state=session.state,
                session_version=session.version,
                transaction_id=transaction.transaction_id,
                required_action=REQUIRE_FRESH_QUOTE,
            )

        return ConfirmResult(
            outcome=UNCERTAIN,
            reason=IN_DOUBT,
            session_state=session.state,
            session_version=session.version,
            transaction_id=transaction.transaction_id,
            required_action=RECONCILE_REQUIRED,
        )

    def _commit_locked(self, session: SessionState, request: ConfirmRequest, now: datetime) -> ConfirmResult:
        quote = session.quote
        order = self._require_order(session.order_id)
        if quote is None:  # pragma: no cover - the reducer guarantees a quote
            return self._refuse_locked(session, QUOTE_MISMATCH, "no quote")

        transaction_id = _transaction_id(session.session_id, quote.quote_id, quote.quote_version)
        self._transactions[transaction_id] = Transaction(
            transaction_id=transaction_id,
            session_id=session.session_id,
            order_id=order.order_id,
            status=TXN_IN_FLIGHT,
            idempotency_key=request.idempotency_key,
            payload_digest=request.payload_digest(),
            quote_id=quote.quote_id,
            candidate_sku=quote.candidate_variant_id,
        )
        # Persist the idempotency intent *before* any side effect.
        self._idempotency[request.idempotency_key] = {
            "digest": request.payload_digest(),
            "transactionId": transaction_id,
            "sessionId": session.session_id,
        }
        self._persist_locked()

        fault = self.faults.pop()

        if fault == FaultKind.FAIL_BEFORE_WRITE:
            return self._abandon_locked(session, transaction_id, SIMULATED_STORE_FAILURE)

        row = self._inventory.get(quote.candidate_variant_id)
        check = revalidate(
            quote,
            now=now,
            inventory_version=None if row is None else self._inventory.version,
            price_minor=None if row is None else row.price_minor,
            currency=None if row is None else row.currency,
            stock=None if row is None else row.stock,
        )
        blocking = _blocking_failures(check.failures)
        if blocking:
            reason = blocking[0]
            mapped = {
                "OUT_OF_STOCK": STOCK_CHANGED,
                "UNKNOWN_SKU": UNKNOWN_SKU,
                "PRICE_CHANGED": PRICE_CHANGED,
                "CURRENCY_CHANGED": CURRENCY_CHANGED,
                "EXPIRED": EXPIRED_QUOTE,
            }[reason]
            abandoned = self._abandon_locked(session, transaction_id, mapped)
            return replace(
                abandoned,
                revalidation=check.as_dict(),
                required_action=REQUIRE_FRESH_QUOTE,
            )

        receipt = self._build_receipt(session, quote, order, request, now, transaction_id)

        # Durable write: transaction record, inventory decrement, ledger enqueue.
        # The session's committed-state transition happens after this block, so a
        # crash here leaves COMMIT_PENDING + a COMMITTED transaction: exactly the
        # in-doubt case recovery must reconcile without a second charge.
        self._transactions[transaction_id] = replace(
            self._transactions[transaction_id],
            status=TXN_COMMITTED,
            receipt=receipt,
            committed_at=now,
        )
        self._idempotency[request.idempotency_key]["status"] = TXN_COMMITTED
        self._ledger.record(
            committed_exchange(
                transaction_id=transaction_id,
                session_id=session.session_id,
                order_id=order.order_id,
                original_sku=order.original_sku,
                replacement_sku=quote.candidate_variant_id,
                original_value_minor=quote.original_value_minor,
                replacement_value_minor=quote.replacement_value_minor,
                currency=quote.currency,
                committed_at=now,
                preview_mode=_preview_mode(session),
                policy_version=str(load_policy()["version"]),
                eligibility_status=(
                    session.decision.status if session.decision is not None else "UNKNOWN_REQUIRES_REFUND_PATH"
                ),
            )
        )
        self._inventory.decrement_stock(quote.candidate_variant_id, now)
        self._persist_locked()

        if fault == FaultKind.PERSIST_THEN_CRASH:
            self._dead = True
            raise StoreCrashed(transaction_id)

        if fault == FaultKind.FAIL_AFTER_WRITE:
            # The write is durable but the caller cannot be told so.  Claiming
            # success here would be a fabrication; reconciling by transaction id is
            # the only honest forward path, and it cannot create a second charge.
            parked = transition(
                session,
                CommitUncertain(attempt_id=transaction_id),
                now=self._clock(),
                policy=load_policy(),
            )
            if not parked.accepted:  # pragma: no cover - state is COMMIT_PENDING
                raise StoreError(f"could not park in-doubt commit: {parked.refusal}")
            self._sessions[session.session_id] = parked.session
            self._persist_locked()
            return ConfirmResult(
                outcome=UNCERTAIN,
                reason=IN_DOUBT,
                session_state=parked.session.state,
                session_version=parked.session.version,
                transaction_id=transaction_id,
                receipt=receipt,
                required_action=RECONCILE_REQUIRED,
            )

        committed = self._resolve_committed_locked(session, self._transactions[transaction_id])
        return ConfirmResult(
            outcome=COMMITTED,
            reason=None,
            session_state=committed.state,
            session_version=committed.version,
            transaction_id=transaction_id,
            receipt=receipt,
        )

    def _build_receipt(
        self,
        session: SessionState,
        quote: Quote,
        order: OrderView,
        request: ConfirmRequest,
        now: datetime,
        transaction_id: str,
    ) -> Receipt:
        approval = session.approval
        if approval is None:  # pragma: no cover - guarded by the reducer
            raise StoreError("commit without an approval")
        decision = session.decision
        previews = 0
        if session.preview_result is not None:
            previews = len(session.preview_result.succeeded_candidate_ids)
        retained = min(quote.original_value_minor, quote.replacement_value_minor)
        return Receipt(
            commit_id=transaction_id,
            session_id=session.session_id,
            order_id=order.order_id,
            return_reason_codes=() if decision is None else decision.matched_reasons,
            eligibility_status="UNKNOWN_REQUIRES_REFUND_PATH" if decision is None else decision.status,
            eligibility_rationale="" if decision is None else decision.explanation_code,
            policy_version=str(load_policy()["version"]),
            original_sku=order.original_sku,
            replacement_sku=quote.candidate_variant_id,
            original_value_minor=quote.original_value_minor,
            replacement_value_minor=quote.replacement_value_minor,
            currency=quote.currency,
            stock_checked_at=now,
            stock_check_inventory_version=self._inventory.version,
            candidates_eligible=len(session.candidate_skus()),
            previews_delivered=previews,
            preview_mode=_preview_mode(session),
            approval=ApprovalRecord.from_approval(approval),
            partial_refund_minor=quote.partial_refund_minor,
            additional_charge_minor=quote.additional_charge_minor,
            retained_gmv_minor=retained,
            outcome=OUTCOME_EXCHANGE_COMMITTED,
            committed_at=now,
            return_support_ref=f"host://returns/{order.order_id}",
        )

    def _resolve_committed_locked(self, session: SessionState, transaction: Transaction) -> SessionState:
        """Bring the session to EXCHANGE_COMMITTED for an existing committed txn."""

        if session.state == "EXCHANGE_COMMITTED":
            return session
        if session.state == "COMMIT_PENDING":
            result = transition(
                session,
                CommitSucceeded(
                    commit_id=transaction.transaction_id,
                    transaction_id=transaction.transaction_id,
                    receipt=None if transaction.receipt is None else transaction.receipt.as_dict(),
                ),
                now=self._clock(),
                policy=load_policy(),
            )
        elif session.state == "RECONCILING":
            result = transition(
                session,
                ReconcileCommitted(
                    commit_id=transaction.transaction_id,
                    transaction_id=transaction.transaction_id,
                    receipt=None if transaction.receipt is None else transaction.receipt.as_dict(),
                ),
                now=self._clock(),
                policy=load_policy(),
            )
        else:  # pragma: no cover - defensive
            raise StoreError(f"cannot resolve committed transaction from {session.state}")
        if not result.accepted:
            raise StoreError(f"resolution refused: {result.refusal}")
        self._sessions[session.session_id] = result.session
        self._persist_locked()
        return result.session

    def _abandon_locked(self, session: SessionState, transaction_id: str, reason: str) -> ConfirmResult:
        """Known failure with no side effect: return to DECISION_READY."""

        self._transactions[transaction_id] = replace(
            self._transactions[transaction_id], status=TXN_ABANDONED
        )
        result = transition(
            session,
            CommitFailedKnown(reason=reason),
            now=self._clock(),
            policy=load_policy(),
        )
        if not result.accepted:  # pragma: no cover - state is COMMIT_PENDING
            raise StoreError(f"could not record known failure: {result.refusal}")
        self._sessions[session.session_id] = result.session
        self._persist_locked()
        return ConfirmResult(
            outcome=KNOWN_FAILURE,
            reason=reason,
            session_state=result.session.state,
            session_version=result.session.version,
            transaction_id=transaction_id,
            required_action=REQUIRE_FRESH_QUOTE,
        )

    def _refuse_locked(
        self, session: SessionState, reason: str, detail: str | None = None
    ) -> ConfirmResult:
        return ConfirmResult(
            outcome=REFUSED,
            reason=reason,
            session_state=session.state,
            session_version=session.version,
            required_action=detail,
        )

    # ------------------------------------------------------------------ #
    # Refund
    # ------------------------------------------------------------------ #
    def request_refund(
        self,
        session_id: str,
        *,
        shopper_identity: str,
        request_token: str,
        reason: str = "CONTINUE_REFUND",
    ) -> RefundResult:
        """The shopper's refund route, sharing the exchange's atomic boundary."""

        with self._lock:
            self._require_alive()
            now = self._clock()
            session = self._sessions.get(session_id)
            if session is None:
                return RefundResult(REFUND_REFUSED, SESSION_NOT_FOUND, "UNKNOWN", 0)
            if shopper_identity != session.shopper_identity:
                return RefundResult(REFUND_REFUSED, SHOPPER_MISMATCH, session.state, session.version)
            if request_token != session.request_token:
                return RefundResult(REFUND_REFUSED, REQUEST_TOKEN_MISMATCH, session.state, session.version)

            if session.state == "REFUND_HANDOFF":
                return RefundResult(
                    REFUND_ALREADY_STARTED, None, session.state, session.version
                )
            if session.state == "EXCHANGE_COMMITTED":
                recorded = replace(session, refund_recorded=True, refund_recorded_at=now)
                self._sessions[session_id] = recorded
                self._persist_locked()
                return RefundResult(
                    REFUND_AFTER_COMMIT,
                    "ORDINARY_RETURN_SUPPORT",
                    recorded.state,
                    recorded.version,
                    transaction_id=recorded.transaction_id,
                )

            result = transition(
                session,
                ContinueRefund(trigger=reason),
                now=now,
                expected_version=session.version,
                policy=load_policy(),
            )
            if result.accepted:
                self._sessions[session_id] = result.session
                order = self._require_order(result.session.order_id)
                self._ledger.record(
                    non_committed_outcome(
                        transaction_id=f"refund:{session_id}:{result.session.version}",
                        session_id=session_id,
                        order_id=order.order_id,
                        original_sku=order.original_sku,
                        replacement_sku=None,
                        original_value_minor=order.original_value_minor,
                        replacement_value_minor=0,
                        currency=order.currency,
                        outcome=OUTCOME_REFUND_HANDOFF,
                        committed_at=now,
                        preview_mode=_preview_mode(session),
                        policy_version=str(load_policy()["version"]),
                        eligibility_status=(
                            session.decision.status
                            if session.decision is not None
                            else "UNKNOWN_REQUIRES_REFUND_PATH"
                        ),
                    )
                )
                self._persist_locked()
                return RefundResult(
                    REFUND_HANDOFF_STARTED, None, result.session.state, result.session.version
                )

            if result.refusal == "REFUND_RECORDED_PENDING_COMMIT":
                # Commit already started: record the request, never start a second
                # payment action; reconciliation resolves the single transaction.
                self._sessions[session_id] = result.session
                self._persist_locked()
                return RefundResult(
                    REFUND_RECORDED,
                    "COMMIT_IN_PROGRESS_RECONCILIATION_PENDING",
                    result.session.state,
                    result.session.version,
                )

            return RefundResult(
                REFUND_REFUSED,
                result.refusal or "REFUSED",
                session.state,
                session.version,
            )

    # ------------------------------------------------------------------ #
    # Reconciliation and recovery
    # ------------------------------------------------------------------ #
    def reconcile(self, session_id: str) -> ConfirmResult:
        """Resolve an in-doubt commit using the existing transaction id only."""

        with self._lock:
            self._require_alive()
            session = self._sessions.get(session_id)
            if session is None:
                return ConfirmResult(REFUSED, SESSION_NOT_FOUND, "UNKNOWN", 0)

            if session.state not in ("RECONCILING", "COMMIT_PENDING"):
                return ConfirmResult(
                    outcome=REFUSED,
                    reason=WRONG_STATE,
                    session_state=session.state,
                    session_version=session.version,
                )

            transaction = self._find_transaction_locked(session_id)
            if transaction is not None and transaction.status == TXN_COMMITTED:
                resolved = self._resolve_committed_locked(session, transaction)
                return ConfirmResult(
                    outcome=COMMITTED,
                    reason=None,
                    session_state=resolved.state,
                    session_version=resolved.version,
                    transaction_id=transaction.transaction_id,
                    receipt=transaction.receipt,
                )

            if session.state == "RECONCILING":
                result = transition(
                    session,
                    ReconcileNotCommitted(reason="NO_COMMITTED_TRANSACTION_FOUND"),
                    now=self._clock(),
                    policy=load_policy(),
                )
                if not result.accepted:  # pragma: no cover
                    raise StoreError(f"reconciliation refused: {result.refusal}")
                self._sessions[session_id] = result.session
                self._persist_locked()
                order = self._require_order(session.order_id)
                self._ledger.record(
                    non_committed_outcome(
                        transaction_id=f"uncommitted:{session_id}:{result.session.version}",
                        session_id=session_id,
                        order_id=order.order_id,
                        original_sku=order.original_sku,
                        replacement_sku=None,
                        original_value_minor=order.original_value_minor,
                        replacement_value_minor=0,
                        currency=order.currency,
                        outcome=OUTCOME_RECONCILING,
                        committed_at=self._clock(),
                        preview_mode=_preview_mode(session),
                        policy_version=str(load_policy()["version"]),
                        eligibility_status=(
                            session.decision.status
                            if session.decision is not None
                            else "UNKNOWN_REQUIRES_REFUND_PATH"
                        ),
                    )
                )
                self._persist_locked()
                return ConfirmResult(
                    outcome=KNOWN_FAILURE,
                    reason="NO_COMMITTED_TRANSACTION",
                    session_state=result.session.state,
                    session_version=result.session.version,
                    required_action="ORDINARY_RETURN_SUPPORT",
                )

            result = transition(
                session,
                CommitFailedKnown(reason="RECOVERY_NO_COMMITTED_TRANSACTION"),
                now=self._clock(),
                policy=load_policy(),
            )
            if not result.accepted:  # pragma: no cover
                raise StoreError(f"recovery refusal: {result.refusal}")
            self._sessions[session_id] = result.session
            self._persist_locked()
            return ConfirmResult(
                outcome=KNOWN_FAILURE,
                reason="NO_COMMITTED_TRANSACTION",
                session_state=result.session.state,
                session_version=result.session.version,
                required_action=REQUIRE_FRESH_QUOTE,
            )

    def _find_transaction_locked(self, session_id: str) -> Transaction | None:
        found: Transaction | None = None
        for transaction in self._transactions.values():
            if transaction.session_id != session_id:
                continue
            if transaction.status == TXN_COMMITTED:
                return transaction
            if found is None:
                found = transaction
        return found

    def _recover_locked(self) -> None:
        """Resolve sessions left mid-commit by a previous process.

        A durable committed transaction is never repeated: it is parked for
        reconciliation.  An interrupted attempt with no committed transaction had no
        side effect, so it returns to the decision state for a fresh quote.
        """

        for session_id, session in list(self._sessions.items()):
            if session.state not in ("COMMIT_PENDING", "RECONCILING"):
                continue
            transaction = self._find_transaction_locked(session_id)
            if session.state == "COMMIT_PENDING":
                if transaction is not None and transaction.status == TXN_COMMITTED:
                    result = transition(
                        session,
                        CommitUncertain(attempt_id=transaction.transaction_id),
                        now=self._clock(),
                        policy=load_policy(),
                    )
                else:
                    result = transition(
                        session,
                        CommitFailedKnown(reason="RECOVERY_NO_COMMITTED_TRANSACTION"),
                        now=self._clock(),
                        policy=load_policy(),
                    )
                if result.accepted:
                    self._sessions[session_id] = result.session
        self._persist_locked()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def to_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "formatVersion": 1,
                "sessions": [codec.encode_session(session) for session in self._sessions.values()],
                "orders": [order.as_dict() for order in self._orders.values()],
                "transactions": [transaction.as_dict() for transaction in self._transactions.values()],
                "idempotency": {key: dict(value) for key, value in self._idempotency.items()},
                "inventory": self._inventory.to_state(),
                "ledger": self._ledger.to_state(),
            }

    def _persist_locked(self) -> None:
        if self._store_path is None:
            return
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_state()
        temporary = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._store_path)

    def _load_locked(self) -> None:
        assert self._store_path is not None
        with self._store_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self._sessions = {
            str(item["sessionId"]): codec.decode_session(item) for item in raw.get("sessions", [])
        }
        self._orders = {
            str(item["orderId"]): OrderView.from_dict(item) for item in raw.get("orders", [])
        }
        self._transactions = {
            str(item["transactionId"]): Transaction.from_dict(item)
            for item in raw.get("transactions", [])
        }
        self._idempotency = {str(key): dict(value) for key, value in raw.get("idempotency", {}).items()}
        self._inventory = Inventory.from_state(raw.get("inventory", {}))
        self._ledger = OutcomeLedger.from_state(raw.get("ledger", {}))
        self._recover_locked()

    # ------------------------------------------------------------------ #
    # Internal guards
    # ------------------------------------------------------------------ #
    def _require_alive(self) -> None:
        if self._dead:
            raise StoreError("store is in a simulated crashed state; reopen it from its store path")

    def _require_session(self, session_id: str) -> SessionState:
        session = self._sessions.get(session_id)
        if session is None:
            raise StoreError(f"unknown session: {session_id}")
        return session

    def _require_order(self, order_id: str) -> OrderView:
        order = self._orders.get(order_id)
        if order is None:
            raise StoreError(f"unknown order: {order_id}")
        return order


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #
def _preview_mode(session: SessionState) -> str:
    if session.preview_result is not None:
        return session.preview_result.mode
    if session.preview_plan is not None:
        return session.preview_plan.mode
    return "MOCK"


def _blocking_failures(failures: tuple[str, ...]) -> tuple[str, ...]:
    """Which revalidation failures block a commit.

    An inventory *version* change alone is not blocking when the price, currency
    and stock the shopper approved are still exactly true; the quote promises
    facts, and those facts are re-checked.  Anything else blocks.
    """

    order = ("EXPIRED", "UNKNOWN_SKU", "PRICE_CHANGED", "CURRENCY_CHANGED", "OUT_OF_STOCK")
    return tuple(code for code in order if code in failures)


def _action_refusal(action: str) -> str | None:
    if action == ACTION_APPROVE_EXCHANGE:
        return None
    if action in ("SELECT", "SELECT_CANDIDATE"):
        return SELECTION_IS_NOT_APPROVAL
    if action in ("PREVIEW_COMPLETE", "PREVIEW_SUCCEEDED"):
        return PREVIEW_IS_NOT_APPROVAL
    if action in ("AGENT_RECOMMENDATION", "RECOMMEND", "AUTO_APPROVE"):
        return AGENT_RECOMMENDATION_IS_NOT_APPROVAL
    return UNKNOWN_ACTION_IS_NOT_APPROVAL


def _map_refusal(result: Transition) -> str:
    code = result.refusal or WRONG_STATE
    if code == "REFUND_RECORDED_PENDING_COMMIT":  # pragma: no cover - handled earlier
        return IN_DOUBT_RECONCILE_FIRST
    if code == "TERMINAL_STATE":
        return TERMINAL_STATE
    if code == "QUOTE_EXPIRED":
        return EXPIRED_QUOTE
    if code == "QUOTE_MISMATCH":
        return QUOTE_MISMATCH
    if code == "NOT_SHOPPER_ORIGIN":
        return NOT_SHOPPER_ORIGIN
    if code == "SHOPPER_IDENTITY_MISMATCH":
        return SHOPPER_MISMATCH
    if code == "REQUEST_TOKEN_MISMATCH":
        return REQUEST_TOKEN_MISMATCH
    if code == "WRONG_STATE":
        # Distinguish "you already refunded", "this session is closed" and "the
        # outcome is unknown, reconcile" from a plain wrong-state refusal; the
        # caller needs a different action in each case.
        state = result.session.state
        if state == "REFUND_HANDOFF":
            return REFUND_WON
        if state in ("EXCHANGE_COMMITTED",):
            return TERMINAL_STATE
        if state == "RECONCILING":
            return IN_DOUBT_RECONCILE_FIRST
        return WRONG_STATE
    return code


def _transaction_id(session_id: str, quote_id: str, quote_version: int) -> str:
    payload = f"{session_id}\x1f{quote_id}\x1f{quote_version}"
    return "txn_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def eligible_rescue(decision: EligibilityDecision) -> bool:
    return decision.status == ELIGIBLE_VISUAL_RESCUE and decision.eligible
