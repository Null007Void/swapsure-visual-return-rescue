"""Optional judge sample photos: downloads must be inert, PNG-only and upload-compatible."""

from __future__ import annotations

import hashlib
import pathlib

from harness.webclient import Client, multipart
from ui.service import ReturnService
from ui.webapp import Application

FEMALE_SHA = "dcde1267dd194793b6af5037c214cca1fb3541105f5bba920d632bc2b15511f4"
MALE_SHA = "7dbfefb799e32c33a9c60c16ec12e36f34ef468b98285e6d97ad45e8b3f197bb"


def app_for(tmp_path):
    return ReturnService(tmp_path, preview_mode="MOCK")


def test_both_samples_download_as_png_with_correct_names(tmp_path):
    service = app_for(tmp_path)
    try:
        client = Client(Application(service))
        for name, digest in (("female-sample.png", FEMALE_SHA), ("male-sample.png", MALE_SHA)):
            response = client.get(f"/samples/{name}")
            assert response.status == 200, (name, response.status)
            headers = {k.lower(): v for k, v in response.headers.items()}
            assert headers.get("content-type") == "image/png"
            assert headers.get("content-disposition") == f'inline; filename="{name}"'
            body = response.body if isinstance(response.body, bytes) else bytes(response.body)
            assert body[:8] == bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
            assert hashlib.sha256(body).hexdigest() == digest, name
    finally:
        service.close()


def test_an_unknown_sample_is_404_and_changes_nothing(tmp_path):
    service = app_for(tmp_path)
    try:
        client = Client(Application(service))
        assert client.get("/samples/other-sample.png").status == 404
        assert client.get("/samples/../service.py").status in (404, 403)
        # no session was created by any of that
        assert not list((tmp_path / "photos").glob("*")) if (tmp_path / "photos").exists() else True
    finally:
        service.close()


def test_the_sample_links_appear_beside_the_upload_control(tmp_path):
    service = app_for(tmp_path)
    try:
        client = Client(Application(service))
        assert client.post("/catalog/women/select", {"sku": "W01-TEAL-WRAP-DRESS"}).status == 303
        sid = client.session_id()
        page = client.get(f"/s/{sid}")
        client.post(f"/s/{sid}/opt-in", {"request_token": client.form_token(page.text)})
        page = client.get(f"/s/{sid}")
        client.post(f"/s/{sid}/consent", {"request_token": client.form_token(page.text),
                                          "imageConsent": "true", "mockAcknowledged": "true"})
        body = client.get(f"/s/{sid}").text
        assert 'accept="PNG"' not in body  # the control still declares image/png, not text
        assert 'accept="image/png"' in body
        assert "Female sample" in body and "Male sample" in body
        assert "/samples/female-sample.png" in body and "/samples/male-sample.png" in body
        assert "PNG only" in body
    finally:
        service.close()


def test_downloading_a_sample_creates_no_task_and_no_reservation(tmp_path):
    service = app_for(tmp_path)
    try:
        client = Client(Application(service))
        assert client.post("/catalog/women/select", {"sku": "W01-TEAL-WRAP-DRESS"}).status == 303
        sid = client.session_id()
        before = service.get_case(sid)
        for _ in range(3):
            assert client.get("/samples/female-sample.png").status == 200
            assert client.get("/samples/male-sample.png").status == 200
        after = service.get_case(sid)
        assert after.state == before.state
        assert after.selection == before.selection
        assert not list((tmp_path / "photos").glob("*")) if (tmp_path / "photos").exists() else True
    finally:
        service.close()


def test_a_downloaded_sample_uploads_through_the_normal_control(tmp_path):
    """The bytes a judge downloads are accepted by the same code path the upload form uses."""
    service = app_for(tmp_path)
    try:
        client = Client(Application(service))
        assert client.post("/catalog/women/select", {"sku": "W01-TEAL-WRAP-DRESS"}).status == 303
        sid = client.session_id()
        page = client.get(f"/s/{sid}")
        client.post(f"/s/{sid}/opt-in", {"request_token": client.form_token(page.text)})
        page = client.get(f"/s/{sid}")
        client.post(f"/s/{sid}/consent", {"request_token": client.form_token(page.text),
                                          "imageConsent": "true", "mockAcknowledged": "true"})
        sample = (pathlib.Path(__file__).resolve().parents[2]
                  / "assets" / "samples" / "female-sample.png").read_bytes()
        service.submit_photo(sid, sample, "image/png")
        assert service.get_case(sid).photo_present is True
        assert "/refund-host" in client.get(f"/s/{sid}").text
    finally:
        service.close()
