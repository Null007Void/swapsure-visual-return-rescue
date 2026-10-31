"""R8-1: provider completion is not a delivered preview until the bytes validate.

These run the real LIVE adapter against an offline fixture. Every page assertion is about what a
shopper sees, not about provider status words.
"""
from __future__ import annotations

import struct

from harness.webclient import Client

from tests.web.test_public_live_journey import (S3, FakeProvider, build, journey_to_candidates,
                                                start_previews, upload_photo, write_references)


def jpeg(width: int = 640, height: int = 480) -> bytes:
    """A JPEG container whose entropy data is not decodable: framing alone must not pass."""
    return (bytes.fromhex("ffd8ffe000104a46494600010100000100010000")
            + bytes.fromhex("ffdb004300") + bytes([16] * 64)
            + bytes.fromhex("ffc0000b08") + struct.pack(">HH", height, width)
            + bytes([3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1])
            + bytes.fromhex("ffc4001f00") + bytes([0] * 16) + b"\x00" * 12
            + bytes.fromhex("ffda0008010100003f00") + b"\x00" * (width * height)
            + bytes.fromhex("ffd9"))


def run_journey(tmp_path, provider):
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    write_references(service, [c.sku for c in service.get_case(client.session_id()).selection.candidates])
    assert upload_photo(client).status == 303
    assert start_previews(client).status == 303
    sid = client.session_id()
    for _ in range(6):
        page = client.get(f"/s/{sid}")
        if "Your decision" in page.text:
            break
        clock.advance(seconds=1)
    return client, service, adapter, sid, page


def test_valid_png_is_delivered_and_paints(tmp_path):
    provider = FakeProvider()
    client, service, adapter, sid, page = run_journey(tmp_path, provider)
    result = service.get_case(sid).session.preview_result
    assert result is not None and result.status in ("SUCCEEDED", "PARTIAL")
    assert "SUCCEEDED" in page.text
    assert page.text.count("<img ") == len([h for h in result.media_handles if h])
    sku = result.candidate_ids[result.media_handles.index(next(h for h in result.media_handles if h))]
    image = client.get(f"/s/{sid}/preview/{sku}")
    assert image.status == 200
    assert image.headers.get("content-type") == "image/png"
    assert image.body[:8] == b"\x89PNG\r\n\x1a\n"
    assert "refund" in page.text.lower()


def test_undecodable_jpeg_bytes_are_refused(tmp_path):
    provider = FakeProvider(image=jpeg())
    client, service, adapter, sid, page = run_journey(tmp_path, provider)
    result = service.get_case(sid).session.preview_result
    assert result is not None
    assert result.status == "FAILED"
    assert result.failure_code == "INVALID_RESULT"
    assert not any(result.media_handles)                     # nothing is advertised
    assert result.task_ids and all(result.task_ids)          # the spend stays attributable
    assert page.text.count("<img ") == 0                     # no broken image tag
    assert "cannot decode or display" in page.text.lower()
    assert "UNSUPPORTED" not in page.text                    # plain words, no invented code
    assert "refund" in page.text.lower()
    assert adapter.units_consumed == 4                       # two candidates, both spent, honestly


def test_malformed_image_bytes_are_refused_the_same_way(tmp_path):
    provider = FakeProvider(image=b"<html><body>not an image</body></html>")
    client, service, adapter, sid, page = run_journey(tmp_path, provider)
    result = service.get_case(sid).session.preview_result
    assert result.status == "FAILED" and result.failure_code == "INVALID_RESULT"
    assert page.text.count("<img ") == 0
    assert "cannot decode or display" in page.text.lower()
    assert "refund" in page.text.lower()


def test_repeated_refreshes_and_tabs_do_not_recreate_or_recharge(tmp_path):
    provider = FakeProvider()
    client, service, adapter, sid, page = run_journey(tmp_path, provider)
    created = len(provider.task_calls())
    units = adapter.units_consumed
    tab = Client(client.app, cookies=dict(client.cookies))
    for _ in range(3):
        client.get(f"/s/{sid}")
        tab.get(f"/s/{sid}")
    assert len(provider.task_calls()) == created
    assert adapter.units_consumed == units
    assert service.get_case(sid).state == "DECISION_READY"
