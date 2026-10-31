"""Regressions: a naive clock must not crash the consent gate, and expiry still holds."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from adapters.youcam_live import ConsentGrant, FailureCode, AdapterError


def grant(**over):
    base = dict(
        session_id="ses_1",
        generation=1,
        version="2026-10-30.v1",
        granted_at=datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 10, 30, 9, 30, tzinfo=timezone.utc),
    )
    base.update(over)
    return ConsentGrant(**base)


def test_naive_now_against_aware_grant_is_accepted_not_crashed():
    grant().assert_live(datetime(2026, 10, 30, 9, 5))  # no tzinfo, still inside the window


def test_aware_now_against_naive_grant_is_accepted_not_crashed():
    g = grant(granted_at=datetime(2026, 10, 30, 9, 0),
              expires_at=datetime(2026, 10, 30, 9, 30))
    g.assert_live(datetime(2026, 10, 30, 9, 5, tzinfo=timezone.utc))


def test_expiry_still_refuses_when_awareness_differs():
    with pytest.raises(AdapterError) as caught:
        grant().assert_live(datetime(2026, 10, 30, 9, 31))
    assert caught.value.code is FailureCode.CONSENT_REQUIRED


def test_revoked_grant_still_refuses():
    with pytest.raises(AdapterError):
        grant(revoked_at=datetime(2026, 10, 30, 9, 4)).assert_live(datetime(2026, 10, 30, 9, 5))
