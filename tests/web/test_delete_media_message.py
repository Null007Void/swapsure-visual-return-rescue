"""The delete-media page must report the real mode and a truthful deletion state."""
from __future__ import annotations

from harness.webclient import Client
from web.helpers import TOKEN, reach_decision
from web.test_public_live_journey import build, journey_to_candidates, png, upload_photo, write_references


def _mock_service(tmp_path):
    from ui.service import ReturnService
    from ui.webapp import Application
    return ReturnService(tmp_path / "data", preview_mode="MOCK"), Application


def test_mock_deletion_page_reports_mock_and_completion(tmp_path):
    from ui.service import ReturnService
    from ui.webapp import Application

    service = ReturnService(tmp_path / "data", preview_mode="MOCK")
    try:
        client = Client(Application(service))
        reach_decision(client)
        sid = client.session_id()
        page = client.get(f"/s/{sid}")
        response = client.post(f"/s/{sid}/delete-media", {TOKEN: client.form_token(page.text)})
        assert response.status == 200
        assert "Current mode: MOCK" in response.text
        assert "Local deletion completed" in response.text
        assert "not applicable in MOCK" not in response.text
    finally:
        service.close()


def test_live_deletion_page_reports_live_and_provider_retention(tmp_path):
    from web.test_public_live_journey import FakeProvider

    app, service, adapter, clock = build(tmp_path, FakeProvider())
    client = Client(app)
    journey_to_candidates(client)
    sid = client.session_id()
    write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
    assert upload_photo(client).status == 303
    sku = service.get_case(sid).selection.candidates[0].sku
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/preview", {TOKEN: client.form_token(page.text), "sku": sku}).status == 303
    client.get(f"/s/{sid}")

    page = client.get(f"/s/{sid}")
    response = client.post(f"/s/{sid}/delete-media", {TOKEN: client.form_token(page.text)})
    assert response.status == 200
    assert "Current mode: LIVE" in response.text
    assert "Mode: MOCK" not in response.text
    assert "retention" in response.text
    assert "not deleted by this action" in response.text or "governed by its own retention" in response.text
