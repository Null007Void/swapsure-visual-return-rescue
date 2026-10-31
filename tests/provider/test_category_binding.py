"""The outgoing provider payload, not the provider's answer, is the thing under test.

One running adapter and ledger: a women's dress first, then a men's top, then a third attempt that
the shared four-unit cap must refuse. Each task must carry its own verified category, its own
garment reference bytes, and the photo that was actually uploaded for that session.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from adapters.youcam_live import LiveSettings, Response, Transport, YouCamLivePreviewAdapter
from harness.clock import ManualClock
from ui.service import ReturnService

HOST = "yce-api-01.makeupar.com"
S3 = "yce-us.s3-accelerate.amazonaws.com"
REFS = pathlib.Path(__file__).resolve().parents[2] / "assets" / "catalog" / "references"
FEMALE = REFS / "W01-TEAL-WRAP-DRESS.png"   # a bundled, decodable PNG
MALE = REFS / "M03-TECHNICAL-JACKET.png"


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class RecordingProvider(Transport):
    """Records every outgoing body so the task payload can be inspected after the fact."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self.files: dict[str, bytes] = {}
        self.tasks: list[dict] = []

    def request(self, method, url, *, headers, body=None, timeout, max_bytes=None):
        self.calls.append((method, url, bytes(body or b"")))
        if url.endswith("/file"):
            n = len([c for c in self.calls if c[1].endswith("/file")])
            file_id = f"f{n}"
            self.files[file_id] = b""
            return Response(200, json.dumps({"data": {"files": [{"file_id": file_id, "requests": [
                {"method": "PUT", "url": f"https://{S3}/u/{file_id}",
                 "headers": {"Content-Type": "image/png"}}]}]}}).encode())
        if url.startswith(f"https://{S3}/u/"):
            file_id = url.rsplit("/", 1)[-1]
            self.files[file_id] = bytes(body or b"")
            return Response(204, b"")
        if url.endswith("/task/cloth-v4"):
            payload = json.loads(body.decode())
            self.tasks.append(payload)
            return Response(200, json.dumps({"data": {"task_id": f"p{len(self.tasks)}"}}).encode())
        if url.startswith(f"https://{S3}/out"):
            return Response(200, b"\x89PNG\r\n\x1a\nfake")
        return Response(200, json.dumps({"data": {"error": None, "task_status": "success",
                                                  "results": {"url": f"https://{S3}/out.png"}}}).encode())


def build(tmp_path):
    clock = ManualClock(__import__("datetime").datetime(
        2026, 10, 31, 4, 0, tzinfo=__import__("datetime").timezone.utc))
    provider = RecordingProvider()
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="offline-test", authorization_ref="OWNER-TEST-1.7.5",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2,
                              unit_cap=4, session_unit_cap=4,
                              ledger_path=str(tmp_path / "units.sqlite3")),
        clock=clock, transport=provider)
    service = ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE", adapter=adapter)
    return service, adapter, provider, clock


def prepare_session(service, collection: str, sku: str, photo: pathlib.Path) -> str:
    session_id = service.start_sample_return(collection, sku)
    service.opt_in(session_id)
    service.record_consent(session_id, image_consent=True, mock_acknowledged=True)
    service.submit_photo(session_id, photo.read_bytes(), "image/png")
    return session_id


def test_dress_then_mens_top_carry_their_own_category_reference_and_photo(tmp_path):
    service, adapter, provider, clock = build(tmp_path)
    try:
        # 1. a women's dress
        dress_sku = "W01-TEAL-WRAP-DRESS"
        sid_dress = prepare_session(service, "women", dress_sku, FEMALE)
        service.start_previews(sid_dress, sku=dress_sku)

        # 2. a men's top, same running adapter and ledger
        top_sku = "M01-ASYMMETRIC-KNIT-POLO"
        sid_top = prepare_session(service, "men", top_sku, MALE)
        service.start_previews(sid_top, sku=top_sku)

        assert len(provider.tasks) == 2, provider.tasks
        dress_task, top_task = provider.tasks

        # the category actually transmitted, per task
        assert dress_task["garment_category"] == "full_body"
        assert top_task["garment_category"] == "upper_body"

        # the garment reference bound to each task is that garment's own bytes
        assert provider.files[dress_task["ref_file_id"]] == (REFS / f"{dress_sku}.png").read_bytes()
        assert provider.files[top_task["ref_file_id"]] == (REFS / f"{top_sku}.png").read_bytes()
        assert sha(provider.files[dress_task["ref_file_id"]]) != sha(provider.files[top_task["ref_file_id"]])

        # the model photo each task carried is the photo uploaded in that session
        assert provider.files[dress_task["src_file_id"]] == FEMALE.read_bytes()
        assert provider.files[top_task["src_file_id"]] == MALE.read_bytes()
        assert dress_task["src_file_id"] != top_task["src_file_id"]

        # one shared four-unit cap: two tasks, 4 units, and nothing more
        assert adapter.units_consumed == 4
        import contextlib
        import sqlite3
        with contextlib.closing(sqlite3.connect(f"file:{tmp_path / 'units.sqlite3'}?mode=ro", uri=True)) as con:
            rows = list(con.execute("SELECT state, units FROM reservations"))
        assert len(rows) == 2 and all(state == "CONFIRMED" and units == 2 for state, units in rows)

        # 3. a third garment in the second session is refused before it can spend
        service.advance_previews(sid_top)
        service.try_another(sid_top)
        with pytest.raises(Exception) as refused:
            service.start_previews(sid_top, sku="M04-TEXTURED-CAMP-SHIRT")
        assert "ALLOWANCE_EXHAUSTED" in str(refused.value)
        assert len(provider.tasks) == 2
        assert adapter.units_consumed == 4
    finally:
        service.close()


def test_a_jacket_uses_the_documented_outer_category(tmp_path):
    service, adapter, provider, clock = build(tmp_path)
    try:
        sku = "M03-TECHNICAL-JACKET"
        sid = prepare_session(service, "men", sku, MALE)
        service.start_previews(sid, sku=sku)
        assert provider.tasks[0]["garment_category"] == "outer"
        assert provider.files[provider.tasks[0]["ref_file_id"]] == (REFS / f"{sku}.png").read_bytes()
    finally:
        service.close()


def test_an_unmapped_garment_is_refused_before_anything_is_sent(tmp_path):
    service, adapter, provider, clock = build(tmp_path)
    try:
        from ui.service import ServiceError

        sku = "W01-TEAL-WRAP-DRESS"
        sid = prepare_session(service, "women", sku, FEMALE)
        case = service.get_case(sid).case
        assert service._provider_category(case, sku) == "full_body"
        with pytest.raises(ServiceError) as refused:
            service._provider_category(case, "NOT-IN-CATALOGUE")
        assert refused.value.code == "CATEGORY_UNMAPPED"
        # and the adapter refuses an unknown category before it stores or sends anything
        with pytest.raises(Exception) as bad:
            adapter.prepare_media(session_id=sid, candidate_id="X", payload=FEMALE.read_bytes(),
                                  content_type="image/png",
                                  consent=service._live_consent(sid, case),
                                  reference_rights="synthetic",
                                  reference_payload=(REFS / f"{sku}.png").read_bytes(),
                                  garment_category="not-a-category")
        assert "unknown garment category" in str(bad.value)
        assert provider.tasks == []
    finally:
        service.close()
