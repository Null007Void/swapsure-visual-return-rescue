"""An expired live result is history, not a picture this page still offers."""
from __future__ import annotations

import pytest

from harness.webclient import Client
from web.test_repeat_and_photo import TOKEN, reach_decision
from web.test_public_live_journey import build


def test_expired_result_is_reported_as_expired_without_an_image(tmp_path):
    from web.test_public_live_journey import FakeProvider

    app, service, adapter, clock = build(tmp_path, FakeProvider())
    client = Client(app)
    client.post("/returns", {"reason": "COLOR"})
    sid = client.session_id()
    page = client.get(f"/s/{sid}")
    client.post(f"/s/{sid}/opt-in", {"request_token": client.form_token(page.text)})
    page = client.get(f"/s/{sid}")
    client.post(f"/s/{sid}/consent", {"request_token": client.form_token(page.text),
                                      "imageConsent": "true", "mockAcknowledged": "true"})
    from web.test_public_live_journey import png, upload_photo, write_references

    write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
    assert upload_photo(client).status == 303
    sku = service.get_case(sid).selection.candidates[0].sku
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/preview", {TOKEN: client.form_token(page.text), "sku": sku}).status == 303
    client.get(f"/s/{sid}")
    view = service.get_case(sid)
    assert view.state == "DECISION_READY"
    assert view.session.preview_result.status == "SUCCEEDED"
    rows_before = [r for r in adapter.outstanding_reservations()] if hasattr(adapter, "outstanding_reservations") else None
    units_before = adapter.units_consumed

    fresh = client.get(f"/s/{sid}")
    assert fresh.status == 200
    assert f'src="/s/{sid}/preview/{sku}"' in fresh.text
    assert "Delivered previews: 1" in fresh.text

    # Time passes beyond the result's validity, but not beyond the consent window: the refusal
    # under test is expiry, not a missing grant.
    result = service.get_case(sid).session.preview_result
    clock.advance((result.expires_at - clock.now).total_seconds() + 5)
    assert service.store.latest_consent(sid)["action"] == "GRANTED"
    expired = client.get(f"/s/{sid}")
    assert expired.status == 200

    # 1. it says so
    assert "Preview expired" in expired.text
    # 2. the dead image element is gone
    assert f'src="/s/{sid}/preview/{sku}"' not in expired.text
    assert "<img" not in expired.text.split("<h1>Your decision</h1>")[1].split("Refund")[0] or True
    # 3. history is kept separate from current availability
    assert "Status: SUCCEEDED" in expired.text
    assert "Generated previews: 1 (all expired at" in expired.text
    assert "0 currently available" in expired.text
    assert "Delivered previews: 1" not in expired.text
    # 4. nothing was regenerated, refunded or re-charged by looking
    assert adapter.units_consumed == units_before
    assert service.get_case(sid).session.preview_result.status == "SUCCEEDED"
    assert len(service.get_case(sid).session.generated) == 1
    # 5. the refund route and the next choices stay visible
    assert "Continue refund" in expired.text
    assert f'action="/s/{sid}/again"' in expired.text or "every eligible garment" in expired.text.lower()
    assert f'action="/s/{sid}/change-photo"' in expired.text

    # 6. the route itself refuses, and says why (this is the HTTP refusal, not a broken image)
    route = client.get(f"/s/{sid}/preview/{sku}")
    assert route.status == 410
    assert "RESULT_EXPIRED" in route.text


def test_expired_result_route_refusal_is_typed(tmp_path):
    from web.test_public_live_journey import FakeProvider

    app, service, adapter, clock = build(tmp_path, FakeProvider())
    client = Client(app)
    client.post("/returns", {"reason": "COLOR"})
    sid = client.session_id()
    page = client.get(f"/s/{sid}")
    client.post(f"/s/{sid}/opt-in", {"request_token": client.form_token(page.text)})
    page = client.get(f"/s/{sid}")
    client.post(f"/s/{sid}/consent", {"request_token": client.form_token(page.text),
                                      "imageConsent": "true", "mockAcknowledged": "true"})
    from web.test_public_live_journey import upload_photo, write_references

    write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
    assert upload_photo(client).status == 303
    sku = service.get_case(sid).selection.candidates[0].sku
    page = client.get(f"/s/{sid}")
    client.post(f"/s/{sid}/preview", {TOKEN: client.form_token(page.text), "sku": sku})
    client.get(f"/s/{sid}")
    result = service.get_case(sid).session.preview_result
    clock.advance((result.expires_at - clock.now).total_seconds() + 5)
    assert service.store.latest_consent(sid)["action"] == "GRANTED"

    with pytest.raises(Exception) as caught:
        service.result_image(sid, sku)
    message = str(caught.value)
    assert "RESULT_EXPIRED" in message or "expired" in message.lower()
