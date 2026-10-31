"""The shared judging demo asks for restraint; it must never impose it."""

from __future__ import annotations

import pytest

from harness.webclient import Client
from ui import views
from ui.service import ReturnService
from ui.webapp import Application


def make_app(tmp_path, monkeypatch, notice: bool):
    monkeypatch.setenv("SWAPSURE_JUDGING_NOTICE", "1" if notice else "0")
    service = ReturnService(tmp_path, preview_mode="MOCK")
    return Application(service), service


def test_the_notice_is_absent_by_default(tmp_path, monkeypatch):
    app, service = make_app(tmp_path, monkeypatch, notice=False)
    try:
        body = Client(app).get("/").text
        assert "up to three previews" not in body
    finally:
        service.close()


def test_the_notice_asks_for_three_previews_and_says_it_is_not_a_limit(tmp_path, monkeypatch):
    app, service = make_app(tmp_path, monkeypatch, notice=True)
    try:
        body = Client(app).get("/").text
        assert "up to three previews" in body
        assert "not a limit" in body
        assert "keep going" in body
        # it must not claim to be offline, and must not hide the refund route
        assert "never pretends to be an offline demo" in body
        assert "Continue refund" in body
    finally:
        service.close()


def _one_session_preview(client, app, sku: str) -> str:
    assert client.post("/catalog/women/select", {"sku": sku}).status == 303
    sid = client.session_id()
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/opt-in",
                       {"request_token": client.form_token(page.text)}).status == 303
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/consent",
                       {"request_token": client.form_token(page.text),
                        "imageConsent": "true",
                        "mockAcknowledged": "true"}).status == 303
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/preview",
                       {"request_token": client.form_token(page.text)}).status == 303
    return sid


def test_the_notice_does_not_gate_previews(tmp_path, monkeypatch):
    """Three is guidance in the banner text only; four previews still run, in two sessions."""
    app, service = make_app(tmp_path, monkeypatch, notice=True)
    try:
        client = Client(app)
        sids = [
            _one_session_preview(client, app, "W01-TEAL-WRAP-DRESS"),
            _one_session_preview(client, app, "W02-IVORY-LACE-BLOUSE"),
        ]
        for sid in sids:
            body = client.get(f"/s/{sid}").text
            assert "limit reached" not in body.lower()
            assert "Continue refund" in body
        assert "Shared judging demo" in client.get(f"/s/{sids[-1]}").text
    finally:
        service.close()


def test_the_banner_is_not_rendered_when_the_flag_is_off(tmp_path, monkeypatch):
    app, service = make_app(tmp_path, monkeypatch, notice=False)
    try:
        assert views._judging_banner(False) == ""
        assert "Shared judging demo" not in Client(app).get("/").text
    finally:
        service.close()


@pytest.mark.parametrize("notice", [True, False])
def test_the_mode_is_never_rewritten_by_the_notice(tmp_path, monkeypatch, notice):
    app, service = make_app(tmp_path, monkeypatch, notice=notice)
    try:
        body = Client(app).get("/").text
        assert "Mode: MOCK" in body
        assert "Mode: LIVE" not in body
    finally:
        service.close()
