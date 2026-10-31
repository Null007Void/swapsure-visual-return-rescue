"""A budget refusal must not be described as the provider returning nothing."""
from __future__ import annotations

import pytest

from ui.views import _NO_IMAGE_REASONS, _no_image_reason

REFUSAL_CODES = ("BUDGET_EXHAUSTED", "RESERVATION_EXHAUSTED", "CONSENT_REQUIRED",
                 "CONSENT_EXPIRED", "DEADLINE_EXCEEDED")


@pytest.mark.parametrize("code", REFUSAL_CODES)
def test_our_own_refusals_never_blame_the_provider(code):
    reason = _no_image_reason(code)
    assert "provider returned none" not in reason
    assert reason in _NO_IMAGE_REASONS[code]


def test_budget_refusal_names_the_allowance():
    assert "allowance" in _no_image_reason("BUDGET_EXHAUSTED").lower()
    assert "allowance" in _no_image_reason("RESERVATION_EXHAUSTED").lower()


def test_unknown_code_still_falls_back_to_a_neutral_sentence():
    assert _no_image_reason("SOMETHING_NEW") == "the provider returned none for it"
    assert _no_image_reason(None) == "the provider returned none for it"
