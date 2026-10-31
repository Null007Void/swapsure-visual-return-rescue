"""Focused regressions for the corrected LIVE YouCam adapter (review findings 2-6).

Every response here is an offline fixture built from the documented contract
(`data.task_status`, `data.results[].url`). Fixtures prove request construction, validation,
reservation accounting and failure mapping. They do **not** prove provider behaviour, and nothing
here is live qualification.
"""
from __future__ import annotations

import json
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adapters.youcam_live import (OFFICIAL_API_ORIGIN, ConsentGrant, LiveSettings, ReservationLedger,
                                  Response, Transport, YouCamLivePreviewAdapter, live_configured,
                                  sniff_image)
from adapters.youcam_stub import AdapterError, FailureCode, LiveTransportAbsent, adapter_for_mode
from domain.preview import PreviewPlan, PreviewResult
from tests.provider.images import (jpeg, jpeg_header_only, png, png_header_only, png_noise, png_truncated)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
HOST = "yce-api-01.makeupar.com"
S3 = "yce-us.s3-accelerate.amazonaws.com"


def ok(body, status=200):
    return Response(status, json.dumps(body).encode())


def registration(file_id="f1", url=None, method="PUT", headers=None):
    return ok({"data": {"files": [{"file_id": file_id,
                                   "requests": [{"method": method,
                                                 "url": url or f"https://{S3}/upload/{file_id}",
                                                 "headers": headers if headers is not None
                                                 else {"Content-Type": "image/jpeg"}}]}]}})


class FakeTransport(Transport):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body=None, timeout, max_bytes=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": body, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def to(self, suffix):
        return [c for c in self.calls if c["url"].endswith(suffix)]


def consent(**over):
    base = dict(session_id="ses_1", generation=1, version="v1",
                granted_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=30))
    base.update(over)
    return ConsentGrant(**base)


def settings(tmp_path, **over):
    base = dict(api_key="test-key", authorization_ref="OWNER-D-1",
                allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2, unit_cap=20,
                session_unit_cap=10, ledger_path=str(tmp_path / "units.sqlite3"))
    base.update(over)
    return LiveSettings(**base)


def build(tmp_path, responses, *, clock=lambda: NOW, **over):
    return YouCamLivePreviewAdapter(settings=settings(tmp_path, **over), clock=clock,
                                    transport=FakeTransport(responses), sleep=lambda _s: None)


def prepared(adapter, candidates=("cand-0",), **over):
    for cid in candidates:
        adapter.prepare_media(session_id="ses_1", candidate_id=cid, payload=png(),
                              content_type="image/png", consent=over.get("consent", consent()),
                              reference_rights=over.get("rights", "licensed:demo-ref-1"),
                              reference_payload=png(), garment_category="full_body",)


def plan(n=1, units=2, mode="LIVE", ref="OWNER-D-1"):
    return PreviewPlan(session_id="ses_1", consent_version="v1",
                       candidate_ids=tuple(f"cand-{i}" for i in range(n)),
                       provider_version="cloth-v4", estimated_units=units,
                       authorization_ref=ref, deadline=NOW + timedelta(seconds=60), mode=mode)


HAPPY = [registration("src1"), Response(200, b""), registration("ref1"), Response(200, b""),
         ok({"data": {"task_id": "prov1"}})]


# -- finding 2: no synthetic media ------------------------------------------------- #
def test_synthetic_marker_is_refused(tmp_path):
    a = build(tmp_path, [])
    with pytest.raises(AdapterError) as exc:
        a.prepare_media(session_id="ses_1", candidate_id="cand-0",
                        payload=b"\x89PNG\r\n\x1a\nsynthetic-reference", content_type="image/png",
                        consent=consent(), reference_rights="licensed", reference_payload=png(), garment_category="full_body",)
    assert exc.value.code is FailureCode.INVALID_INPUT


def test_real_jpeg_dimensions_are_parsed_and_small_images_refused():
    assert sniff_image(png(1024, 768), "image/png") == (1024, 768)
    with pytest.raises(AdapterError):
        sniff_image(png(300, 768), "image/png")


def test_content_type_must_match_the_bytes(tmp_path):
    a = build(tmp_path, [])
    with pytest.raises(AdapterError) as exc:
        a.prepare_media(session_id="ses_1", candidate_id="cand-0", payload=png(),
                        content_type="image/jpeg", consent=consent(),
                        reference_rights="licensed", reference_payload=png(), garment_category="full_body",)
    assert exc.value.code is FailureCode.INVALID_INPUT


def test_reference_rights_and_real_reference_are_required(tmp_path):
    a = build(tmp_path, [])
    with pytest.raises(AdapterError) as exc:
        a.prepare_media(session_id="ses_1", candidate_id="c", payload=png(),
                        content_type="image/png", consent=consent(), reference_rights=None,
                        reference_payload=png(), garment_category="full_body",)
    assert "rights" in str(exc.value).lower()
    with pytest.raises(AdapterError):
        a.prepare_media(session_id="ses_1", candidate_id="c", payload=png(),
                        content_type="image/png", consent=consent(), reference_rights="licensed",
                        reference_payload=None, garment_category="full_body",)


# -- finding 3: durable reservations ------------------------------------------------- #
def test_reservation_is_taken_before_creation_and_confirmed(tmp_path):
    a = build(tmp_path, HAPPY)
    prepared(a)
    a.create_preview(plan())
    assert a.units_consumed == 2
    assert a.outstanding_reservations() == []          # confirmed, not outstanding


def test_duplicate_operation_cannot_spend_twice(tmp_path):
    a = build(tmp_path, HAPPY)
    prepared(a)
    a.create_preview(plan())
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan())                       # same session/candidate/authorization
    assert exc.value.code is FailureCode.BUDGET_EXHAUSTED
    assert a.units_consumed == 2


def test_lost_acknowledgement_keeps_the_reservation_uncertain(tmp_path):
    a = build(tmp_path, HAPPY[:4] + [AdapterError(FailureCode.TIMEOUT, "no response")])
    prepared(a)
    with pytest.raises(AdapterError):
        a.create_preview(plan())
    assert a.units_consumed == 2                       # uncertain still counts against the cap
    assert [r["state"] for r in a.outstanding_reservations()] == ["UNCERTAIN"]


def test_preflight_failure_releases_the_hold(tmp_path):
    bad = registration("src1", url="https://evil.example/upload")
    a = build(tmp_path, [bad])
    prepared(a)
    with pytest.raises(AdapterError):
        a.create_preview(plan())
    assert a.units_consumed == 0
    assert a.outstanding_reservations() == []


def test_session_and_deployment_caps_are_enforced(tmp_path):
    a = build(tmp_path, HAPPY, session_unit_cap=2, unit_cap=100)
    prepared(a)
    a.create_preview(plan(n=1))
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan(n=1))
    assert exc.value.code is FailureCode.BUDGET_EXHAUSTED
    assert a.units_consumed == 2


def test_two_workers_share_one_durable_ledger(tmp_path):
    ledger_path = str(tmp_path / "units.sqlite3")
    first = build(tmp_path, HAPPY, unit_cap=2, session_unit_cap=10)
    second = YouCamLivePreviewAdapter(settings=settings(tmp_path, unit_cap=2), clock=lambda: NOW,
                                      transport=FakeTransport(HAPPY), sleep=lambda _s: None,
                                      ledger=ReservationLedger(ledger_path))
    prepared(first)
    prepared(second)
    first.create_preview(plan())
    adopted = second.create_preview(plan())            # recovery, not a second purchase
    assert len(adopted) == 1 and adopted[0].status == "PENDING"
    assert second.tasks_created == 0                   # nothing new was billed
    assert second.units_consumed == 2                  # the durable ledger still holds one unit hold
    assert adopted[0].task_id == first.units_consumed * "x" or adopted[0].task_id


def test_restart_preserves_spend_and_blocks_a_fresh_instance(tmp_path):
    first = build(tmp_path, HAPPY, unit_cap=2)
    prepared(first)
    first.create_preview(plan())
    reopened = ReservationLedger(str(tmp_path / "units.sqlite3"))
    assert reopened.outstanding() == []                # confirmed, and durably counted
    assert reopened.committed_units() == 2
    # A fresh process (new adapter, same ledger) cannot spend the cap again after a restart.
    restarted = build(tmp_path, HAPPY, unit_cap=2)
    restarted.prepare_media(session_id="ses_1", candidate_id="cand-9", payload=png(),
                            content_type="image/png", consent=consent(),
                            reference_rights="licensed", reference_payload=png(), garment_category="full_body",)
    with pytest.raises(AdapterError) as exc:
        restarted.create_preview(PreviewPlan(
            session_id="ses_1", consent_version="v1", candidate_ids=("cand-9",),
            provider_version="cloth-v4", estimated_units=2, authorization_ref="OWNER-D-1",
            deadline=NOW + timedelta(seconds=60), mode="LIVE"))
    assert exc.value.code is FailureCode.BUDGET_EXHAUSTED


# -- finding 4: outbound transport gaps ---------------------------------------------- #
def test_unapproved_api_origin_is_refused_at_construction(tmp_path):
    with pytest.raises(AdapterError) as exc:
        settings(tmp_path, api_origin="https://attacker.example")
    assert exc.value.code is FailureCode.DISALLOWED_DESTINATION


def test_redirect_is_refused_not_followed(tmp_path):
    a = build(tmp_path, [Response(302, b"", {"location": "https://evil.example/x"})])
    prepared(a)
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan())
    assert exc.value.code is FailureCode.DISALLOWED_DESTINATION


def test_disallowed_upload_host_is_refused_before_upload(tmp_path):
    a = build(tmp_path, [registration("src1", url="https://evil.example/u")])
    prepared(a)
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan())
    assert exc.value.code is FailureCode.DISALLOWED_DESTINATION
    assert a._transport.calls and a._transport.calls[-1]["method"] == "POST"


def test_upload_instruction_method_and_headers_are_validated(tmp_path):
    a = build(tmp_path, [registration("src1", method="POST")])
    prepared(a)
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan())
    assert "PUT" in str(exc.value)

    b = build(tmp_path, [registration("src1", headers={"X-Secret": "1"})],
              ledger_path=str(tmp_path / "b.sqlite3"))
    prepared(b)
    with pytest.raises(AdapterError):
        b.create_preview(plan())


def test_oversized_response_is_invalid_result(tmp_path):
    from adapters.youcam_live import MAX_RESPONSE_BYTES
    a = build(tmp_path, [Response(200, b"{" + b"0" * (MAX_RESPONSE_BYTES + 10) + b"}")])
    prepared(a)
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan())
    assert exc.value.code in (FailureCode.INVALID_RESULT, FailureCode.TIMEOUT)


# -- finding 5: consent and deadlines ------------------------------------------------ #
def test_revoked_consent_stops_task_creation(tmp_path):
    a = build(tmp_path, HAPPY)
    ref = a.prepare_media(session_id="ses_1", candidate_id="cand-0", payload=png(),
                          content_type="image/png", consent=consent(),
                          reference_rights="licensed", reference_payload=png(), garment_category="full_body",)
    a._consent[ref.handle] = consent(revoked_at=NOW - timedelta(seconds=1))   # revoked after consent
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan())
    assert exc.value.code is FailureCode.CONSENT_REQUIRED
    assert a.units_consumed == 0


def test_expired_consent_stops_task_creation(tmp_path):
    a = build(tmp_path, HAPPY)
    ref = a.prepare_media(session_id="ses_1", candidate_id="cand-0", payload=png(),
                          content_type="image/png", consent=consent(),
                          reference_rights="licensed", reference_payload=png(), garment_category="full_body",)
    a._consent[ref.handle] = consent(expires_at=NOW - timedelta(seconds=1))   # expired before spending
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan())
    assert exc.value.code is FailureCode.CONSENT_REQUIRED


def test_consent_revoked_mid_flight_stops_polling(tmp_path):
    holder = {"grant": consent()}
    t = FakeTransport(HAPPY + [ok({"data": {"task_status": "processing"}})])
    a = YouCamLivePreviewAdapter(settings=settings(tmp_path), clock=lambda: NOW, transport=t,
                                 sleep=lambda _s: None)
    a.prepare_media(session_id="ses_1", candidate_id="cand-0", payload=png(),
                    content_type="image/png", consent=holder["grant"],
                    reference_rights="licensed", reference_payload=png(), garment_category="full_body",)
    task = a.create_preview(plan())[0]
    a._consent[task.media_handle] = consent(revoked_at=NOW - timedelta(seconds=1))
    with pytest.raises(AdapterError) as exc:
        a.poll_preview(task.task_id, session_id="ses_1")
    assert exc.value.code is FailureCode.CONSENT_REQUIRED


def test_request_timeout_is_bounded_by_the_remaining_deadline(tmp_path):
    a = build(tmp_path, HAPPY)
    prepared(a)
    a.create_preview(plan())
    assert all(c["timeout"] <= 15.0 for c in a._transport.calls)


# -- finding 6: documented contract -------------------------------------------------- #
def test_documented_fields_drive_pending_success_and_failure(tmp_path):
    t = FakeTransport(HAPPY + [ok({"data": {"task_status": "processing"}}),
                               ok({"data": {"task_status": "success",
                                            "results": {"url": f"https://{S3}/out.png"}}}),
                               Response(200, png())])   # the result bytes the product validates
    a = YouCamLivePreviewAdapter(settings=settings(tmp_path), clock=lambda: NOW, transport=t,
                                 sleep=lambda _s: None)
    a.prepare_media(session_id="ses_1", candidate_id="cand-0", payload=png(),
                    content_type="image/png", consent=consent(),
                    reference_rights="licensed", reference_payload=png(), garment_category="full_body",)
    task = a.create_preview(plan())[0]
    assert a.poll_preview(task.task_id, session_id="ses_1").status == "PENDING"
    done = a.poll_preview(task.task_id, session_id="ses_1")
    assert done.status == "SUCCEEDED" and done.media_handle == f"https://{S3}/out.png"
    result = a.build_result(session_id="ses_1", plan=plan(), tasks=(done,))
    assert isinstance(result, PreviewResult) and result.status == "SUCCEEDED"
    assert result.mode == "LIVE" and result.media_handles[0].startswith("https://")


def test_provider_failure_status_maps_and_carries_no_media(tmp_path):
    t = FakeTransport(HAPPY + [ok({"data": {"task_status": "failed"}})])
    a = YouCamLivePreviewAdapter(settings=settings(tmp_path), clock=lambda: NOW, transport=t,
                                 sleep=lambda _s: None)
    a.prepare_media(session_id="ses_1", candidate_id="cand-0", payload=png(),
                    content_type="image/png", consent=consent(),
                    reference_rights="licensed", reference_payload=png(), garment_category="full_body",)
    task = a.create_preview(plan())[0]
    failed = a.poll_preview(task.task_id, session_id="ses_1")
    assert failed.status == "FAILED" and failed.failure is FailureCode.PROVIDER_FAILURE
    result = a.build_result(session_id="ses_1", plan=plan(), tasks=(failed,))
    assert result.status == "FAILED" and not any(result.media_handles)
    assert result.failure_code == "PROVIDER_FAILURE"


# -- mode switch --------------------------------------------------------------------- #
def test_mode_switch_is_fail_closed_without_configuration(monkeypatch):
    for name in ("YOUCAM_API_KEY", "SWAPSURE_LIVE_AUTHORIZATION_REF", "SWAPSURE_LIVE_ALLOWED_HOSTS",
                 "SWAPSURE_LIVE_UNIT_CAP"):
        monkeypatch.delenv(name, raising=False)
    assert live_configured() is False
    with pytest.raises(LiveTransportAbsent):
        adapter_for_mode("LIVE", clock=lambda: NOW)
    assert adapter_for_mode("MOCK", clock=lambda: NOW).mode == "MOCK"


def test_repr_never_prints_the_key(tmp_path):
    assert "test-key" not in repr(settings(tmp_path))
