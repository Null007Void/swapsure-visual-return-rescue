"""A catalogue click binds one garment; that exact reference and category reach the provider."""
from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import datetime, timezone

from adapters.youcam_live import LiveSettings, Response, Transport, YouCamLivePreviewAdapter
from harness.clock import ManualClock
from ui.service import ReturnService

HOST = "yce-api-01.makeupar.com"
S3 = "yce-us.s3-accelerate.amazonaws.com"
REFS = pathlib.Path(__file__).resolve().parents[2] / "assets" / "catalog" / "references"
PHOTO = REFS / "W01-TEAL-WRAP-DRESS.png"   # bundled fixture photo, not a shopper's


class RecordingProvider(Transport):
    def __init__(self):
        self.tasks = []
        self.files = {}
        self.calls = 0

    def request(self, method, url, *, headers, body=None, timeout, max_bytes=None):
        self.calls += 1
        if url.endswith("/file"):
            n = len([k for k in self.files]) + 1
            self.files[f"f{n}"] = b""
            return Response(200, json.dumps({"data": {"files": [{"file_id": f"f{n}", "requests": [
                {"method": "PUT", "url": f"https://{S3}/u/f{n}",
                 "headers": {"Content-Type": "image/png"}}]}]}}).encode())
        if url.startswith(f"https://{S3}/u/"):
            self.files[url.rsplit("/", 1)[-1]] = bytes(body or b"")
            return Response(204, b"")
        if url.endswith("/task/cloth-v4"):
            self.tasks.append(json.loads(body.decode()))
            return Response(200, json.dumps({"data": {"task_id": f"p{len(self.tasks)}"}}).encode())
        return Response(200, json.dumps({"data": {"error": None, "task_status": "success",
                                                  "results": {"url": f"https://{S3}/out.png"}}}).encode())


def build(tmp_path):
    clock = ManualClock(datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc))
    provider = RecordingProvider()
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="offline", authorization_ref="OWNER-TEST-BIND",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2,
                              unit_cap=8, session_unit_cap=8,
                              ledger_path=str(tmp_path / "units.sqlite3")),
        clock=clock, transport=provider)
    return ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE", adapter=adapter), adapter, provider


def click_and_generate(service, collection, sku):
    session_id = service.start_sample_return(collection, sku)   # the catalogue click
    service.opt_in(session_id)
    service.record_consent(session_id, image_consent=True, mock_acknowledged=True)
    service.submit_photo(session_id, PHOTO.read_bytes(), "image/png")
    assert service.selected_garment(session_id) == sku
    service.start_previews(session_id)                          # no sku argument: the bound one
    return session_id


def test_red_dress_click_sends_that_dress_reference_and_category(tmp_path):
    service, adapter, provider = build(tmp_path)
    try:
        sku = "W06-RED-FLORAL-MAXIDRESS"
        click_and_generate(service, "women", sku)
        task = provider.tasks[0]
        assert task["garment_category"] == "full_body"
        assert provider.files[task["ref_file_id"]] == (REFS / (sku + ".png")).read_bytes()
        assert hashlib.sha256(provider.files[task["src_file_id"]]).hexdigest() == \
            hashlib.sha256(PHOTO.read_bytes()).hexdigest()
    finally:
        service.close()


def test_mens_click_sends_that_jacket_reference_and_category(tmp_path):
    service, adapter, provider = build(tmp_path)
    try:
        sku = "M03-TECHNICAL-JACKET"
        click_and_generate(service, "men", sku)
        task = provider.tasks[0]
        assert task["garment_category"] == "outer"
        assert provider.files[task["ref_file_id"]] == (REFS / (sku + ".png")).read_bytes()
    finally:
        service.close()


def test_change_garment_clears_the_binding_but_photo_and_consent_stay(tmp_path):
    service, adapter, provider = build(tmp_path)
    try:
        sku = "W06-RED-FLORAL-MAXRESS" if False else "W06-RED-FLORAL-MAXIDRESS"
        sid = service.start_sample_return("women", sku)
        service.opt_in(sid)
        service.record_consent(sid, image_consent=True, mock_acknowledged=True)
        service.submit_photo(sid, PHOTO.read_bytes(), "image/png")
        service.clear_garment(sid)
        assert service.selected_garment(sid) is None
        assert service.photo.has(sid), "the photo survives changing the garment"
        service.choose_garment(sid, "W01-TEAL-WRAP-DRESS")
        assert service.selected_garment(sid) == "W01-TEAL-WRAP-DRESS"
        service.start_previews(sid)
        assert provider.tasks[0]["garment_category"] == "full_body"
        assert provider.files[provider.tasks[0]["ref_file_id"]] == (REFS / "W01-TEAL-WRAP-DRESS.png").read_bytes()
    finally:
        service.close()
