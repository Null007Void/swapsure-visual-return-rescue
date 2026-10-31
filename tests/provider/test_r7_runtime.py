"""Focused regressions for the four remaining runtime closures (1.6.2).

R1  PNG geometry/format are validated before any inflation; inflation is bounded and refuses
    overflow, incomplete streams and trailing compressed data
R2  deletion tombstones survive a service restart: delivery is refused with zero provider fetches
R3  the consent page's layout carries the real mode, so the whole LIVE page has no MOCK banner
R4  the qualification path is PNG-only, and UI plus documentation say so
"""
from __future__ import annotations

import json
import pathlib
import struct
import zlib
from datetime import datetime, timezone

import pytest

import adapters.youcam_live as live
from adapters.youcam_live import Response, Transport, LiveSettings, YouCamLivePreviewAdapter
from adapters.youcam_stub import AdapterError, FailureCode
from harness.clock import ManualClock
from harness.webclient import Client
from tests.provider.images import png
from ui.service import ReturnService
from ui.webapp import Application

HOST = "yce-api-01.makeupar.com"
S3 = "yce-us.s3-accelerate.amazonaws.com"
START = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ PNG fixture builder
def chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) % (2 ** 32)))


def build_png(*, width=512, height=384, color_type=2, bit_depth=8, raw=None,
              idat_suffix=b"", ihdr_body=None, trailing=b"") -> bytes:
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type, 1)
    if raw is None:
        raw = b"".join(b"\x00" + bytes((10, 20, 30)) * width for _ in range(height))
    body = ihdr_body if ihdr_body is not None else struct.pack(
        ">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", body) + chunk(b"IDAT", zlib.compress(raw) + idat_suffix)
            + chunk(b"IEND", b"") + trailing)


# ------------------------------------------------------------------ R1
def test_r1_ihdr_length_and_format_are_rejected_before_inflation():
    bad_length = build_png(ihdr_body=struct.pack(">IIBBBBB", 512, 384, 8, 2, 0, 0, 0) + b"\x00")
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(bad_length, "image/png")
    assert "IHDR" in str(exc.value)

    bad_depth = build_png(bit_depth=16)
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(bad_depth, "image/png")
    assert "pixel format" in str(exc.value)

    palette = build_png(color_type=3)
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(palette, "image/png")
    assert "pixel format" in str(exc.value)


def test_r1_oversized_geometry_is_refused_without_decompressing():
    # The IDAT is deliberately un-inflatable: reaching it would raise a different message.
    payload = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", 5000, 100, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", b"not-a-zlib-stream")
               + chunk(b"IEND", b""))
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(payload, "image/png")
    assert "maximum side" in str(exc.value)


def test_r1_decoded_size_bound_is_enforced(monkeypatch):
    monkeypatch.setattr(live, "MAX_DECODED_BYTES", 1024)
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(build_png(width=512, height=384), "image/png")
    assert "above the" in str(exc.value)


def test_r1_inflation_overflow_is_refused_without_allocating_the_whole_stream():
    # A small file whose declared geometry is modest but whose stream inflates far past it.
    oversize_raw = b"\x00" * (512 * 384 * 3 + 100_000)
    payload = build_png(width=512, height=384, color_type=2, raw=oversize_raw)
    assert len(payload) < 600_000           # a small fixture, not a memory-exhaustion test
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(payload, "image/png")
    assert "declared size" in str(exc.value)


def test_r1_incomplete_stream_and_trailing_compressed_data_are_refused():
    raw = b"".join(b"\x00" + bytes((1, 2, 3)) * 512 for _ in range(384))
    stream = zlib.compress(raw)
    incomplete = (b"\x89PNG\r\n\x1a\n"
                  + chunk(b"IHDR", struct.pack(">IIBBBBB", 512, 384, 8, 2, 0, 0, 0))
                  + chunk(b"IDAT", stream[: len(stream) // 2])
                  + chunk(b"IEND", b""))
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(incomplete, "image/png")
    assert "incomplete" in str(exc.value) or "wrong length" in str(exc.value)

    trailing = (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 512, 384, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", stream + b"\x00" * 32)
                + chunk(b"IEND", b""))
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(trailing, "image/png")
    assert "trailing" in str(exc.value) or "wrong length" in str(exc.value)


# ------------------------------------------------------------------ R2
class FakeProvider(Transport):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, *, headers, body=None, timeout, max_bytes=None):
        self.calls.append((method, url))
        if url.endswith("/file"):
            n = len([c for c in self.calls if c[1].endswith("/file")])
            return Response(200, json.dumps({"data": {"files": [{"file_id": f"f{n}", "requests": [
                {"method": "PUT", "url": f"https://{S3}/u/{n}",
                 "headers": {"Content-Type": "image/png"}}]}]}}).encode())
        if url.startswith(f"https://{S3}/u/"):
            return Response(204, b"")
        if url.endswith("/task/cloth-v4"):
            return Response(200, json.dumps({"data": {"task_id": "p1"}}).encode())
        if url.startswith(f"https://{S3}/out"):
            return Response(200, png(640, 480))
        return Response(200, json.dumps({"data": {"error": None, "task_status": "success",
                                                  "results": {"url": f"https://{S3}/out.png"}}}).encode())

    def image_fetches(self):
        return [c for c in self.calls if c[1].startswith(f"https://{S3}/out")]


def multipart(payload: bytes, token: str):
    boundary = "----r7"
    crlf = chr(13) + chr(10)
    head = ("--" + boundary + crlf
            + 'Content-Disposition: form-data; name="request_token"' + crlf + crlf + token + crlf
            + "--" + boundary + crlf
            + 'Content-Disposition: form-data; name="photo"; filename="me.png"' + crlf
            + "Content-Type: image/png" + crlf + crlf).encode()
    return head + payload + (crlf + "--" + boundary + "--" + crlf).encode(), \
        "multipart/form-data; boundary=" + boundary


def make_service(tmp_path, provider, clock):
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="k", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2, unit_cap=8,
                              session_unit_cap=8, ledger_path=str(tmp_path / "units.sqlite3")),
        clock=lambda: clock.now, transport=provider)
    return ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE", adapter=adapter)


def run_live_journey(service, data_dir):
    client = Client(Application(service))
    assert client.post("/returns", {"reason": "COLOR"}).status == 303
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(f"/s/{client.session_id()}/opt-in",
                       {"request_token": client.form_token(page.text)}).status == 303
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(f"/s/{client.session_id()}/consent",
                       {"request_token": client.form_token(page.text),
                        "imageConsent": "true", "mockAcknowledged": "true"}).status == 303
    panel = client.get(f"/s/{client.session_id()}/consent-panel")
    body, ctype = multipart(png(700, 520), client.form_token(panel.text))
    assert client.request("POST", f"/s/{client.session_id()}/photo", body=body,
                          content_type=ctype).status == 303
    refs = service.data_dir / "references"
    refs.mkdir(parents=True, exist_ok=True)
    for cand in service.get_case(client.session_id()).selection.candidates:
        (refs / f"{cand.sku}.png").write_bytes(png(512, 400))
        (refs / f"{cand.sku}.rights.json").write_text(
            json.dumps({"cleared": True, "rights_ref": "licensed"}), encoding="utf-8")
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(f"/s/{client.session_id()}/preview",
                       {"request_token": client.form_token(page.text)}).status == 303
    client.get(f"/s/{client.session_id()}")
    return client


def test_r2_tombstone_survives_restart_and_blocks_delivery(tmp_path):
    clock = ManualClock(START)
    provider = FakeProvider()
    service = make_service(tmp_path, provider, clock)
    try:
        client = run_live_journey(service, tmp_path)
        sid = client.session_id()
        sku = service.get_case(sid).selection.candidates[0].sku
        assert client.get(f"/s/{sid}/preview/{sku}").status == 200
        page = client.get(f"/s/{sid}")
        assert client.post(f"/s/{sid}/delete-media",
                           {"request_token": client.form_token(page.text)}).status in (303, 200)
    finally:
        service.close()

    # A fresh process: no in-memory cache, no in-memory tombstone dictionary.
    restarted_provider = FakeProvider()
    restarted = make_service(tmp_path, restarted_provider, clock)
    try:
        assert restarted._result_tombstones == {}            # memory really is empty
        assert restarted.store.get_image_tombstone(sid) is not None
        assert restarted.result_image(sid, sku) if False else True
        with pytest.raises(Exception) as exc:
            restarted.result_image(sid, sku)
        assert "IMAGERY_DELETED" in str(exc.value)
        assert restarted_provider.image_fetches() == []      # zero provider fetches
    finally:
        restarted.close()


# ------------------------------------------------------------------ R3
def test_r3_live_consent_page_has_no_mock_banner(tmp_path):
    clock = ManualClock(START)
    service = make_service(tmp_path, FakeProvider(), clock)
    try:
        client = Client(Application(service))
        assert client.post("/returns", {"reason": "COLOR"}).status == 303
        page = client.get(f"/s/{client.session_id()}")
        assert client.post(f"/s/{client.session_id()}/opt-in",
                           {"request_token": client.form_token(page.text)}).status == 303
        text = client.get(f"/s/{client.session_id()}").text
        lowered = text.lower()
        assert "mode: live" in lowered
        for banned in ("mode: mock", "zero provider calls", "zero youcam units", "placeholder",
                       "does not process a photo of you", "no live transport"):
            assert banned not in lowered, banned
    finally:
        service.close()


def test_r3_mock_consent_page_keeps_the_mock_banner(tmp_path):
    service = ReturnService(tmp_path / "m", clock=ManualClock(START))
    try:
        client = Client(Application(service))
        assert client.post("/returns", {"reason": "COLOR"}).status == 303
        page = client.get(f"/s/{client.session_id()}")
        assert client.post(f"/s/{client.session_id()}/opt-in",
                           {"request_token": client.form_token(page.text)}).status == 303
        lowered = client.get(f"/s/{client.session_id()}").text.lower()
        assert "mode: mock" in lowered and "zero provider calls" in lowered
    finally:
        service.close()


# ------------------------------------------------------------------ R4
def test_r4_qualification_path_is_png_only_and_ui_matches(tmp_path):
    from tests.provider.images import jpeg
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(jpeg(640, 480), "image/jpeg")
    assert exc.value.code is FailureCode.INVALID_INPUT
    assert "PNG" in str(exc.value)

    from ui.photo import ALLOWED_TYPES
    assert ALLOWED_TYPES == ("image/png",)

    from ui.references import _SUFFIXES
    assert [suffix for suffix, _ in _SUFFIXES] == [".png"]

    # The rendered upload control offers PNG only, on the real page.
    clock = ManualClock(START)
    service = make_service(tmp_path, FakeProvider(), clock)
    try:
        client = Client(Application(service))
        assert client.post("/returns", {"reason": "COLOR"}).status == 303
        page = client.get(f"/s/{client.session_id()}")
        assert client.post(f"/s/{client.session_id()}/opt-in",
                           {"request_token": client.form_token(page.text)}).status == 303
        panel = client.get(f"/s/{client.session_id()}/consent-panel").text
        assert 'accept="image/png"' in panel
        assert "image/jpeg" not in panel
    finally:
        service.close()

    doc = pathlib.Path("README.md").read_text(encoding="utf-8")
    assert "PNG only" in doc or "PNG-only" in doc


def test_r7_live_home_page_reports_live_not_mock(tmp_path):
    """The entry page is where an operator first looks; it must not claim MOCK on a LIVE server."""
    service = make_service(tmp_path, FakeProvider(), ManualClock(START))
    try:
        client = Client(Application(service))
        text = client.get("/").text.lower()
        assert "mode: live" in text
        for banned in ("mode: mock", "zero provider calls", "zero youcam units", "placeholder"):
            assert banned not in text, banned
    finally:
        service.close()


def test_r7_mock_home_page_still_reports_mock(tmp_path):
    service = ReturnService(tmp_path / "m2", clock=ManualClock(START))
    try:
        text = Client(Application(service)).get("/").text.lower()
        assert "mode: mock" in text and "zero provider calls" in text
    finally:
        service.close()
