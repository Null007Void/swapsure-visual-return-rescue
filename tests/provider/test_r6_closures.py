"""The three exact regressions for the 1.6 closures.

R1  the consent grant page states the real LIVE processing before any upload
R2  cached/uncached result images obey consent validity, result expiry and deletion tombstones,
    and the adapter refuses polling without consent
R3  image validation decodes the whole payload, enforces the documented minimums, and gives image
    downloads their own size ceiling
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import adapters.youcam_live as live
from adapters.youcam_live import (MAX_IMAGE_BYTES, MAX_RESPONSE_BYTES, LiveSettings, Response,
                                  Transport, YouCamLivePreviewAdapter)
from adapters.youcam_stub import AdapterError, FailureCode
from harness.clock import ManualClock
from harness.webclient import Client
from tests.provider.images import (jpeg, jpeg_header_only, png, png_header_only, png_noise,
                                   png_truncated)
from ui.service import ReturnService
from ui.webapp import Application

HOST = "yce-api-01.makeupar.com"
S3 = "yce-us.s3-accelerate.amazonaws.com"
START = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


class FakeProvider(Transport):
    def __init__(self, *, image: bytes | None = None):
        self.calls: list[tuple[str, str]] = []
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
            return Response(200, json.dumps({"data": {"task_id": "p1"}}).encode())
        if url.startswith(f"https://{S3}/out"):
            return Response(200, self.image)
        return Response(200, json.dumps({"data": {"error": None, "task_status": "success",
                                                  "results": {"url": f"https://{S3}/out.png"}}}).encode())

    def image_fetches(self):
        return [c for c in self.calls if c[1].startswith(f"https://{S3}/out")]

    def task_calls(self):
        return [c for c in self.calls if c[1].endswith("/task/cloth-v4")]


def build(tmp_path, provider, *, clock=None):
    clock = clock or ManualClock(START)
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="k", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2, unit_cap=8,
                              session_unit_cap=8, ledger_path=str(tmp_path / "units.sqlite3")),
        clock=lambda: clock.now, transport=provider)
    service = ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE", adapter=adapter)
    return Application(service), service, adapter, clock


def write_references(service, skus, width=512, height=400):
    refs = service.data_dir / "references"
    refs.mkdir(parents=True, exist_ok=True)
    for sku in skus:
        (refs / f"{sku}.png").write_bytes(png(width, height, (200, 60, 60)))
        (refs / f"{sku}.rights.json").write_text(
            json.dumps({"cleared": True, "rights_ref": f"licensed:{sku}"}), encoding="utf-8")


def multipart(payload: bytes, token: str) -> tuple[bytes, str]:
    boundary = "----regression"
    crlf = chr(13) + chr(10)
    head = ("--" + boundary + crlf
            + 'Content-Disposition: form-data; name="request_token"' + crlf + crlf + token + crlf
            + "--" + boundary + crlf
            + 'Content-Disposition: form-data; name="photo"; filename="me.png"' + crlf
            + "Content-Type: image/png" + crlf + crlf).encode()
    return head + payload + (crlf + "--" + boundary + "--" + crlf).encode(), \
        "multipart/form-data; boundary=" + boundary


def to_candidates(client):
    assert client.post("/returns", {"reason": "COLOR"}).status == 303
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(f"/s/{client.session_id()}/opt-in",
                       {"request_token": client.form_token(page.text)}).status == 303


def grant_and_upload(client, photo=None):
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(f"/s/{client.session_id()}/consent",
                       {"request_token": client.form_token(page.text),
                        "imageConsent": "true", "mockAcknowledged": "true"}).status == 303
    panel = client.get(f"/s/{client.session_id()}/consent-panel")
    body, ctype = multipart(photo or png(700, 520), client.form_token(panel.text))
    assert client.request("POST", f"/s/{client.session_id()}/photo", body=body,
                          content_type=ctype).status == 303


def start_previews(client):
    page = client.get(f"/s/{client.session_id()}")
    return client.post(f"/s/{client.session_id()}/preview",
                       {"request_token": client.form_token(page.text)})


def live_session(tmp_path, provider, *, clock=None):
    app, service, adapter, clock = build(tmp_path, provider, clock=clock)
    client = Client(app)
    to_candidates(client)
    grant_and_upload(client)                            # consent, then the shopper photo
    sid = client.session_id()
    write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
    assert start_previews(client).status == 303
    client.get(f"/s/{sid}")                             # completes the provider tasks
    return client, service, adapter, clock


# ------------------------------------------------------------------ R1
def test_r1_live_consent_page_states_provider_processing_and_retention(tmp_path):
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    to_candidates(client)

    page = client.get(f"/s/{client.session_id()}")       # the grant page, before any upload
    text = page.text
    assert page.status == 200
    assert "Consent for visual previews" in text
    assert "YouCam" in text and "Perfect Corp" in text
    assert "leave this server" in text                   # photo is sent to the provider
    assert "30 days" in text and "24 hours" in text      # the unresolved retention conflict
    assert "not settled" in text or "conflict" in text
    assert "not proof of fit" in text
    assert "MOCK placeholder, not a YouCam result" not in text   # no MOCK-only grant text in LIVE
    assert not service.photo.has(client.session_id())     # nothing uploaded yet


def test_r1_mock_consent_page_keeps_its_offline_wording(tmp_path):
    clock = ManualClock(START)
    service = ReturnService(tmp_path / "m", clock=clock)
    client = Client(Application(service))
    to_candidates(client)
    text = client.get(f"/s/{client.session_id()}").text
    assert "MOCK placeholder, not a YouCam result" in text
    assert "does not process a photo of you" in text
    assert "30 days" not in text


# ------------------------------------------------------------------ R2
def test_r2_cached_and_uncached_images_obey_expiry_and_consent(tmp_path, monkeypatch):
    provider = FakeProvider()
    client, service, adapter, clock = live_session(tmp_path, provider)
    sku = service.get_case(client.session_id()).selection.candidates[0].sku
    sid = client.session_id()

    first = client.get(f"/s/{sid}/preview/{sku}")
    second = client.get(f"/s/{sid}/preview/{sku}")
    assert (first.status, second.status) == (200, 200)
    assert len(provider.image_fetches()) == 1            # the second call is served from cache

    # Result expiry: past the delivery window, cached or not, nothing is served or refetched.
    clock.advance(seconds=int(live.RESULT_VALIDITY.total_seconds()) + 60)
    assert client.get(f"/s/{sid}/preview/{sku}").status == 410
    assert len(provider.image_fetches()) == 1

    # Consent expiry with a long result validity: the grant, not the cache, is the blocker.
    monkeypatch.setattr(live, "RESULT_VALIDITY", live.timedelta(hours=4))
    monkeypatch.setattr("ui.service.CONSENT_WINDOW", live.timedelta(minutes=1))
    clock.advance(seconds=120)
    assert client.get(f"/s/{sid}/preview/{sku}").status == 409
    assert len(provider.image_fetches()) == 1


def test_r2_deletion_and_revocation_clear_cache_and_source_photo(tmp_path):
    provider = FakeProvider()
    client, service, adapter, clock = live_session(tmp_path, provider)
    sku = service.get_case(client.session_id()).selection.candidates[0].sku
    sid = client.session_id()
    assert client.get(f"/s/{sid}/preview/{sku}").status == 200
    fetches = len(provider.image_fetches())
    assert service.photo.has(sid)

    # Delete through the public action: photo gone, cache cleared, no refetch, tombstone recorded.
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/delete-media",
                       {"request_token": client.form_token(page.text)}).status in (303, 200)
    assert not service.photo.has(sid)
    assert client.get(f"/s/{sid}/preview/{sku}").status == 410
    assert len(provider.image_fetches()) == fetches
    assert sid in service._result_tombstones


def test_r2_revocation_blocks_cached_images_and_polling(tmp_path):
    provider = FakeProvider()
    client, service, adapter, clock = live_session(tmp_path, provider)
    sid = client.session_id()
    sku = service.get_case(sid).selection.candidates[0].sku
    assert client.get(f"/s/{sid}/preview/{sku}").status == 200

    page = client.get(f"/s/{sid}/consent-panel")
    assert client.post(f"/s/{sid}/revoke",
                       {"request_token": client.form_token(page.text)}).status == 303
    assert not service.photo.has(sid)
    fetches = len(provider.image_fetches())
    blocked = client.get(f"/s/{sid}/preview/{sku}")
    assert blocked.status in (404, 410)                 # delivery refused after revocation
    assert len(provider.image_fetches()) == fetches     # and nothing was refetched from the provider

    # The adapter itself now refuses polling when no consent remains, even for a terminal task.
    task = next(iter(adapter._tasks.values()))
    adapter._consent.clear()
    with pytest.raises(AdapterError) as exc:
        adapter.poll_preview(task.task_id, session_id=sid)
    assert exc.value.code is FailureCode.CONSENT_REQUIRED


# ------------------------------------------------------------------ R3
def test_r3_header_only_and_truncated_images_are_refused():
    for payload in (png_header_only(), png_truncated()):
        with pytest.raises(AdapterError) as exc:
            live.sniff_image(payload, "image/png")
        assert exc.value.code is FailureCode.INVALID_INPUT
    with pytest.raises(AdapterError):
        live.sniff_image(jpeg_header_only(), "image/png")
    # a structurally complete JPEG still passes
    with pytest.raises(AdapterError) as jpeg_exc:
        live.sniff_image(jpeg(640, 480), "image/jpeg")
    assert jpeg_exc.value.code is FailureCode.INVALID_INPUT      # PNG-only qualification path


def test_r3_documented_minimums_reject_384_square_but_accept_provider_sizes():
    with pytest.raises(AdapterError) as exc:
        live.sniff_image(png(384, 384), "image/png")
    assert "512" in str(exc.value)
    with pytest.raises(AdapterError):
        live.sniff_image(png(511, 400), "image/png")     # long side below 512
    assert live.sniff_image(png(512, 384), "image/png") == (512, 384)
    assert live.sniff_image(png(1024, 768), "image/png") == (1024, 768)


def test_r3_image_download_has_its_own_ceiling(tmp_path):
    assert MAX_IMAGE_BYTES > MAX_RESPONSE_BYTES
    big = png_noise(900, 700)
    assert len(big) > MAX_RESPONSE_BYTES                 # larger than the JSON response ceiling
    provider = FakeProvider(image=big)
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="k", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2, unit_cap=8,
                              session_unit_cap=8, ledger_path=str(tmp_path / "u.sqlite3")),
        clock=lambda: START, transport=provider)
    payload, content_type = adapter.fetch_result_image(
        f"https://{S3}/out.png", deadline=datetime(2026, 10, 30, 9, 5, tzinfo=timezone.utc))
    assert payload == big and content_type == "image/png"
