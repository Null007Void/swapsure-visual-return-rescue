"""Downloads are inert even against a LIVE-shaped service, and both samples upload through HTTP."""

from __future__ import annotations

import contextlib
import pathlib
import sqlite3

from harness.webclient import Client
from ui.service import ReturnService
from ui.webapp import Application

SAMPLES = ("female-sample.png", "male-sample.png")
BOUNDARY = "----swapsure-sample-boundary"


def _multipart(fields: dict[str, bytes], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{BOUNDARY}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value + b"\r\n")
    for name, (filename, payload, ctype) in files.items():
        parts.append(f"--{BOUNDARY}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
        parts.append(payload + b"\r\n")
    parts.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={BOUNDARY}"


def _ready_client(tmp_path, sku="W01-TEAL-WRAP-DRESS"):
    service = ReturnService(tmp_path, preview_mode="MOCK")
    client = Client(Application(service))
    assert client.post("/catalog/women/select", {"sku": sku}).status == 303
    sid = client.session_id()
    page = client.get(f"/s/{sid}")
    client.post(f"/s/{sid}/opt-in", {"request_token": client.form_token(page.text)})
    page = client.get(f"/s/{sid}")
    client.post(f"/s/{sid}/consent",
                {"request_token": client.form_token(page.text),
                 "imageConsent": "true", "mockAcknowledged": "true"})
    return service, client, sid


def test_both_samples_upload_through_the_http_form(tmp_path):
    service, client, sid = _ready_client(tmp_path)
    try:
        for name in SAMPLES:
            page = client.get(f"/s/{sid}")
            sample = (pathlib.Path(__file__).resolve().parents[2]
                      / "assets" / "samples" / name).read_bytes()
            body, ctype = _multipart({"request_token": client.form_token(page.text).encode()},
                                     {"photo": (name, sample, "image/png")})
            response = client.post(f"/s/{sid}/photo", body=body, headers={"Content-Type": ctype})
            assert response.status in (200, 303), (name, response.status)
            assert service.get_case(sid).photo_present is True, name
        # consent and refund remain intact after both uploads
        body_text = client.get(f"/s/{sid}").text
        assert "/refund-host" in body_text
        assert "Generate" in body_text or "preview" in body_text.lower()
    finally:
        service.close()


def test_downloads_add_no_tasks_and_no_reservations_even_in_live_shape(tmp_path):
    """A service wired to a real ledger and a transport that refuses to be called."""
    from adapters import youcam_live

    ledger_path = tmp_path / "units.sqlite3"

    class ExplodingTransport:
        def request(self, *args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("a sample download tried to reach the provider")

    settings = youcam_live.LiveSettings(
        api_key="test-key", authorization_ref="JUDGE-TEST",
        allowed_media_hosts=frozenset({"yce-api-01.makeupar.com"}),
        unit_cost=2, unit_cap=80, session_unit_cap=80,
        ledger_path=str(ledger_path),
    )
    from datetime import datetime, timezone
    adapter = youcam_live.YouCamLivePreviewAdapter(
        settings=settings, transport=ExplodingTransport(),
        clock=lambda: datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))
    service = ReturnService(tmp_path / "data", preview_mode="LIVE", adapter=adapter)
    try:
        client = Client(Application(service))
        for _ in range(3):
            for name in SAMPLES:
                response = client.get(f"/samples/{name}")
                assert response.status == 200
        assert adapter._ledger.committed_units() == 0
        with contextlib.closing(sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)) as con:
            assert con.execute("select count(*) from reservations").fetchone()[0] == 0
            assert con.execute("select count(*) from provider_tasks").fetchone()[0] == 0
    finally:
        service.close()
        close_ledger = getattr(getattr(adapter, "_ledger", None), "close", None)
        if close_ledger is not None:
            close_ledger()


def test_the_download_headers_are_attachment_with_the_png_name(tmp_path):
    service = ReturnService(tmp_path, preview_mode="MOCK")
    try:
        client = Client(Application(service))
        for name in SAMPLES:
            response = client.get(f"/samples/{name}")
            headers = {k.lower(): v for k, v in response.headers.items()}
            assert headers["content-type"] == "image/png"
            assert headers["content-disposition"] == f'attachment; filename="{name}"'
        page_probe = Client(Application(service))
        assert page_probe.get("/").status == 200
    finally:
        service.close()


def test_the_page_keeps_the_download_attribute_and_states_the_consent_order(tmp_path):
    service, client, sid = _ready_client(tmp_path)
    try:
        body = client.get(f"/s/{sid}").text
        for name in SAMPLES:
            assert f'download="{name}"' in body, name
        assert "Downloading one needs no consent" in body
        assert "the image-consent step applies to every upload" in body
        assert "Pressing Generate is what sends the image" in body
    finally:
        service.close()
