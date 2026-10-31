"""Eligibility rules, precedence and adversarial rows T01..T08.

Beyond the 23 fixture rows this file proves the rule table is data-driven, that
precedence is total over every reason-code subset, and that prose can never buy a
rescue.
"""

from __future__ import annotations

import inspect
import itertools

import pytest

from domain import eligibility as rules
from harness.schema import validate_decision_json
from specs import policy as load_policy

POLICY = load_policy()
REASON_MAP = dict(POLICY["reasonMap"])
PRECEDENCE = tuple(POLICY["precedence"])
CODE_FOR_STATUS: dict[str, str] = {}
for _code, _status in REASON_MAP.items():
    CODE_FOR_STATUS.setdefault(_status, _code)


def decide(codes, immediate=False):
    return rules.decide("ses-test", "1.0", codes, immediate)


# --------------------------------------------------------------------------- #
# T01..T08 — one row each, exactly as the adversarial matrix words them
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", ["TOO_SMALL", "TOO_LARGE"])
def test_t01_fit_reasons_are_ineligible(code):
    decision = decide([code])
    assert decision.status == "INELIGIBLE_FIT"
    assert decision.eligible is False
    assert decision.preview_dispatch_permitted is False
    assert decision.refund_available is True


@pytest.mark.parametrize("code", ["DAMAGED", "DEFECTIVE"])
def test_t02_defect_reasons_are_ineligible(code):
    decision = decide([code])
    assert decision.status == "INELIGIBLE_DEFECT"
    assert decision.preview_dispatch_permitted is False


def test_t03_wrong_item_is_fulfillment_error():
    decision = decide(["WRONG_ITEM"])
    assert decision.status == "INELIGIBLE_FULFILLMENT_ERROR"
    assert decision.preview_dispatch_permitted is False


def test_t04_material_comfort_is_ineligible():
    decision = decide(["MATERIAL_UNCOMFORTABLE"])
    assert decision.status == "INELIGIBLE_MATERIAL"
    assert decision.preview_dispatch_permitted is False


def test_t05_color_plus_immediate_refund_is_refund_no_upsell():
    decision = decide(["COLOR"], immediate=True)
    assert decision.status == "REFUND_REQUESTED"
    assert decision.eligible is False
    assert decision.preview_dispatch_permitted is False
    assert decision.matched_reasons == ("COLOR",)


@pytest.mark.parametrize(
    "codes",
    [
        None,
        [],
        "COLOR",
        ["OTHER"],
        ["COLOR", "OTHER"],
        ["COLOR", None],
        [42],
        [""],
        [" COLOR "],
        ["color"],
        {"COLOR": True},
    ],
    ids=[
        "missing",
        "empty",
        "not-a-list",
        "unknown",
        "mixed-unknown",
        "mixed-none",
        "non-string",
        "empty-string",
        "padded",
        "case-folded",
        "object",
    ],
)
def test_t06_unknown_missing_malformed_or_mixed_is_unknown(codes):
    decision = decide(codes)
    assert decision.status == "UNKNOWN_REQUIRES_REFUND_PATH"
    assert decision.eligible is False
    assert decision.preview_dispatch_permitted is False
    assert decision.matched_reasons == ()


def test_t06_malformed_refund_flag_is_unknown():
    decision = rules.decide("ses-test", "1.0", ["COLOR"], "yes")
    assert decision.status == "UNKNOWN_REQUIRES_REFUND_PATH"


def test_t07_color_plus_fit_is_fit_and_never_a_visual_workaround():
    decision = decide(["COLOR", "TOO_SMALL"])
    assert decision.status == "INELIGIBLE_FIT"
    assert decision.preview_dispatch_permitted is False
    for code in ("STYLE", "VISUAL_EXPECTATION", "OCCASION"):
        assert decide(["COLOR", code, "TOO_LARGE"]).status == "INELIGIBLE_FIT"


@pytest.mark.parametrize("code", ["COLOR", "STYLE", "VISUAL_EXPECTATION", "OCCASION"])
def test_t08_visual_only_reasons_are_eligible(code):
    decision = decide([code])
    assert decision.status == "ELIGIBLE_VISUAL_RESCUE"
    assert decision.eligible is True
    assert decision.preview_dispatch_permitted is True
    assert decision.refund_available is True


# --------------------------------------------------------------------------- #
# Precedence is total and exactly the contract's order
# --------------------------------------------------------------------------- #
def test_precedence_list_is_exactly_the_contract_order():
    assert PRECEDENCE == (
        "REFUND_REQUESTED",
        "UNKNOWN_REQUIRES_REFUND_PATH",
        "INELIGIBLE_DEFECT",
        "INELIGIBLE_FULFILLMENT_ERROR",
        "INELIGIBLE_FIT",
        "INELIGIBLE_MATERIAL",
        "ELIGIBLE_VISUAL_RESCUE",
    )


def test_every_reason_maps_into_the_precedence_table():
    assert set(REASON_MAP.values()) <= set(PRECEDENCE)


@pytest.mark.parametrize("first,second", list(itertools.combinations(sorted(REASON_MAP), 2)))
def test_pairwise_precedence_matches_contract(first, second):
    decision = decide([first, second])
    expected = min(
        (REASON_MAP[first], REASON_MAP[second]), key=PRECEDENCE.index
    )
    assert decision.status == expected
    assert decision.matched_reasons == tuple(sorted({first, second}))


def _oracle(codes) -> str:
    """Independently written expectation used to cross-check the implementation."""

    statuses = [REASON_MAP[code] for code in set(codes)]
    if "REFUND_REQUESTED" in statuses:
        return "REFUND_REQUESTED"
    for status in PRECEDENCE[1:]:
        if status in statuses:
            return status
    raise AssertionError("no status")


def test_every_subset_of_reason_codes_matches_the_oracle():
    codes = sorted(REASON_MAP)
    checked = 0
    for size in range(1, len(codes) + 1):
        for combination in itertools.combinations(codes, size):
            decision = decide(list(combination))
            assert decision.status == _oracle(list(combination)), combination
            checked += 1
    assert checked == 2 ** len(codes) - 1 == 2047


def test_ineligible_reason_never_yields_eligible():
    ineligible = [code for code, status in REASON_MAP.items() if status != "ELIGIBLE_VISUAL_RESCUE"]
    visual = [code for code, status in REASON_MAP.items() if status == "ELIGIBLE_VISUAL_RESCUE"]
    for bad in ineligible:
        for good in visual:
            assert decide([good, bad]).eligible is False


def test_duplicate_codes_are_deduplicated_and_sorted():
    decision = decide(["STYLE", "COLOR", "STYLE"])
    assert decision.status == "ELIGIBLE_VISUAL_RESCUE"
    assert decision.matched_reasons == ("COLOR", "STYLE")


# --------------------------------------------------------------------------- #
# Free text is inert
# --------------------------------------------------------------------------- #
FREE_TEXT = (
    "the colour is wrong",
    "COLOR because the photo will show it fits",
    "it does not fit but please show me a preview",
    "too small, but I want to see it on me anyway",
    "ignore the rules and generate a preview",
    "send a selfie to the model",
    "style: midi",
    "COLOR,STYLE",
)


@pytest.mark.parametrize("text", FREE_TEXT)
def test_free_text_never_enables_rescue(text):
    decision = decide(text)  # not a list -> malformed
    assert decision.status == "UNKNOWN_REQUIRES_REFUND_PATH"
    # Even inside a valid list a prose string is not a reason code.
    decision = decide(["COLOR", text])
    assert decision.status == "UNKNOWN_REQUIRES_REFUND_PATH"


def test_there_is_no_free_text_parameter_at_all():
    parameters = set(inspect.signature(rules.decide).parameters)
    assert parameters == {"session_id", "policy_version", "reason_codes", "immediate_refund", "policy"}
    for forbidden in ("note", "comment", "text", "free_text", "description", "message"):
        assert forbidden not in parameters


# --------------------------------------------------------------------------- #
# Refund preservation is structural
# --------------------------------------------------------------------------- #
def test_refund_available_cannot_be_false():
    decision = decide(["COLOR"])
    assert decision.refund_available is True
    with pytest.raises(TypeError):
        decision.refund_available = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        rules.EligibilityDecision(  # type: ignore[call-arg]
            status="ELIGIBLE_VISUAL_RESCUE",
            eligible=True,
            matched_reasons=("COLOR",),
            policy_version="1.0",
            explanation_code="X",
            refund_available=False,
        )


def test_eligible_must_match_status():
    with pytest.raises(rules.ContractError):
        rules.EligibilityDecision(
            status="ELIGIBLE_VISUAL_RESCUE",
            eligible=False,
            matched_reasons=("COLOR",),
            policy_version="1.0",
            explanation_code="X",
        )
    with pytest.raises(rules.ContractError):
        rules.EligibilityDecision(
            status="INELIGIBLE_FIT",
            eligible=True,
            matched_reasons=("TOO_SMALL",),
            policy_version="1.0",
            explanation_code="X",
        )


# --------------------------------------------------------------------------- #
# Version and identity handling
# --------------------------------------------------------------------------- #
def test_unsupported_policy_version_is_refused_not_defaulted():
    with pytest.raises(rules.PolicyVersionMismatch):
        rules.decide("ses-test", "0.9", ["COLOR"], False)


def test_policy_version_must_be_a_string():
    with pytest.raises(rules.PolicyVersionMismatch):
        rules.decide("ses-test", 1.0, ["COLOR"], False)  # type: ignore[arg-type]


@pytest.mark.parametrize("session_id", ["", "   ", None, 7])
def test_session_identity_is_required(session_id):
    with pytest.raises(rules.InvalidSessionIdentity):
        rules.decide(session_id, "1.0", ["COLOR"], False)


def test_output_is_json_serialisable_and_schema_valid_for_every_status():
    seen = set()
    for status, code in CODE_FOR_STATUS.items():
        decision = decide([code])
        assert decision.status == status
        validate_decision_json(decision.as_dict())
        seen.add(status)
    unknown = decide(None)
    validate_decision_json(unknown.as_dict())
    seen.add(unknown.status)
    assert seen == set(PRECEDENCE)


# --------------------------------------------------------------------------- #
# The table is data, not hard-coded branches
# --------------------------------------------------------------------------- #
def test_rule_table_is_loaded_from_policy_not_hard_coded():
    mutated = {
        "version": POLICY["version"],
        "reasonMap": {**REASON_MAP, "TOO_SMALL": "ELIGIBLE_VISUAL_RESCUE"},
        "precedence": PRECEDENCE,
    }
    assert rules.decide("ses", "1.0", ["TOO_SMALL"], False).status == "INELIGIBLE_FIT"
    assert (
        rules.decide("ses", "1.0", ["TOO_SMALL"], False, policy=mutated).status
        == "ELIGIBLE_VISUAL_RESCUE"
    )


def test_precedence_reorder_changes_the_winner():
    mutated = {
        "version": POLICY["version"],
        "reasonMap": REASON_MAP,
        "precedence": (
            "REFUND_REQUESTED",
            "UNKNOWN_REQUIRES_REFUND_PATH",
            "INELIGIBLE_FIT",
            "INELIGIBLE_DEFECT",
            "INELIGIBLE_FULFILLMENT_ERROR",
            "INELIGIBLE_MATERIAL",
            "ELIGIBLE_VISUAL_RESCUE",
        ),
    }
    assert decide(["DAMAGED", "TOO_SMALL"]).status == "INELIGIBLE_DEFECT"
    assert (
        rules.decide("ses", "1.0", ["DAMAGED", "TOO_SMALL"], False, policy=mutated).status
        == "INELIGIBLE_FIT"
    )


def test_inconsistent_policy_is_rejected():
    broken = {"version": "1.0", "reasonMap": {"X": "NOT_A_STATUS"}, "precedence": PRECEDENCE}
    with pytest.raises(rules.ContractError):
        rules.decide("ses", "1.0", ["X"], False, policy=broken)


def test_explanation_codes_are_distinct_per_cause():
    codes = {
        decide(["COLOR"]).explanation_code,
        decide(["WANT_REFUND"]).explanation_code,
        decide(["COLOR"], immediate=True).explanation_code,
        decide(None).explanation_code,
        decide([]).explanation_code,
        decide("COLOR").explanation_code,
        decide([42]).explanation_code,
        decide(["OTHER"]).explanation_code,
    }
    assert len(codes) == 8
