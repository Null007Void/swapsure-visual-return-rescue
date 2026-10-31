"""Table-driven check of all 23 spec-oracle eligibility cases (E01..E23).

The fixture is the oracle: every row is executed, and each decision is validated
against the normative schema as well as the expected status.  A row that is not
executed would be silently missing evidence, so the count is asserted too.
"""

from __future__ import annotations

import pytest

from domain.eligibility import ELIGIBLE_VISUAL_RESCUE, decide
from harness.schema import validate_decision_json
from specs import eligibility_cases

CASES = eligibility_cases()
CASE_IDS = [str(case["id"]) for case in CASES]


def test_fixture_has_all_23_cases():
    assert len(CASES) == 23
    assert CASE_IDS == [f"E{index:02d}" for index in range(1, 24)]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_fixture_case_status(case):
    decision = decide(
        session_id="ses-fixture",
        policy_version="1.0",
        reason_codes=case["reasonCodes"],
        immediate_refund=case["immediateRefund"],
    )
    assert decision.status == case["expected"]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_fixture_case_matches_schema(case):
    decision = decide(
        session_id="ses-fixture",
        policy_version="1.0",
        reason_codes=case["reasonCodes"],
        immediate_refund=case["immediateRefund"],
    )
    payload = decision.as_dict()
    validate_decision_json(payload)
    assert payload["refundAvailable"] is True
    assert payload["policyVersion"] == "1.0"
    assert payload["eligible"] is (decision.status == ELIGIBLE_VISUAL_RESCUE)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_fixture_case_preview_dispatch_gate(case):
    """No visual work may be permitted unless the decision is an eligible rescue."""

    decision = decide(
        session_id="ses-fixture",
        policy_version="1.0",
        reason_codes=case["reasonCodes"],
        immediate_refund=case["immediateRefund"],
    )
    assert decision.preview_dispatch_permitted is (case["expected"] == ELIGIBLE_VISUAL_RESCUE)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_fixture_case_explanation_code_is_present(case):
    decision = decide(
        session_id="ses-fixture",
        policy_version="1.0",
        reason_codes=case["reasonCodes"],
        immediate_refund=case["immediateRefund"],
    )
    assert decision.explanation_code
    assert decision.explanation_code.isupper() or decision.explanation_code.replace("_", "").isalnum()
