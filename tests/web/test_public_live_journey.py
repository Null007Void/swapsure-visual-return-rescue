"""Public-route live journey (review round 5 acceptance gate).

Every step goes through the normal HTTP routes and forms of the WSGI application — the photo is
uploaded as a real multipart form, consent is granted through the consent form, previews are started
through the preview action, and result imagery is fetched from the same-origin preview route. The
service methods `submit_photo` and `start_previews` are never called directly.

Images are genuine, decodable PNGs built with zlib (no header-plus-zeros stubs). The provider is an
offline fixture. This is not live qualification: no network, credential or unit is involved.
"""
from __future__ import annotations

import json
import struct
import zlib
from datetime import datetime, timezone

import pytest

from adapters.youcam_live import LiveSettings, Response, Transport, YouCamLivePreviewAdapter
from harness.clock import ManualClock
from harness.webclient import Client
from ui.service import ReturnService
from ui.webapp import Application

HOST = "yce-api-01.makeupar.com"
S3 = "yce-us.s3-accelerate.amazonaws.com"
START = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------- real PNG fixtures
def png(width: int = 640, height: int = 480, rgb: tuple[int, int, int] = (30, 90, 200)) -> bytes:
    """A genuine PNG: real IHDR/IDAT/IEND chunks and a valid zlib deflate stream."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(raw, 6))
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + chunk(b"IEND", b"")


def decodable(payload: bytes) -> tuple[int, int]:
    """Decode a PNG independently of the product code, proving the bytes really are an image."""
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert payload[12:16] == b"IHDR"
    width, height = struct.unpack(">II", payload[16:24])
    idat = b""
    index = 8
    while index < len(payload):
        length = struct.unpack(">I", payload[index:index + 4])[0]
        tag = payload[index + 4:index + 8]
        data = payload[index + 8:index + 8 + length]
        if tag == b"IDAT":
            idat += data
        index += 12 + length
    pixels = zlib.decompress(idat)
    assert len(pixels) == height * (1 + width * 3)
    return width, height


# ----------------------------------------------------------------- offline provider
class FakeProvider(Transport):
    def __init__(self, *, poll="success", task_response=None, image: bytes | None = None):
        self.calls: list[tuple[str, str]] = []
        self.poll = poll
        self.task_response = task_response
        self.image = image if image is not None else png(640, 480, (10, 200, 120))

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
            if self.task_response is not None:
                return self.task_response
            return Response(200, json.dumps({"data": {"task_id": f"p{len(self.calls)}"}}).encode())
        if url.startswith(f"https://{S3}/out"):
            return Response(200, self.image)
        if self.poll == "success":
            return Response(200, json.dumps({"data": {"error": None, "task_status": "success",
                                                      "results": {"url": f"https://{S3}/out.png"}}}).encode())
        return Response(200, json.dumps({"data": {"error": None, "task_status": "processing",
                                                  "results": None}}).encode())

    def task_calls(self):
        return [c for c in self.calls if c[1].endswith("/task/cloth-v4")]


def build(tmp_path, provider, *, latency=0.0, unit_cap=8, ledger=None):
    clock = ManualClock(START)
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="test-key", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2,
                              unit_cap=unit_cap, session_unit_cap=unit_cap,
                              ledger_path=str(ledger or (tmp_path / "units.sqlite3"))),
        clock=lambda: clock.now, transport=provider)
    service = ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE",
                            adapter=adapter, latency_seconds=latency)
    return Application(service), service, adapter, clock


def write_references(service, skus, *, cleared=True):
    refs = service.data_dir / "references"
    refs.mkdir(parents=True, exist_ok=True)
    for sku in skus:
        (refs / f"{sku}.png").write_bytes(png(512, 400, (200, 60, 60)))
        (refs / f"{sku}.rights.json").write_text(
            json.dumps({"cleared": cleared, "rights_ref": f"licensed:{sku}:demo"}), encoding="utf-8")


def multipart(payload: bytes, filename="me.png", content_type="image/png", field="photo",
              token: str = ""):
    boundary = "----swapsure-test-boundary"
    crlf = chr(13) + chr(10)
    head = (
        "--" + boundary + crlf
        + 'Content-Disposition: form-data; name="request_token"' + crlf + crlf
        + token + crlf
        + "--" + boundary + crlf
        + 'Content-Disposition: form-data; name="' + field + '"; filename="' + filename + '"' + crlf
        + "Content-Type: " + content_type + crlf + crlf
    ).encode("utf-8")
    tail = (crlf + "--" + boundary + "--" + crlf).encode("utf-8")
    return head + payload + tail, "multipart/form-data; boundary=" + boundary


def journey_to_candidates(client):
    assert client.post("/returns", {"reason": "COLOR"}).status == 303
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(f"/s/{client.session_id()}/opt-in",
                       {"request_token": client.form_token(page.text)}).status == 303
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(f"/s/{client.session_id()}/consent",
                       {"request_token": client.form_token(page.text),
                        "imageConsent": "true", "mockAcknowledged": "true"}).status == 303


def upload_photo(client, payload=None):
    page = client.get(f"/s/{client.session_id()}/consent-panel")
    token = client.form_token(page.text)
    body, ctype = multipart(payload or png(640, 480), token=token)
    return client.request("POST", f"/s/{client.session_id()}/photo", body=body, content_type=ctype)


def start_previews(client):
    page = client.get(f"/s/{client.session_id()}")
    return client.post(f"/s/{client.session_id()}/preview",
                       {"request_token": client.form_token(page.text)})


# ----------------------------------------------------------------- tests
def test_public_journey_shows_a_real_image_on_the_session_page(tmp_path):
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    write_references(service, [c.sku for c in service.get_case(client.session_id()).selection.candidates])

    assert upload_photo(client).status == 303
    assert service.photo.has(client.session_id())
    assert start_previews(client).status == 303

    page = client.get(f"/s/{client.session_id()}")
    assert page.status == 200
    for sku in [c.sku for c in service.get_case(client.session_id()).selection.candidates]:
        image = client.get(f"/s/{client.session_id()}/preview/{sku}")
        assert image.status == 200, image.text[:200]
        assert image.headers.get("content-type") == "image/png"
        width, height = decodable(image.body)
        assert (width, height) == (640, 480)
        assert f"img src=\"/s/{client.session_id()}/preview/{sku}\"" in page.text
    assert "YOUCAM RESULT" in page.text
    assert "placeholder" not in page.text.lower()
    assert adapter.units_consumed == 2 * len(provider.task_calls())


def test_browser_csp_keeps_imagery_same_origin(tmp_path):
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    write_references(service, [c.sku for c in service.get_case(client.session_id()).selection.candidates])
    upload_photo(client)
    start_previews(client)
    page = client.get(f"/s/{client.session_id()}")
    csp = page.headers.get("content-security-policy", "")
    assert "img-src 'self'" in csp
    assert "https://yce-us" not in page.text        # no external image origin in the markup


def test_partial_failure_shows_one_image_and_no_image_for_the_failure(tmp_path):
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    skus = [c.sku for c in service.get_case(client.session_id()).selection.candidates]
    write_references(service, skus)
    upload_photo(client)

    original = provider.request

    def flaky(method, url, *, headers, body=None, timeout, max_bytes=None):
        if url.endswith("/task/cloth-v4") and len(provider.task_calls()) >= 1:
            return Response(500, b"{}")
        return original(method, url, headers=headers, body=body, timeout=timeout)

    provider.request = flaky
    start_previews(client)
    page = client.get(f"/s/{client.session_id()}")
    result = service.get_case(client.session_id()).session.preview_result
    assert result is not None and result.status == "PARTIAL"
    succeeded = [s for s, h in zip(result.candidate_ids, result.media_handles) if h]
    failed = [s for s, h in zip(result.candidate_ids, result.media_handles) if not h]
    assert len(succeeded) == 1 and len(failed) == 1
    assert client.get(f"/s/{client.session_id()}/preview/{succeeded[0]}").status == 200
    assert client.get(f"/s/{client.session_id()}/preview/{failed[0]}").status == 404
    assert "refund" in page.text.lower()


def test_elapsed_deadline_completes_as_failure_and_refund_stays(tmp_path):
    provider = FakeProvider(poll="processing")
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    write_references(service, [c.sku for c in service.get_case(client.session_id()).selection.candidates])
    upload_photo(client)
    start_previews(client)
    clock.advance(seconds=400)   # past the configurable wait deadline
    page = client.get(f"/s/{client.session_id()}")
    result = service.get_case(client.session_id()).session.preview_result
    assert result is not None and result.status == "UNRESOLVED"
    # A wait deadline is not a verdict: the provider never said it failed, and the task id is
    # kept so the same task can be queried later without another generation.
    assert result.failure_code == "RECONCILIATION_PENDING"
    assert not any(result.media_handles)
    assert all(result.task_ids)
    assert "not answered" in page.text.lower() or "not a failure" in page.text.lower()
    assert "refund" in page.text.lower()


def test_lost_job_after_restart_does_not_rebill_or_lose_the_refund(tmp_path):
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    write_references(service, [c.sku for c in service.get_case(client.session_id()).selection.candidates])
    upload_photo(client)
    start_previews(client)
    tasks_before, units_before = len(provider.task_calls()), adapter.units_consumed
    service.store.delete_job(client.session_id())          # a process restarted mid-preview
    page = client.get(f"/s/{client.session_id()}")
    result = service.get_case(client.session_id()).session.preview_result
    assert result is not None and result.mode == "LIVE" and result.status == "FAILED"
    assert result.failure_code == "INTERRUPTED"
    assert len(provider.task_calls()) == tasks_before       # nothing re-created
    assert adapter.units_consumed == units_before           # nothing re-billed
    assert "refund" in page.text.lower()


def test_revocation_deletes_the_local_photo_and_blocks_delivery(tmp_path):
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    skus = [c.sku for c in service.get_case(client.session_id()).selection.candidates]
    write_references(service, skus)
    upload_photo(client)
    start_previews(client)
    client.get(f"/s/{client.session_id()}")
    assert client.get(f"/s/{client.session_id()}/preview/{skus[0]}").status == 200
    assert service.photo.has(client.session_id())

    page = client.get(f"/s/{client.session_id()}/consent-panel")
    token = client.form_token(page.text)
    assert client.post(f"/s/{client.session_id()}/revoke", {"request_token": token}).status == 303

    assert not service.photo.has(client.session_id())
    assert service.store.latest_consent(client.session_id())["action"] == "REVOKED"
    blocked = client.get(f"/s/{client.session_id()}/preview/{skus[0]}")
    assert blocked.status in (404, 409)
    assert b"PNG" not in blocked.body[:16]      # no imagery is delivered after revocation


def test_uncleared_reference_refuses_before_any_task(tmp_path):
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    write_references(service, [c.sku for c in service.get_case(client.session_id()).selection.candidates],
                     cleared=False)
    upload_photo(client)
    page = client.post(f"/s/{client.session_id()}/preview",
                       {"request_token": client.form_token(client.get(f"/s/{client.session_id()}").text)})
    assert page.status in (409, 500)
    assert provider.task_calls() == []


def test_malformed_upload_and_malformed_ports_are_typed_errors(tmp_path):
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    panel = client.get(f"/s/{client.session_id()}/consent-panel")
    body, ctype = multipart(b"not an image at all", token=client.form_token(panel.text))
    bad = client.request("POST", f"/s/{client.session_id()}/photo", body=body, content_type=ctype)
    assert bad.status == 400
    assert "INVALID_INPUT" in bad.text or "JPEG" in bad.text or "PNG" in bad.text

    # A malformed port in a configured URL is a typed refusal, not a crash.
    from adapters.youcam_live import _safe_url
    assert _safe_url("https://yce-api-01.makeupar.com:notaport/x") is False
    assert _safe_url("https://yce-api-01.makeupar.com:8443/x") is False
    assert _safe_url("https://yce-api-01.makeupar.com/x") is True
