"""End-to-end application tests for the LIVE path (review findings 1, 2 and 5).

The unchanged application is exercised: `ReturnService` is built in LIVE mode with one injected
adapter, so the service and its coordinator share that single instance. The shopper photo goes
through `service.submit_photo` (real byte validation), the garment references through the on-disk
rights-cleared catalogue, and the provider is an offline fixture.

These are application tests, not live qualification: no network, no credential, no spend.
"""
from __future__ import annotations

import json
import struct
from datetime import datetime, timezone

import pytest

from adapters.youcam_live import LiveSettings, Response, Transport, YouCamLivePreviewAdapter
from adapters.youcam_stub import AdapterError, FailureCode
from harness.clock import ManualClock
from harness.webclient import Client
from tests.web.helpers import reach_candidates
from ui.service import ReturnService
from ui.views import decision_ready_page
from ui.webapp import Application
from tests.provider.images import (jpeg, jpeg_header_only, png, png_header_only, png_noise, png_truncated)

START = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
HOST = "yce-api-01.makeupar.com"
S3 = "yce-us.s3-accelerate.amazonaws.com"


class FakeProvider(Transport):
    """Offline provider using the documented shapes: data.task_id, data.results.url, task_status."""

    def __init__(self, *, poll_body=None, task_response=None):
        self.calls = []
        self.poll_body = poll_body if poll_body is not None else {
            "data": {"error": None, "results": {"url": f"https://{S3}/result.png"},
                     "task_status": "success"}}
        self.task_response = task_response

    def request(self, method, url, *, headers, body=None, timeout, max_bytes=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "timeout": timeout})
        if url.endswith("/file"):
            n = len([c for c in self.calls if c["url"].endswith("/file")])
            return Response(200, json.dumps({"data": {"files": [{
                "file_id": f"file-{n}",
                "requests": [{"method": "PUT", "url": f"https://{S3}/u/{n}",
                              "headers": {"Content-Type": "image/jpg"}}]}]}}).encode())
        if url.startswith(f"https://{S3}/u/"):
            return Response(204, b"")
        if url.startswith(f"https://{S3}/result.png") or url.startswith(f"https://{S3}/out"):
            return Response(200, png(640, 480))
        if url.endswith("/task/cloth-v4"):
            if self.task_response is not None:
                return self.task_response
            return Response(200, json.dumps({"data": {"task_id": "prov-1"}}).encode())
        return Response(200, json.dumps(self.poll_body).encode())

    def task_calls(self):
        return [c for c in self.calls if c["url"].endswith("/task/cloth-v4")]


def build_service(tmp_path, provider, *, unit_cap=6):
    clock = ManualClock(START)
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="test-key", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2,
                              unit_cap=unit_cap, session_unit_cap=unit_cap,
                              ledger_path=str(tmp_path / "units.sqlite3")),
        clock=(lambda: clock.now()) if callable(clock.now) else (lambda: clock.now),
        transport=provider)
    service = ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE", adapter=adapter)
    return service, adapter, clock


def write_references(service, skus):
    refs = service.data_dir / "references"
    refs.mkdir(parents=True, exist_ok=True)
    for sku in skus:
        (refs / f"{sku}.png").write_bytes(png())
        (refs / f"{sku}.rights.json").write_text(
            json.dumps({"cleared": True, "rights_ref": f"licensed:{sku}:demo"}), encoding="utf-8")


def reach_ready(service, client):
    """Drive the real journey to CANDIDATES_READY, returning the session id the app itself used."""
    seen = {}
    original = service.get_case

    def spy(session_id):
        seen.setdefault("sid", session_id)
        return original(session_id)

    service.get_case = spy
    reach_candidates(client)
    sid = seen.get("sid")
    assert sid, "the application never exposed a session id"
    return sid


def test_live_application_journey_end_to_end(tmp_path):
    provider = FakeProvider()
    service, adapter, _ = build_service(tmp_path, provider)
    try:
        client = Client(Application(service))
        sid = reach_ready(service, client)
        write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
        service.submit_photo(sid, png(700, 520), "image/png")
        service.start_previews(sid)

        assert adapter._media, "no shopper media reached the adapter"
        for _ in range(8):
            service.advance_previews(sid)
            if service.sim.session(sid).preview_result is not None:
                break

        result = service.sim.session(sid).preview_result
        assert result is not None, "the live preview never produced a result"
        assert result.mode == "LIVE" and result.status == "SUCCEEDED"
        assert all(h.startswith("https://") for h in result.media_handles if h)

        html = decision_ready_page(service.get_case(sid))
        assert f"/s/{sid}/preview/" in html        # served same-origin, not from the provider host
        assert f"https://{S3}" not in html      # the provider image is what is displayed
        assert "YOUCAM RESULT" in html
        assert "not a fit or sizing guarantee" in html.lower()
        assert "refund" in html.lower()                # refund access survives a live success

        created = len(provider.task_calls())
        assert created >= 1
        assert adapter.units_consumed == 2 * created   # one reservation per billed task
        assert adapter.outstanding_reservations() == []
    finally:
        service.close()


def test_missing_photo_refuses_before_any_provider_call(tmp_path):
    provider = FakeProvider()
    service, adapter, _ = build_service(tmp_path, provider)
    try:
        client = Client(Application(service))
        sid = reach_ready(service, client)
        write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
        with pytest.raises(Exception) as exc:
            service.start_previews(sid)
        assert "photo" in str(exc.value).lower()
        assert provider.task_calls() == []
        assert adapter.units_consumed == 0
    finally:
        service.close()


def test_uncleared_reference_refuses_before_any_provider_call(tmp_path):
    provider = FakeProvider()
    service, adapter, _ = build_service(tmp_path, provider)
    try:
        client = Client(Application(service))
        sid = reach_ready(service, client)
        write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
        for path in (service.data_dir / "references").glob("*.rights.json"):
            path.write_text(json.dumps({"cleared": False, "rights_ref": "x"}), encoding="utf-8")
        service.submit_photo(sid, png(700, 520), "image/png")
        with pytest.raises(Exception) as exc:
            service.start_previews(sid)
        assert "cleared" in str(exc.value).lower() or "rights" in str(exc.value).lower()
        assert provider.task_calls() == []
    finally:
        service.close()


def test_provider_failure_yields_no_image_and_keeps_refund(tmp_path):
    provider = FakeProvider(poll_body={"data": {"error": "error_pose", "results": None,
                                                "task_status": "failed"}})
    service, adapter, _ = build_service(tmp_path, provider)
    try:
        client = Client(Application(service))
        sid = reach_ready(service, client)
        write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
        service.submit_photo(sid, png(700, 520), "image/png")
        service.start_previews(sid)
        for _ in range(8):
            service.advance_previews(sid)
            if service.sim.session(sid).preview_result is not None:
                break
        result = service.sim.session(sid).preview_result
        assert result is not None and result.status == "FAILED"
        assert not any(result.media_handles)
        assert "refund" in decision_ready_page(service.get_case(sid)).lower()
    finally:
        service.close()


def test_malformed_provider_response_does_not_crash_the_journey(tmp_path):
    provider = FakeProvider(poll_body={"data": {"results": ["not-an-object"],
                                                "task_status": "success"}})
    service, adapter, _ = build_service(tmp_path, provider)
    try:
        client = Client(Application(service))
        sid = reach_ready(service, client)
        write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
        service.submit_photo(sid, png(700, 520), "image/png")
        service.start_previews(sid)
        for _ in range(8):
            service.advance_previews(sid)
            if service.sim.session(sid).preview_result is not None:
                break
        result = service.sim.session(sid).preview_result
        assert result is not None and result.status == "FAILED"
        assert not any(result.media_handles)
    finally:
        service.close()
