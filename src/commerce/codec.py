"""JSON codecs for the offline commerce store.

This is a local persistence format, not a wire protocol: it exists so a simulated
commit survives a process restart (T30 evidence).  It carries no image bytes,
media handles beyond opaque local ids, or provider URLs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from commerce.quote import Quote
from domain.catalog import CandidateSelection, Exclusion
from domain.eligibility import EligibilityDecision
from domain.preview import PreviewPlan, PreviewResult
from domain.ranking import RankedCandidate
from domain.state_machine import Approval, SessionState


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def encode_decision(decision: EligibilityDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "status": decision.status,
        "eligible": decision.eligible,
        "matchedReasons": list(decision.matched_reasons),
        "policyVersion": decision.policy_version,
        "explanationCode": decision.explanation_code,
    }


def decode_decision(raw: Mapping[str, Any] | None) -> EligibilityDecision | None:
    if raw is None:
        return None
    return EligibilityDecision(
        status=str(raw["status"]),
        eligible=bool(raw["eligible"]),
        matched_reasons=tuple(str(item) for item in raw["matchedReasons"]),
        policy_version=str(raw["policyVersion"]),
        explanation_code=str(raw["explanationCode"]),
    )


def _encode_candidate(candidate: RankedCandidate) -> dict[str, Any]:
    return {
        "sku": candidate.sku,
        "merchant": candidate.merchant,
        "category": candidate.category,
        "size": candidate.size,
        "currency": candidate.currency,
        "priceMinor": candidate.price_minor,
        "colorFamily": candidate.color_family,
        "absDeltaMinor": candidate.abs_delta_minor,
        "preferenceMatches": candidate.preference_matches,
        "matchedPreferenceFields": list(candidate.matched_preference_fields),
    }


def _decode_candidate(raw: Mapping[str, Any]) -> RankedCandidate:
    return RankedCandidate(
        sku=str(raw["sku"]),
        merchant=str(raw["merchant"]),
        category=str(raw["category"]),
        size=str(raw["size"]),
        currency=str(raw["currency"]),
        price_minor=int(raw["priceMinor"]),
        color_family=str(raw["colorFamily"]),
        abs_delta_minor=int(raw["absDeltaMinor"]),
        preference_matches=int(raw["preferenceMatches"]),
        matched_preference_fields=tuple(str(item) for item in raw.get("matchedPreferenceFields", [])),
    )


def encode_selection(selection: CandidateSelection | None) -> dict[str, Any] | None:
    if selection is None:
        return None
    return {
        "sessionId": selection.session_id,
        "inventoryVersion": selection.inventory_version,
        "snapshotCapturedAt": selection.snapshot_captured_at,
        "snapshotAgeSeconds": selection.snapshot_age_seconds,
        "inventoryFresh": selection.inventory_fresh,
        "currency": selection.currency,
        "size": selection.size,
        "maxPriceDeltaMinor": selection.max_price_delta_minor,
        "maxCandidates": selection.max_candidates,
        "consideredCount": selection.considered_count,
        "candidates": [_encode_candidate(item) for item in selection.candidates],
        "excluded": [item.as_dict() for item in selection.excluded],
        "truncatedSkus": list(selection.truncated_skus),
    }


def decode_selection(raw: Mapping[str, Any] | None) -> CandidateSelection | None:
    if raw is None:
        return None
    return CandidateSelection(
        session_id=str(raw["sessionId"]),
        inventory_version=str(raw["inventoryVersion"]),
        snapshot_captured_at=str(raw["snapshotCapturedAt"]),
        snapshot_age_seconds=float(raw["snapshotAgeSeconds"]),
        inventory_fresh=bool(raw["inventoryFresh"]),
        currency=str(raw["currency"]),
        size=str(raw["size"]),
        max_price_delta_minor=int(raw["maxPriceDeltaMinor"]),
        max_candidates=int(raw["maxCandidates"]),
        considered_count=int(raw["consideredCount"]),
        candidates=tuple(_decode_candidate(item) for item in raw.get("candidates", [])),
        excluded=tuple(
            Exclusion(
                row_index=int(item["rowIndex"]),
                sku=item.get("sku"),
                reasons=tuple(str(code) for code in item.get("reasons", [])),
                detail=tuple(str(code) for code in item.get("detail", [])),
            )
            for item in raw.get("excluded", [])
        ),
        truncated_skus=tuple(str(item) for item in raw.get("truncatedSkus", [])),
    )


def encode_quote(quote: Quote | None) -> dict[str, Any] | None:
    return None if quote is None else quote.as_dict()


def decode_quote(raw: Mapping[str, Any] | None) -> Quote | None:
    if raw is None:
        return None
    return Quote(
        quote_id=str(raw["quoteId"]),
        order_id=str(raw["orderId"]),
        candidate_variant_id=str(raw["candidateVariantId"]),
        original_value_minor=int(raw["originalValueMinor"]),
        replacement_value_minor=int(raw["replacementValueMinor"]),
        currency=str(raw["currency"]),
        partial_refund_minor=int(raw["partialRefundMinor"]),
        additional_charge_minor=int(raw["additionalChargeMinor"]),
        tax_shipping_adjustment_minor=int(raw["taxShippingAdjustmentMinor"]),
        inventory_version=str(raw["inventoryVersion"]),
        expires_at=_dt(str(raw["expiresAt"])),  # type: ignore[arg-type]
        quote_version=int(raw["quoteVersion"]),
    )


def encode_approval(approval: Approval | None) -> dict[str, Any] | None:
    if approval is None:
        return None
    payload = approval.as_dict()
    payload["requestToken"] = approval.request_token
    return payload


def decode_approval(raw: Mapping[str, Any] | None) -> Approval | None:
    if raw is None:
        return None
    return Approval(
        quote_id=str(raw["quoteId"]),
        quote_version=int(raw["quoteVersion"]),
        origin=str(raw["origin"]),
        approved_at=_dt(str(raw["approvedAt"])),  # type: ignore[arg-type]
        idempotency_key=str(raw["idempotencyKey"]),
        request_token=str(raw["requestToken"]),
    )


def encode_preview_plan(plan: PreviewPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    payload = plan.as_dict()
    return payload


def decode_preview_plan(raw: Mapping[str, Any] | None) -> PreviewPlan | None:
    if raw is None:
        return None
    return PreviewPlan(
        session_id=str(raw["sessionId"]),
        consent_version=str(raw["consentVersion"]),
        candidate_ids=tuple(str(item) for item in raw["candidateIds"]),
        provider_version=str(raw["providerVersion"]),
        estimated_units=int(raw["estimatedUnits"]),
        authorization_ref=None if raw.get("authorizationRef") is None else str(raw["authorizationRef"]),
        deadline=_dt(str(raw["deadline"])),  # type: ignore[arg-type]
        mode=str(raw["mode"]),
    )


def encode_preview_result(result: PreviewResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    payload = result.as_dict()
    payload["mediaHandles"] = list(result.media_handles)
    return payload


def decode_preview_result(raw: Mapping[str, Any] | None) -> PreviewResult | None:
    if raw is None:
        return None
    return PreviewResult(
        session_id=str(raw["sessionId"]),
        mode=str(raw["mode"]),
        status=str(raw["status"]),
        candidate_ids=tuple(str(item) for item in raw["candidateIds"]),
        task_ids=tuple(str(item) for item in raw["taskIds"]),
        created_at=_dt(str(raw["createdAt"])),  # type: ignore[arg-type]
        expires_at=_dt(str(raw["expiresAt"])),  # type: ignore[arg-type]
        media_handles=tuple(
            None if item is None else str(item) for item in raw.get("mediaHandles", [])
        ),
        failure_code=None if raw.get("failureCode") is None else str(raw["failureCode"]),
    )


def encode_session(session: SessionState) -> dict[str, Any]:
    return {
        "sessionId": session.session_id,
        "orderId": session.order_id,
        "shopperIdentity": session.shopper_identity,
        "requestToken": session.request_token,
        "state": session.state,
        "version": session.version,
        "decision": encode_decision(session.decision),
        "consentVersion": session.consent_version,
        "selection": encode_selection(session.selection),
        "previewPlan": encode_preview_plan(session.preview_plan),
        "previewResult": encode_preview_result(session.preview_result),
        "photoVersion": session.photo_version,
        "generated": list(session.generated),
        "quote": encode_quote(session.quote),
        "approval": encode_approval(session.approval),
        "transactionId": session.transaction_id,
        "commitId": session.commit_id,
        "receipt": session.receipt,
        "refundRecorded": session.refund_recorded,
        "refundRecordedAt": _iso(session.refund_recorded_at) if session.refund_recorded_at else None,
        "invalidatedPreviewWork": session.invalidated_preview_work,
        "invalidatedQuoteWork": session.invalidated_quote_work,
        "history": list(session.history),
    }


def decode_session(raw: Mapping[str, Any]) -> SessionState:
    return SessionState(
        session_id=str(raw["sessionId"]),
        order_id=str(raw["orderId"]),
        shopper_identity=str(raw["shopperIdentity"]),
        request_token=str(raw["requestToken"]),
        state=str(raw["state"]),
        version=int(raw["version"]),
        decision=decode_decision(raw.get("decision")),
        consent_version=raw.get("consentVersion"),
        selection=decode_selection(raw.get("selection")),
        preview_plan=decode_preview_plan(raw.get("previewPlan")),
        preview_result=decode_preview_result(raw.get("previewResult")),
        photo_version=raw.get("photoVersion"),
        generated=tuple(str(item) for item in raw.get("generated", [])),
        quote=decode_quote(raw.get("quote")),
        approval=decode_approval(raw.get("approval")),
        transaction_id=raw.get("transactionId"),
        commit_id=raw.get("commitId"),
        receipt=raw.get("receipt"),
        refund_recorded=bool(raw.get("refundRecorded", False)),
        refund_recorded_at=_dt(raw.get("refundRecordedAt")),
        invalidated_preview_work=bool(raw.get("invalidatedPreviewWork", False)),
        invalidated_quote_work=bool(raw.get("invalidatedQuoteWork", False)),
        history=tuple(str(item) for item in raw.get("history", [])),
    )
