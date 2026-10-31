"""Deterministic eligibility rules — `docs/05-decision-contracts.md`, policy v1.0.

Contract summary implemented here
---------------------------------
Input: session ID, policy version, **structured** `reasonCodes` array, and an
`immediateRefund` boolean.  There is deliberately no free-text parameter: prose
cannot enable visual rescue, and no reason code is normalised, trimmed, case
folded or fuzzy matched.

Precedence (from ``contracts/policy.json``, highest first):

    REFUND_REQUESTED
    UNKNOWN_REQUIRES_REFUND_PATH
    INELIGIBLE_DEFECT
    INELIGIBLE_FULFILLMENT_ERROR
    INELIGIBLE_FIT
    INELIGIBLE_MATERIAL
    ELIGIBLE_VISUAL_RESCUE

Output: an :class:`EligibilityDecision` that satisfies
``contracts/eligibility.schema.json`` and always reports
``refundAvailable == True``.  ``eligible`` is true only for
``ELIGIBLE_VISUAL_RESCUE``.

Fail-open rule: anything malformed, empty or unrecognised becomes
``UNKNOWN_REQUIRES_REFUND_PATH`` (the ordinary return route), never a rescue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from specs import policy as load_policy

REFUND_REQUESTED = "REFUND_REQUESTED"
UNKNOWN_REQUIRES_REFUND_PATH = "UNKNOWN_REQUIRES_REFUND_PATH"
INELIGIBLE_DEFECT = "INELIGIBLE_DEFECT"
INELIGIBLE_FULFILLMENT_ERROR = "INELIGIBLE_FULFILLMENT_ERROR"
INELIGIBLE_FIT = "INELIGIBLE_FIT"
INELIGIBLE_MATERIAL = "INELIGIBLE_MATERIAL"
ELIGIBLE_VISUAL_RESCUE = "ELIGIBLE_VISUAL_RESCUE"

ALL_STATUSES: tuple[str, ...] = (
    REFUND_REQUESTED,
    UNKNOWN_REQUIRES_REFUND_PATH,
    INELIGIBLE_DEFECT,
    INELIGIBLE_FULFILLMENT_ERROR,
    INELIGIBLE_FIT,
    INELIGIBLE_MATERIAL,
    ELIGIBLE_VISUAL_RESCUE,
)

# Explanation codes are stable strings: a UI translates them, a log stores them,
# and no user-facing claim is generated from them.
EXPLAIN_IMMEDIATE_REFUND = "IMMEDIATE_REFUND_REQUESTED"
EXPLAIN_REFUND_CODE = "REFUND_REQUESTED_BY_SHOPPER"
EXPLAIN_MISSING_REASONS = "UNKNOWN_MISSING_REASONS"
EXPLAIN_MALFORMED_REASONS = "UNKNOWN_MALFORMED_REASON_INPUT"
EXPLAIN_EMPTY_REASONS = "UNKNOWN_EMPTY_REASONS"
EXPLAIN_MALFORMED_CODE = "UNKNOWN_MALFORMED_REASON_CODE"
EXPLAIN_UNRECOGNIZED_CODE = "UNKNOWN_UNRECOGNIZED_REASON"
EXPLAIN_MALFORMED_FLAG = "UNKNOWN_MALFORMED_REFUND_FLAG"

_STATUS_EXPLANATION = {
    INELIGIBLE_DEFECT: "INELIGIBLE_DEFECT_MATCHED",
    INELIGIBLE_FULFILLMENT_ERROR: "INELIGIBLE_FULFILLMENT_ERROR_MATCHED",
    INELIGIBLE_FIT: "INELIGIBLE_FIT_MATCHED",
    INELIGIBLE_MATERIAL: "INELIGIBLE_MATERIAL_MATCHED",
    ELIGIBLE_VISUAL_RESCUE: "ELIGIBLE_VISUAL_RESCUE_MATCHED",
}

# Canonical order in which malformed input causes are reported.  Only the first
# cause is surfaced, so the explanation code is deterministic when several things
# are wrong at once.
_CAUSE_ORDER: tuple[str, ...] = (
    EXPLAIN_MISSING_REASONS,
    EXPLAIN_MALFORMED_REASONS,
    EXPLAIN_EMPTY_REASONS,
    EXPLAIN_MALFORMED_CODE,
    EXPLAIN_UNRECOGNIZED_CODE,
    EXPLAIN_MALFORMED_FLAG,
)


class EligibilityError(ValueError):
    """Base class for eligibility contract violations."""


class PolicyVersionMismatch(EligibilityError):
    """The caller asked for a policy version this build does not implement."""


class InvalidSessionIdentity(EligibilityError):
    """sessionId is not a usable pseudonymous identifier."""


class ContractError(EligibilityError):
    """The policy table itself is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """Typed decision output; shape fixed by `contracts/eligibility.schema.json`."""

    status: str
    eligible: bool
    matched_reasons: tuple[str, ...]
    policy_version: str
    explanation_code: str

    def __post_init__(self) -> None:
        if self.status not in ALL_STATUSES:
            raise ContractError(f"unknown status {self.status!r}")
        if not isinstance(self.matched_reasons, tuple) or not all(
            isinstance(code, str) for code in self.matched_reasons
        ):
            raise ContractError("matched_reasons must be a tuple of strings")
        if not isinstance(self.explanation_code, str) or not self.explanation_code:
            raise ContractError("explanation_code must be a non-empty string")
        if self.eligible != (self.status == ELIGIBLE_VISUAL_RESCUE):
            raise ContractError(
                "eligible must be true exactly when status is ELIGIBLE_VISUAL_RESCUE"
            )

    @property
    def refund_available(self) -> bool:
        """Frozen product rule: the refund path is always available.

        Exposed as a property, not a field, so no code path can produce a
        decision that reports the refund route as unavailable.
        """

        return True

    @property
    def preview_dispatch_permitted(self) -> bool:
        """Only an eligible rescue may ever dispatch visual work."""

        return self.eligible

    def as_dict(self) -> dict[str, Any]:
        """Wire form, key-for-key compatible with the normative schema."""

        return {
            "status": self.status,
            "eligible": self.eligible,
            "matchedReasons": list(self.matched_reasons),
            "policyVersion": self.policy_version,
            "explanationCode": self.explanation_code,
            "refundAvailable": self.refund_available,
        }


def _validate_policy(policy: Mapping[str, Any]) -> None:
    reason_map = policy.get("reasonMap")
    precedence = policy.get("precedence")
    if not isinstance(reason_map, Mapping) or not reason_map:
        raise ContractError("policy.reasonMap missing or empty")
    if not isinstance(precedence, (list, tuple)) or not precedence:
        raise ContractError("policy.precedence missing or empty")
    mapped = set(reason_map.values())
    for status in mapped:
        if status not in ALL_STATUSES:
            raise ContractError(f"reasonMap maps to status outside the contract: {status}")
    for status in mapped:
        if status not in precedence:
            raise ContractError(f"status {status} is absent from precedence")
    for status in precedence:
        if not isinstance(status, str) or status not in ALL_STATUSES:
            raise ContractError(f"precedence contains unknown status {status!r}")
    if len(set(precedence)) != len(precedence):
        raise ContractError("precedence contains duplicates")


def _reason_fault(reason_codes: Any, reason_map: Mapping[str, Any]) -> str | None:
    """Return the single malformed-input explanation code, or None if well formed.

    Strict on purpose: no trimming, no case folding, no partial matching, and a
    single unknown or non-string element poisons the whole submission.  A shopper
    who cannot express a recognised reason gets the ordinary return route.
    """

    if reason_codes is None:
        return EXPLAIN_MISSING_REASONS
    # A JSON array and its immutable tuple equivalent are both accepted.  A bare
    # string is not: treating characters as codes would invent structure that the
    # shopper never supplied.
    if not isinstance(reason_codes, (list, tuple)):
        return EXPLAIN_MALFORMED_REASONS
    if not reason_codes:
        return EXPLAIN_EMPTY_REASONS
    for code in reason_codes:
        if not isinstance(code, str) or not code or code != code.strip():
            return EXPLAIN_MALFORMED_CODE
    for code in reason_codes:
        if code not in reason_map:
            return EXPLAIN_UNRECOGNIZED_CODE
    return None


def _unknown(explanation_code: str, policy_version: str) -> EligibilityDecision:
    return EligibilityDecision(
        status=UNKNOWN_REQUIRES_REFUND_PATH,
        eligible=False,
        matched_reasons=(),
        policy_version=policy_version,
        explanation_code=explanation_code,
    )


def decide(
    session_id: str,
    policy_version: str,
    reason_codes: Any,
    immediate_refund: Any,
    *,
    policy: Mapping[str, Any] | None = None,
) -> EligibilityDecision:
    """Classify structured return reasons into one decision status.

    ``reason_codes`` is typed ``Any`` only so that malformed caller input can be
    judged rather than crashing; the contract type is ``list[str]``.
    """

    if not isinstance(session_id, str) or not session_id.strip():
        raise InvalidSessionIdentity("sessionId must be a non-empty pseudonymous string")

    effective = load_policy() if policy is None else policy
    _validate_policy(effective)

    declared_version = effective.get("version")
    if policy_version != declared_version:
        raise PolicyVersionMismatch(
            f"policy version {policy_version!r} is not implemented; contract is {declared_version!r}"
        )
    resolved_version = str(declared_version)

    reason_map: Mapping[str, Any] = effective["reasonMap"]
    precedence: Sequence[str] = tuple(effective["precedence"])
    precedence_index = {status: index for index, status in enumerate(precedence)}

    faults: list[str] = []
    fault = _reason_fault(reason_codes, reason_map)
    if fault is not None:
        faults.append(fault)
    flag_is_valid = isinstance(immediate_refund, bool)
    if not flag_is_valid:
        faults.append(EXPLAIN_MALFORMED_FLAG)

    if faults:
        # Deterministic single cause, chosen by _CAUSE_ORDER.
        ordered = [code for code in _CAUSE_ORDER if code in faults]
        return _unknown(ordered[0], resolved_version)

    codes: list[str] = list(reason_codes)
    matched = tuple(sorted(set(codes)))
    immediate: bool = immediate_refund

    if immediate:
        return EligibilityDecision(
            status=REFUND_REQUESTED,
            eligible=False,
            matched_reasons=matched,
            policy_version=resolved_version,
            explanation_code=EXPLAIN_IMMEDIATE_REFUND,
        )

    candidates: dict[str, str] = {}
    for code in codes:
        status = reason_map[code]
        if status not in precedence_index:
            raise ContractError(f"policy status {status!r} has no precedence entry")
        candidates[code] = status

    winning_status = min(
        (status for status in candidates.values()), key=lambda status: precedence_index[status]
    )

    if winning_status == REFUND_REQUESTED:
        explanation = EXPLAIN_REFUND_CODE
    else:
        explanation = _STATUS_EXPLANATION.get(winning_status)
        if explanation is None:  # pragma: no cover - guarded by _validate_policy
            raise ContractError(f"no explanation code for status {winning_status!r}")

    return EligibilityDecision(
        status=winning_status,
        eligible=winning_status == ELIGIBLE_VISUAL_RESCUE,
        matched_reasons=matched,
        policy_version=resolved_version,
        explanation_code=explanation,
    )
