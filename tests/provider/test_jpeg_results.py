"""Provider results: PNG and JPEG both render; bad bytes never do.

Images here are produced by Pillow, so a "valid JPEG" is genuinely decodable. The provider is an
offline fixture: simulated execution, no key, no units.
"""
from __future__ import annotations

import io

from PIL import Image

from harness.webclient import Client

from tests.web.test_public_live_journey import (FakeProvider, build, journey_to_candidates,
                                                start_previews, upload_photo, write_references)


def image_bytes(fmt: str = "JPEG", size=(640, 480), colour=(30, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, fmt, **({"quality": 88} if fmt == "JPEG" else {}))
    return buf.getvalue()


def png_pixels(payload: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(payload)) as img:
        img.load()
        return img.size


def oversized_jpeg() -> bytes:
    """A small file whose SOF header claims 20000x20000: the pixel bound must refuse it."""
    data = bytearray(image_bytes("JPEG", (640, 480)))
    marker = data.find(b"\xff\xc0")
    assert marker != -1
    data[marker + 5:marker + 7] = (20000).to_bytes(2, "big")     # height
    data[marker + 7:marker + 9] = (20000).to_bytes(2, "big")     # width
    return bytes(data)


def journey(tmp_path, provider):
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    write_references(service, [c.sku for c in service.get_case(client.session_id()).selection.candidates])
    assert upload_photo(client).status == 303
    assert start_previews(client).status == 303
    sid = client.session_id()
    page = None
    for _ in range(6):
        page = client.get(f"/s/{sid}")
        if "Your decision" in page.text:
            break
        clock.advance(seconds=1)
    return client, service, adapter, sid, page


def result_of(service, sid):
    return service.get_case(sid).session.preview_result


def test_valid_jpeg_is_delivered_as_jpeg(tmp_path):
    jpeg = image_bytes("JPEG")
    client, service, adapter, sid, page = journey(tmp_path, FakeProvider(image=jpeg))
    result = result_of(service, sid)
    assert result.status == "SUCCEEDED"
    assert page.text.count("<img ") == len([h for h in result.media_handles if h])
    sku = result.candidate_ids[0]
    response = client.get(f"/s/{sid}/preview/{sku}")
    assert response.status == 200
    assert response.headers.get("content-type") == "image/jpeg"
    assert response.body[:2] == b"\xff\xd8"
    width, height = png_pixels(response.body.replace(b"\xff\xd8", b"\xff\xd8", 1))
    assert (width, height) == (640, 480)
    assert "refund" in page.text.lower()


def test_valid_png_still_delivered_as_png(tmp_path):
    png = image_bytes("PNG")
    client, service, adapter, sid, page = journey(tmp_path, FakeProvider(image=png))
    result = result_of(service, sid)
    assert result.status == "SUCCEEDED"
    response = client.get(f"/s/{sid}/preview/{result.candidate_ids[0]}")
    assert response.headers.get("content-type") == "image/png"
    assert response.body[:8] == b"\x89PNG\r\n\x1a\n"


def test_truncated_jpeg_is_refused(tmp_path):
    truncated = image_bytes("JPEG")[: int(len(image_bytes("JPEG")) * 0.6)]
    client, service, adapter, sid, page = journey(tmp_path, FakeProvider(image=truncated))
    result = result_of(service, sid)
    assert result.status == "FAILED" and result.failure_code == "INVALID_RESULT"
    assert not any(result.media_handles)
    assert page.text.count("<img ") == 0
    assert "cannot decode or display" in page.text.lower()
    assert "refund" in page.text.lower()


def test_png_magic_in_front_of_jpeg_bytes_is_refused(tmp_path):
    disguised = b"\x89PNG\r\n\x1a\n" + image_bytes("JPEG")
    client, service, adapter, sid, page = journey(tmp_path, FakeProvider(image=disguised))
    result = result_of(service, sid)
    assert result.status == "FAILED" and result.failure_code == "INVALID_RESULT"
    assert page.text.count("<img ") == 0


def test_oversized_dimensions_are_refused(tmp_path):
    client, service, adapter, sid, page = journey(tmp_path, FakeProvider(image=oversized_jpeg()))
    result = result_of(service, sid)
    assert result.status == "FAILED" and result.failure_code == "INVALID_RESULT"
    assert page.text.count("<img ") == 0
    assert adapter.units_consumed == 4          # the spend is kept and reported honestly


def test_random_bytes_with_image_magic_are_refused(tmp_path):
    client, service, adapter, sid, page = journey(tmp_path, FakeProvider(image=b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096))
    result = result_of(service, sid)
    assert result.status == "FAILED" and result.failure_code == "INVALID_RESULT"
    assert page.text.count("<img ") == 0


def test_cached_delivery_does_not_refetch(tmp_path):
    provider = FakeProvider(image=image_bytes("JPEG"))
    client, service, adapter, sid, page = journey(tmp_path, provider)
    sku = result_of(service, sid).candidate_ids[0]
    fetches_before = len([c for c in provider.calls if "out" in str(c[1])])
    first = client.get(f"/s/{sid}/preview/{sku}")
    second = client.get(f"/s/{sid}/preview/{sku}")
    assert first.body == second.body
    assert len([c for c in provider.calls if "out" in str(c[1])]) == fetches_before
    assert adapter.units_consumed == 4


def test_revoked_consent_stops_delivery(tmp_path):
    client, service, adapter, sid, page = journey(tmp_path, FakeProvider(image=image_bytes("JPEG")))
    sku = result_of(service, sid).candidate_ids[0]
    assert client.get(f"/s/{sid}/preview/{sku}").status == 200
    panel = client.get(f"/s/{sid}/consent-panel")
    assert client.post(f"/s/{sid}/revoke", {"request_token": client.form_token(panel.text)}).status == 303
    after = client.get(f"/s/{sid}/preview/{sku}")
    assert after.status in (403, 404, 409, 410)      # refused: no bytes after revocation
    assert after.body[:4] != b"%PDF" and after.body[:8] != bytes([137, 80, 78, 71, 13, 10, 26, 10])
    assert after.body[:2] != bytes([255, 216])


def test_repeat_refresh_after_jpeg_delivery_creates_nothing(tmp_path):
    provider = FakeProvider(image=image_bytes("JPEG"))
    client, service, adapter, sid, page = journey(tmp_path, provider)
    created, units = len(provider.task_calls()), adapter.units_consumed
    for _ in range(3):
        client.get(f"/s/{sid}")
    assert len(provider.task_calls()) == created
    assert adapter.units_consumed == units
