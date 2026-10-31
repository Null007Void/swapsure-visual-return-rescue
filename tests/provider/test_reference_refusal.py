"""A missing rights reference is a typed refusal, not an internal error."""
from __future__ import annotations

import pathlib
import tempfile

from adapters.youcam_live import LiveSettings, YouCamLivePreviewAdapter
from harness.clock import ManualClock
from harness.webclient import Client
from ui.service import ReturnService
from ui.webapp import Application

from tests.web.test_public_live_journey import (S3, START, FakeProvider, journey_to_candidates,
                                                upload_photo)


def test_missing_reference_is_a_typed_refusal_and_spends_nothing():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="no-reference-"))
    clock = ManualClock(START)
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="k", authorization_ref="A",
                              allowed_media_hosts=frozenset({"yce-api-01.makeupar.com", S3}),
                              unit_cost=2, unit_cap=8, session_unit_cap=8,
                              ledger_path=str(tmp / "units.sqlite3")),
        clock=lambda: clock.now, transport=FakeProvider())
    service = ReturnService(tmp / "data", clock=clock, preview_mode="LIVE", adapter=adapter)
    try:
        client = Client(Application(service))
        journey_to_candidates(client)
        upload_photo(client)
        page = client.get(f"/s/{client.session_id()}")
        response = client.post(f"/s/{client.session_id()}/preview",
                               {"request_token": client.form_token(page.text)})
        assert response.status == 409
        assert "INVALID_INPUT" in response.text
        assert "INTERNAL_ERROR" not in response.text
        assert adapter.tasks_created == 0
        assert adapter.units_consumed == 0
    finally:
        service.close()
