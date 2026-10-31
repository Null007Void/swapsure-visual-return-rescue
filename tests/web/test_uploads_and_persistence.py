"""Own-input upload, restart persistence, TTL and deletion lifecycle.

Evidence for AC06/AC07 persistence rows and the T26/T36 surfaces: an oversized or
malformed file is refused without retention, an uploaded scenario drives the
session, and every consent/deletion fact survives a process restart.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

import pytest

from harness.webclient import Client, multipart
from ui.service import ReturnService
from ui.webapp import Application

from .helpers import (
    approve,
    choose,
    grant_consent,
    opt_in,
    reach_candidates,
    reach_decision,
    reach_receipt,
    start_previews,
    start_return,
    TOKEN,
)

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "web-scenario.example.json"


def _upload(client, payload: bytes, filename: str = "scenario.json", ctype: str = "application/json"):
    body, content_type = multipart("scenario", filename, payload, content_type=ctype)
    return client.post("/uploads", body=body, content_type=content_type)


def test_example_scenario_upload_shows_exact_bytes_and_drives_the_session(client, service):
    raw = EXAMPLE.read_bytes()
    response = _upload(client, raw, filename="web-scenario.example.json")
    assert response.status == 200
    assert hashlib.sha256(raw).hexdigest() in response.text
    assert str(len(raw)) in response.text
    assert "web-scenario.example.json" in response.text

    handle = re.search(r'name="uploadHandle" value="([^"]+)"', response.text).group(1)
    created = client.post("/returns", {"uploadHandle": handle})
    assert created.status == 303
    page = client.get(f"/s/{client.session_id()}")
    assert "ELIGIBLE_VISUAL_RESCUE" in page.text
    assert "COLOR" in page.text

    assert opt_in(client).status == 303
    assert grant_consent(client).status == 303
    candidates = client.get(f"/s/{client.session_id()}")
    assert "UP-NAVY-84" in candidates.text
    assert "UP-GREEN-84" in candidates.text
    assert "UP-EMPTY-80" in candidates.text.split("Excluded rows")[1]
    assert "ORIGINAL_COLOR_FAMILY" not in candidates.text  # original SKU differs

    decision = start_previews(client)
    assert "Your decision" in decision.text
    quote_page = choose(client, "UP-NAVY-84")
    assert "USD 5.00" in quote_page.text
    assert approve(client).status == 303
    receipt = client.get(f"/s/{client.session_id()}")
    assert "USD 84.00" in receipt.text

    # The uploaded bytes are stored outside any served directory and are not
    # reachable through the media route.
    stored = service.store.get_upload(handle)
    assert stored is not None
    assert pathlib.Path(stored["path"]).read_bytes() == raw
    assert "uploads" in stored["path"]
    assert f"/media/{handle}" not in receipt.text


def test_oversized_upload_is_refused_without_retention(client, service):
    payload = b'{"order": "' + b"A" * (70 * 1024) + b'"}'
    response = _upload(client, payload)
    assert response.status == 413
    assert "UPLOAD_TOO_LARGE" in response.text
    assert service.store.get_upload("upl_missing") is None
    assert not (service.data_dir / "uploads").exists() or not any(
        (service.data_dir / "uploads").iterdir()
    )


def test_malformed_json_and_utf8_are_typed_failures(client):
    broken = _upload(client, b'{"order": ')
    assert broken.status == 400
    assert "BAD_JSON" in broken.text
    encoding = _upload(client, b"\xff\xfe\x00not-utf8")
    assert encoding.status == 400
    assert "BAD_ENCODING" in encoding.text


def test_unexpected_fields_and_bad_rows_are_refused(client, service):
    document = json.loads(EXAMPLE.read_text())
    document["rescueEligible"] = True  # a caller trying to grant itself a rule
    response = _upload(client, json.dumps(document).encode())
    assert response.status == 400
    assert "UNEXPECTED_FIELD" in response.text
    assert "rescueEligible" in response.text

    document = json.loads(EXAMPLE.read_text())
    document["catalog"][0]["stock"] = "many"
    response = _upload(client, json.dumps(document).encode())
    assert response.status == 400
    assert "INVALID_FIELD" in response.text

    document = json.loads(EXAMPLE.read_text())
    document["catalog"][0]["currency"] = "EUR"
    response = _upload(client, json.dumps(document).encode())
    assert response.status == 400
    assert "CURRENCY_MISMATCH" in response.text
    assert service.store.get_upload("upl_missing") is None


def test_sqlite_store_uses_wal_and_mirrors_session_state(client, service):
    assert service.store.journal_mode().lower() == "wal"
    reach_decision(client)
    snapshot = service.store.get_snapshot(client.session_id())
    assert snapshot is not None
    assert snapshot["state"] == "DECISION_READY"
    payload = snapshot["payload"]
    assert payload["decision"]["status"] == "ELIGIBLE_VISUAL_RESCUE"
    assert payload["selection"]["candidates"]
    assert service.store.get_case(client.session_id()) is not None


def test_restart_retains_session_receipt_ledger_and_refund_route(data_dir, web_clock, client, service):
    reach_receipt(client)
    session_id = client.session_id()
    cookie = dict(client.cookies)
    commit_id = service.sim.transactions()[0].transaction_id
    service.close()

    reopened = ReturnService(data_dir, clock=web_clock)
    try:
        revived = Client(Application(reopened))
        revived.cookies.update(cookie)
        page = revived.get(f"/s/{session_id}")
        assert page.status == 200
        assert "Rescue Receipt" in page.text
        assert "USD 84.00" in page.text
        assert reopened.sim.session(session_id).state == "EXCHANGE_COMMITTED"
        assert [txn.transaction_id for txn in reopened.sim.transactions()] == [commit_id]
        assert reopened.store.ledger_totals()["retainedGmvMinor"] == 8400
        assert reopened.store.get_receipt(session_id)["commitId"] == commit_id
        assert revived.get("/refund-host").status == 200
        ledger = revived.get("/ledger")
        assert ledger.status == 200 and "USD 84.00" in ledger.text
    finally:
        reopened.close()


def test_restart_retains_deletion_and_consent_ledger(data_dir, web_clock, client, service):
    reach_decision(client)
    session_id = client.session_id()
    page = client.get(f"/s/{session_id}")
    revoke = client.post(f"/s/{session_id}/revoke", {TOKEN: client.form_token(page.text)})
    assert revoke.status == 303
    assert service.sim.session(session_id).state == "REFUND_HANDOFF"
    assert all(item.local_status == "DELETED" for item in service.media.list_session(session_id))
    service.close()

    reopened = ReturnService(data_dir, clock=web_clock)
    try:
        assert reopened.store.latest_consent(session_id)["action"] == "REVOKED"
        history = [row["action"] for row in reopened.store.consent_history(session_id)]
        assert history == ["GRANTED", "REVOKED"]
        deletions = reopened.store.deletion_history(session_id)
        assert deletions and all(row["local_status"] == "DELETED" for row in deletions)
        assert all(item.local_status == "DELETED" for item in reopened.media.list_session(session_id))
    finally:
        reopened.close()


def test_media_ttl_is_thirty_minutes_and_expiry_removes_the_file(client, service, web_clock):
    reach_decision(client)
    media = service.media.list_session(client.session_id())
    assert media and all((item.expires_at - item.created_at).total_seconds() == 1800 for item in media)
    handle = media[0].handle
    path = media[0].path
    assert path.exists()
    assert client.get(f"/media/{handle}").status == 200

    web_clock.advance(1801)
    expired = client.get(f"/media/{handle}")
    assert expired.status == 410
    assert "MEDIA_EXPIRED" in expired.text
    assert not path.exists()
    row = service.store.get_media(handle)
    assert row["local_status"] == "DELETED"


def test_failed_deletion_is_persisted_and_retried_after_restart(data_dir, web_clock):
    def locked(_path: str) -> None:
        raise PermissionError("file is locked by another process")

    first = ReturnService(data_dir, clock=web_clock, media_unlink=locked)
    try:
        client = Client(Application(first))
        reach_decision(client)
        session_id = client.session_id()
        handle = first.media.list_session(session_id)[0].handle
        path = first.media.list_session(session_id)[0].path
        page = client.get(f"/s/{session_id}")
        response = client.post(f"/s/{session_id}/delete-media", {TOKEN: client.form_token(page.text)})
        assert response.status == 200
        assert "pending retry" in response.text
        row = first.store.get_media(handle)
        assert row["local_status"] == "PENDING_RETRY"
        assert path.exists()
        assert first.store.pending_deletions()
    finally:
        first.close()

    second = ReturnService(data_dir, clock=web_clock)  # real unlink, restart sweeps
    try:
        row = second.store.get_media(handle)
        assert row is not None
        assert row["local_status"] == "DELETED"
        assert not path.exists()
        statuses = [item["local_status"] for item in second.store.deletion_history(session_id)]
        assert "PENDING_RETRY" in statuses and "DELETED" in statuses
    finally:
        second.close()


def test_restart_during_previews_lands_an_honest_failed_preview(data_dir, web_clock):
    from harness.webclient import Client as WebClient

    first = ReturnService(data_dir, clock=web_clock, latency_seconds=30.0)
    client = WebClient(Application(first))
    try:
        reach_candidates(client)
        page = client.get(f"/s/{client.session_id()}")
        assert client.post(
            f"/s/{client.session_id()}/preview", {TOKEN: client.form_token(page.text)}
        ).status == 303
        pending = client.get(f"/s/{client.session_id()}")
        assert "Generating mock previews" in pending.text
        session_id = client.session_id()
        cookie = dict(client.cookies)
    finally:
        first.close()

    second = ReturnService(data_dir, clock=web_clock)
    try:
        revived = WebClient(Application(second))
        revived.cookies.update(cookie)
        page = revived.get(f"/s/{session_id}")
        assert "Your decision" in page.text
        assert "Preview unavailable" in page.text
        assert page.text.count("<img ") == 0
        assert second.adapter.tasks_created == 0  # no blind re-dispatch
        assert second.sim.session(session_id).state == "DECISION_READY"
        assert 'href="/refund-host"' in page.text
    finally:
        second.close()


def test_late_result_after_revocation_is_discarded(client, service):
    from harness.webclient import Client as WebClient

    reach_candidates(client)
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(
        f"/s/{client.session_id()}/preview", {TOKEN: client.form_token(page.text)}
    ).status == 303
    session_id = client.session_id()
    job = service.store.load_job(session_id)
    assert job is not None

    # Revocation increments the consent generation and cancels dispatch.
    page = client.get(f"/s/{session_id}")
    assert client.post(f"/s/{session_id}/revoke", {TOKEN: client.form_token(page.text)}).status == 303
    assert service.store.load_job(session_id) is None
    outcome = service.coordinator.tick(job, generation_valid=False)
    assert outcome.status == "DISCARDED"
    assert outcome.reason == "CONSENT_REVOKED"
    assert service.sim.session(session_id).state == "REFUND_HANDOFF"
    assert all(item.local_status == "DELETED" for item in service.media.list_session(session_id))
    assert service.media.list_session(session_id)  # the ledger row remains, bytes do not


def test_consent_generation_change_invalidates_a_saved_job(client, service):
    reach_candidates(client)
    page = client.get(f"/s/{client.session_id()}")
    client.post(f"/s/{client.session_id()}/preview", {TOKEN: client.form_token(page.text)})
    session_id = client.session_id()
    job = service.store.load_job(session_id)
    assert job is not None
    case = service.store.get_case(session_id)
    service.store.update_case(
        session_id, now=service.clock(), consent_generation=case.consent_generation + 1
    )
    revived = service.get_case(session_id)
    assert revived.case.consent_generation != job["generation"]
