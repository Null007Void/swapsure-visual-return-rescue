"""Repeat try-on, photo change, and photo-bound results — driven through the public routes.

The provider is the offline fixture from ``test_public_live_journey``: simulated execution, no
network, no credential, no unit.
"""
from __future__ import annotations

import contextlib
import sqlite3


import pytest

from harness.webclient import Client
from web.test_public_live_journey import (
    FakeProvider,
    build,
    journey_to_candidates,
    multipart,
    png,
    upload_photo,
    write_references,
)

TOKEN = "request_token"


def _token(client) -> str:
    return client.form_token(client.get(f"/s/{client.session_id()}").text)


def reach_decision(tmp_path, provider=None, *, sku: str | None = None):
    """Journey to a first delivered preview, with an explicit single-garment plan."""
    provider = provider or FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    sid = client.session_id()
    write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
    assert upload_photo(client).status == 303
    chosen = sku or service.get_case(sid).selection.candidates[0].sku
    started = client.post(f"/s/{sid}/preview", {TOKEN: _token(client), "sku": chosen})
    assert started.status == 303, started.text[:400]
    page = client.get(f"/s/{sid}")
    assert service.get_case(sid).state == "DECISION_READY", service.get_case(sid).state
    return client, service, provider, adapter, clock, chosen, page


def ledger_rows(path) -> list[tuple[str, str, int]]:
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
        return [tuple(row) for row in con.execute(
            "SELECT operation_id, state, units FROM reservations ORDER BY created_at")]


# --------------------------------------------------------------------- try another
def test_try_another_keeps_photo_and_consent_and_spends_nothing(tmp_path):
    client, service, provider, adapter, clock, chosen, _ = reach_decision(tmp_path)
    sid = client.session_id()
    before = service.get_case(sid)
    tasks_before = len(provider.task_calls())
    rows_before = ledger_rows(tmp_path / "units.sqlite3")

    response = client.post(f"/s/{sid}/again", {TOKEN: _token(client)})
    assert response.status == 303
    after = service.get_case(sid)

    assert after.state == "CANDIDATES_READY"
    assert after.session.photo_version == before.session.photo_version
    assert after.session.consent_version == before.session.consent_version
    assert after.session.preview_result is None
    # The photo does not have to be uploaded again.
    assert service.photo.has(sid)
    # Re-opening the choice creates no provider task and no reservation.
    assert len(provider.task_calls()) == tasks_before
    assert ledger_rows(tmp_path / "units.sqlite3") == rows_before
    # The garment already tried with this photo is still recorded as spent.
    assert f"{before.session.photo_version}:{chosen}" in after.session.generated


def test_second_garment_on_the_same_photo_is_a_separate_task(tmp_path):
    """Two garments from the bundled catalogue, one photo, two explicitly confirmed generations."""
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    assert client.post("/catalog/women/select", {"sku": "W02-IVORY-LACE-BLOUSE"}).status == 303
    sid = client.session_id()
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/opt-in", {TOKEN: client.form_token(page.text)}).status == 303
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/consent", {
        TOKEN: client.form_token(page.text), "imageConsent": "true",
        "mockAcknowledged": "true"}).status == 303
    assert upload_photo(client).status == 303

    first = "W02-IVORY-LACE-BLOUSE"
    assert client.post(f"/s/{sid}/preview", {TOKEN: _token(client), "sku": first}).status == 303
    client.get(f"/s/{sid}")          # the pending page polls, then shows the decision
    assert service.get_case(sid).state == "DECISION_READY"
    photo_version = service.get_case(sid).session.photo_version

    assert client.post(f"/s/{sid}/again", {TOKEN: _token(client)}).status == 303
    after = service.get_case(sid)
    assert after.state == "CANDIDATES_READY"
    assert after.session.photo_version == photo_version
    assert service.photo.has(sid)
    remaining = [c.sku for c in after.selection.candidates if c.sku != first]
    assert remaining, "the bundled tops collection should offer a second garment"
    second = remaining[0]
    assert client.post(f"/s/{sid}/preview", {TOKEN: _token(client), "sku": second}).status == 303
    client.get(f"/s/{sid}")

    rows = ledger_rows(tmp_path / "units.sqlite3")
    assert len(rows) == 2, rows
    assert len({row[0] for row in rows}) == 2, "each garment is its own operation"
    assert all(row[1] == "CONFIRMED" and row[2] == 2 for row in rows)
    assert len(provider.task_calls()) == 2
    view = service.get_case(sid)
    assert view.state == "DECISION_READY"
    assert set(view.session.preview_result.candidate_ids) == {second}
    assert view.session.preview_result.photo_version == photo_version


def test_same_photo_and_garment_is_never_charged_twice(tmp_path):
    client, service, provider, adapter, clock, first, _ = reach_decision(tmp_path)
    sid = client.session_id()
    rows_before = ledger_rows(tmp_path / "units.sqlite3")
    assert client.post(f"/s/{sid}/again", {TOKEN: _token(client)}).status == 303

    again = client.post(f"/s/{sid}/preview", {TOKEN: _token(client), "sku": first})
    assert again.status == 409
    assert "ALREADY_GENERATED" in again.text
    assert ledger_rows(tmp_path / "units.sqlite3") == rows_before
    assert len(provider.task_calls()) == 1
    assert service.get_case(sid).state == "CANDIDATES_READY"


def test_try_another_is_refused_before_a_result(tmp_path):
    app, service, provider = None, None, None
    from ui.service import ReturnService
    from ui.webapp import Application

    service = ReturnService(tmp_path / "data", preview_mode="MOCK")
    client = Client(Application(service))
    try:
        assert client.post("/returns", {"reason": "COLOR"}).status == 303
        response = client.post(f"/s/{client.session_id()}/again", {TOKEN: _token(client)})
        assert response.status == 409
        assert service.get_case(client.session_id()).state != "CANDIDATES_READY"
    finally:
        service.close()


# --------------------------------------------------------------------- change photo
def test_change_photo_discards_photo_consent_and_result(tmp_path):
    client, service, provider, adapter, clock, chosen, page = reach_decision(tmp_path)
    sid = client.session_id()
    assert service.photo.has(sid)

    response = client.post(f"/s/{sid}/change-photo", {TOKEN: _token(client)})
    assert response.status == 303
    assert response.headers.get("location", "").endswith(f"/s/{sid}")

    view = service.get_case(sid)
    assert view.state == "CONSENT_PENDING"
    assert view.session.photo_version is None
    assert view.session.consent_version is None
    assert view.session.preview_result is None
    assert not service.photo.has(sid), "the old photo is deleted, not kept for the new person"
    latest = service.store.latest_consent(sid)
    assert latest["action"] == "WITHDRAWN"
    # Nothing was spent by changing the photo.
    assert len(ledger_rows(tmp_path / "units.sqlite3")) == 1


def test_new_photo_is_a_new_operation_never_a_reuse(tmp_path):
    client, service, provider, adapter, clock, first, _ = reach_decision(tmp_path)
    sid = client.session_id()
    assert client.post(f"/s/{sid}/change-photo", {TOKEN: _token(client)}).status == 303

    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/consent", {
        TOKEN: client.form_token(page.text), "imageConsent": "true",
        "mockAcknowledged": "true"}).status == 303
    write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
    assert upload_photo(client, png(640, 480, (40, 40, 200))).status == 303
    started = client.post(f"/s/{sid}/preview", {TOKEN: _token(client), "sku": first})
    assert started.status == 303, started.text[:400]
    client.get(f"/s/{sid}")

    rows = ledger_rows(tmp_path / "units.sqlite3")
    assert len(rows) == 2
    assert len({row[0] for row in rows}) == 2, "a different photo is a different task"
    assert len(provider.task_calls()) == 2

    # The new photo's own result must be servable: changing photo leaves no session tombstone.
    image = client.get(f"/s/{sid}/preview/{first}")
    assert image.status == 200, image.text[:200]
    assert image.headers.get("content-type") == "image/png"
    assert image.body[1:4] == bytes("PNG", "ascii")


def test_consent_panel_reports_the_real_mode(tmp_path):
    client, service, provider, adapter, clock, chosen, _ = reach_decision(tmp_path)
    sid = client.session_id()
    panel = client.get(f"/s/{sid}/consent-panel")
    assert panel.status == 200
    assert "Mode: LIVE" in panel.text
    assert "Mode: MOCK" not in panel.text


def test_a_garment_already_tried_is_not_offered_again(tmp_path):
    client, service, provider, adapter, clock, first, _ = reach_decision(tmp_path)
    sid = client.session_id()
    assert client.post(f"/s/{sid}/again", {TOKEN: _token(client)}).status == 303
    page = client.get(f"/s/{sid}").text
    assert f'value="{first}"' not in page, f"{first} was already produced for this photo"
