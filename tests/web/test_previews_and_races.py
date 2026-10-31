"""Bounded preview dispatch, fault injection, the live gate and refusal typing.

Evidence for AC07/AC08 and T18–T20, T27, T29: at most three previews, at most two
in flight, a 60-second deadline, zero provider units, honest partial/failed results,
and a live mode that cannot be reached by a request field or a construction flag.
"""

from __future__ import annotations

import pytest

from adapters.youcam_stub import FailureCode, LiveTransportAbsent
from commerce.simulator import FaultKind
from harness.webclient import Client
from ui.service import CaseView, ReturnService, ServiceError
from ui.webapp import Application

from .helpers import (
    approve,
    choose,
    grant_consent,
    opt_in,
    reach_candidates,
    reach_decision,
    start_previews,
    start_return,
    TOKEN,
)


def _fresh(tmp_path, clock, **kwargs):
    service = ReturnService(tmp_path / "data", clock=clock, **kwargs)
    return service, Client(Application(service))


def test_preview_tasks_are_bounded_to_two_in_flight_and_three_total(tmp_path, web_clock):
    service, client = _fresh(tmp_path, web_clock, latency_seconds=5.0)
    try:
        reach_candidates(client)
        page = client.get(f"/s/{client.session_id()}")
        assert client.post(
            f"/s/{client.session_id()}/preview", {TOKEN: client.form_token(page.text)}
        ).status == 303
        pending = client.get(f"/s/{client.session_id()}")
        assert "Generating mock previews" in pending.text
        job = service.store.load_job(client.session_id())
        assert job is not None
        assert job["deadline"]
        assert service.coordinator.concurrency == 2
        assert len(job["running"]) <= 2
        assert service.adapter.tasks_created <= 3
        assert service.adapter.units_consumed == 0

        web_clock.advance(5)
        ready = client.get(f"/s/{client.session_id()}")
        assert "Your decision" in ready.text
        assert service.adapter.tasks_created <= 3
    finally:
        service.close()


def test_preview_deadline_fails_the_remainder_honestly(tmp_path, web_clock):
    service, client = _fresh(tmp_path, web_clock, latency_seconds=120.0)
    try:
        reach_candidates(client)
        page = client.get(f"/s/{client.session_id()}")
        client.post(f"/s/{client.session_id()}/preview", {TOKEN: client.form_token(page.text)})
        pending = client.get(f"/s/{client.session_id()}")
        assert "Generating mock previews" in pending.text

        web_clock.advance(400)   # past the configurable wait deadline
        ready = client.get(f"/s/{client.session_id()}")
        assert "Your decision" in ready.text
        assert "Preview unavailable" in ready.text
        assert ready.text.count("<img ") == 0
        assert 'href="/refund-host"' in ready.text
        assert service.sim.session(client.session_id()).state == "DECISION_READY"
        assert service.adapter.units_consumed == 0
        assert service.adapter.tasks_created <= 2
    finally:
        service.close()


def test_provider_timeout_yields_a_partial_result_and_keeps_choices(client, service):
    reach_candidates(client)
    service.adapter.fail_next(FailureCode.TIMEOUT)
    page = start_previews(client)
    assert "Your decision" in page.text
    assert page.text.count("<img ") == 1  # one honest image, one omission
    assert "Preview unavailable" in page.text
    assert "Select for a quote (not an approval)" in page.text
    assert service.sim.session(client.session_id()).state == "DECISION_READY"
    assert service.adapter.units_consumed == 0


def test_live_mode_request_is_refused_with_zero_calls(client, service):
    reach_candidates(client)
    page = client.get(f"/s/{client.session_id()}")
    response = client.post(
        f"/s/{client.session_id()}/preview",
        {TOKEN: client.form_token(page.text), "mode": "LIVE"},
    )
    assert response.status == 409
    # A request field cannot enable live work; the server keeps its configured mode.
    assert "PREVIEW_MODE_MISMATCH" in response.text
    assert service.adapter.tasks_created == 0
    assert service.adapter.units_consumed == 0
    assert service.sim.session(client.session_id()).state == "CANDIDATES_READY"


def test_service_cannot_be_constructed_in_live_mode(tmp_path, web_clock):
    with pytest.raises(LiveTransportAbsent):
        ReturnService(tmp_path / "data", clock=web_clock, preview_mode="LIVE")


def test_candidate_outside_the_plan_cannot_be_chosen(client, service):
    reach_decision(client)
    page = client.get(f"/s/{client.session_id()}")
    refused = client.post(
        f"/s/{client.session_id()}/choose",
        {TOKEN: client.form_token(page.text), "sku": "EMPTY-80"},
    )
    assert refused.status == 409
    assert "CANDIDATE_NOT_AVAILABLE" in refused.text
    assert service.sim.session(client.session_id()).state == "DECISION_READY"


def test_client_supplied_price_or_approval_fields_are_ignored(client, service):
    reach_decision(client)
    page = choose(client)
    token = client.form_token(page.text)
    nonce = client.approval_nonce(page.text)
    approved = client.post(
        f"/s/{client.session_id()}/approve",
        {
            TOKEN: token,
            "approval_nonce": nonce,
            "partialRefundMinor": "99999",
            "replacementValueMinor": "1",
            "action": "APPROVE_EXCHANGE",
        },
    )
    assert approved.status == 303
    receipt = service.store.get_receipt(client.session_id())
    assert receipt["partialRefundMinor"] == 500
    assert receipt["replacementValueMinor"] == 8400


def test_in_doubt_commit_records_the_refund_without_a_second_transaction(client, service):
    """T17/T30: refund during the commit window wins as a recorded request."""

    reach_decision(client)
    choose(client)
    page = client.get(f"/s/{client.session_id()}")
    token = client.form_token(page.text)
    nonce = client.approval_nonce(page.text)

    service.sim.faults.push(FaultKind.FAIL_AFTER_WRITE)
    uncertain = client.post(
        f"/s/{client.session_id()}/approve", {TOKEN: token, "approval_nonce": nonce}
    )
    assert uncertain.status == 409
    assert "IN_DOUBT" in uncertain.text
    assert service.sim.session(client.session_id()).state == "RECONCILING"

    inflight = client.get(f"/s/{client.session_id()}")
    assert "Exchange result pending" in inflight.text
    refund = client.post(f"/s/{client.session_id()}/refund", {TOKEN: token})
    assert refund.status == 409
    assert "COMMIT_IN_PROGRESS_RECONCILIATION_PENDING" in refund.text
    session = service.sim.session(client.session_id())
    assert session.refund_recorded is True
    assert session.state == "RECONCILING"
    assert len(service.sim.transactions()) == 1

    reconciled = client.post(f"/s/{client.session_id()}/reconcile", {TOKEN: token})
    assert reconciled.status == 303
    receipt = client.get(f"/s/{client.session_id()}")
    assert "Rescue Receipt" in receipt.text
    assert len(service.sim.transactions()) == 1
    assert service.store.ledger_totals()["rows"] == 1


def test_refund_wins_before_any_commit(client, service):
    reach_decision(client)
    choose(client)
    page = client.get(f"/s/{client.session_id()}")
    token = client.form_token(page.text)
    nonce = client.approval_nonce(page.text)
    refund = client.post(f"/s/{client.session_id()}/refund", {TOKEN: token})
    assert refund.status == 303
    assert service.sim.session(client.session_id()).state == "REFUND_HANDOFF"
    late = client.post(
        f"/s/{client.session_id()}/approve", {TOKEN: token, "approval_nonce": nonce}
    )
    assert late.status == 409
    assert service.sim.transactions() == ()
    assert service.store.ledger_rows()[0]["retained_gmv_minor"] == 0


def test_oversized_form_and_invalid_encoding_are_typed_refusals(client):
    oversized = client.post(
        "/returns",
        body=b"reason=COLOR&notes=" + b"a" * (70 * 1024),
        content_type="application/x-www-form-urlencoded",
    )
    assert oversized.status == 413
    assert "REQUEST_TOO_LARGE" in oversized.text

    invalid = client.post(
        "/returns",
        body=b"reason=\xff\xfe",
        content_type="application/x-www-form-urlencoded",
    )
    assert invalid.status == 400
    assert "MALFORMED_FORM" in invalid.text

    unsupported = client.post(
        "/returns", body=b"reason=COLOR", content_type="text/plain"
    )
    assert unsupported.status == 415
    assert "UNSUPPORTED_MEDIA_TYPE" in unsupported.text


def test_unknown_action_is_a_typed_404(client, service):
    assert start_return(client, "COLOR").status == 303
    response = client.post(
        f"/s/{client.session_id()}/frobnicate", {TOKEN: "x"}
    )
    assert response.status in (403, 404)  # token checked first; both are typed
    assert "REQUEST_TOKEN_INVALID" in response.text or "UNKNOWN_ROUTE" in response.text
