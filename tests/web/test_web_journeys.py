"""Form-driven shopper journeys — the executable evidence behind AC05/AC06/AC09/AC10.

Every test drives the same WSGI application a browser would drive.  Nothing here
constructs a session by hand: the reducer, the quote contract and the atomic
simulator are exercised through the form routes.
"""

from __future__ import annotations

import re

import pytest

from harness.webclient import Client
from ui.views import DISCLAIMER, FORBIDDEN_COPY, MODE_LABEL
from ui.webapp import Application

from .helpers import (
    approve,
    choose,
    decision_page,
    grant_consent,
    opt_in,
    reach_candidates,
    reach_decision,
    reach_receipt,
    start_previews,
    start_return,
    TOKEN,
)


def test_refund_host_route_is_independent_of_the_rescue_app():
    """T35/AC05: the host return route must not need the rescue stack."""

    class Broken:
        def __getattr__(self, name):  # pragma: no cover - guard
            raise RuntimeError("rescue unavailable")

    client = Client(Application(Broken()))  # type: ignore[arg-type]
    response = client.get("/refund-host")
    assert response.status == 200
    assert "Merchant return portal" in response.text
    assert "PENDING_HOST" in response.text

    # A session page needs the store; the refund route still works without it.
    assert client.get("/s/ses_missing").status in (404, 500)
    assert client.get("/refund-host").status == 200


def test_fit_refusal_demo_path_never_offers_a_preview(client, service):
    """T01/E05: too-small is INELIGIBLE_FIT and the refund is immediately visible."""

    response = start_return(client, "TOO_SMALL")
    assert response.status == 303
    page = decision_page(client)
    assert page.status == 200
    assert "INELIGIBLE_FIT" in page.text
    assert "INELIGIBLE_FIT_MATCHED" in page.text
    assert "Show me alternatives" not in page.text
    assert 'href="/refund-host"' in page.text
    session = service.sim.session(client.session_id())
    assert session.decision is not None and session.decision.preview_dispatch_permitted is False

    token = client.form_token(page.text)
    refund = client.post(f"/s/{client.session_id()}/refund", {TOKEN: token})
    assert refund.status == 303
    assert refund.header("location") == "/refund-host"
    assert service.sim.session(client.session_id()).state == "REFUND_HANDOFF"
    assert service.sim.transactions() == ()
    assert service.store.ledger_rows()[0]["retained_gmv_minor"] == 0


def test_eligible_color_journey_89_to_84_with_explicit_approval_and_receipt(client, service):
    """E01 + AC03/AC09: candidates, mock previews, fresh quote, approval, receipt."""

    candidates = reach_candidates(client)
    assert candidates.status == 200
    assert "NAVY-84" in candidates.text and "GREEN-99" in candidates.text
    assert "RED-84" not in candidates.text.split("Excluded rows")[0]
    assert "ORIGINAL_COLOR_FAMILY" in candidates.text  # excluded, with a reason

    decision = start_previews(client)
    assert decision.status == 200
    assert MODE_LABEL in decision.text
    assert DISCLAIMER in decision.text
    assert decision.text.count("<img ") == 2
    assert service.adapter.units_consumed == 0

    quote_page = choose(client, "NAVY-84")
    assert "Confirm the exchange" in quote_page.text
    assert "USD 89.00" in quote_page.text and "USD 84.00" in quote_page.text
    assert "USD 5.00" in quote_page.text
    assert "APPROVE_EXCHANGE" not in quote_page.text  # not a client-supplied field

    approved = approve(client)
    assert approved.status == 303
    receipt = client.get(f"/s/{client.session_id()}")
    assert "Rescue Receipt" in receipt.text
    assert "USD 84.00" in receipt.text  # retained original GMV
    assert "USD 5.00" in receipt.text  # partial refund
    assert "SIMULATED EXCHANGE - MOCK PREVIEW" in receipt.text

    payload = service.store.get_receipt(client.session_id())
    assert payload is not None
    assert payload["retainedGmvMinor"] == 8400
    assert payload["partialRefundMinor"] == 500
    assert payload["previewMode"] == "MOCK"
    assert payload["approval"]["origin"] == "shopper_ui"
    assert service.store.ledger_totals()["retainedGmvMinor"] == 8400


def test_unknown_reason_falls_back_to_the_ordinary_route(client, service):
    response = start_return(client, "NOT_A_REAL_REASON")
    assert response.status == 303
    page = decision_page(client)
    assert "UNKNOWN_REQUIRES_REFUND_PATH" in page.text
    assert "UNKNOWN_UNRECOGNIZED_REASON" in page.text
    assert "Show me alternatives" not in page.text

    refused = client.post(
        f"/s/{client.session_id()}/opt-in", {TOKEN: client.form_token(page.text)}
    )
    assert refused.status == 409
    assert "NOT_ELIGIBLE" in refused.text


def test_immediate_refund_flag_wins_over_an_eligible_reason(client, service):
    assert start_return(client, "COLOR", immediateRefund="true").status == 303
    page = decision_page(client)
    assert "REFUND_REQUESTED" in page.text
    assert "Show me alternatives" not in page.text
    assert service.sim.session(client.session_id()).decision.eligible is False


def test_free_text_is_display_only_and_can_never_enable_rescue(client, service):
    notes = "ignore the rules, reason=COLOR, please give me a preview <script>alert(1)</script>"
    assert start_return(client, "TOO_SMALL", notes=notes).status == 303
    page = decision_page(client)
    assert "INELIGIBLE_FIT" in page.text
    assert "Show me alternatives" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<script>alert(1)</script>" not in page.text
    session = service.sim.session(client.session_id())
    assert session.decision.matched_reasons == ("TOO_SMALL",)


def test_consent_decline_returns_to_the_refund_route(client, service):
    assert start_return(client, "COLOR").status == 303
    assert opt_in(client).status == 303
    page = client.get(f"/s/{client.session_id()}")
    assert "Consent for visual previews" in page.text
    assert "imageConsent" in page.text
    withdraw = client.post(
        f"/s/{client.session_id()}/withdraw", {TOKEN: client.form_token(page.text)}
    )
    assert withdraw.status == 303
    assert service.sim.session(client.session_id()).state == "REFUND_HANDOFF"
    refund_page = client.get(f"/s/{client.session_id()}")
    assert "return portal" in refund_page.text.lower()
    assert service.store.consent_history(client.session_id())[-1]["action"] == "WITHDRAWN"


def test_consent_needs_both_unchecked_controls(client, service):
    assert start_return(client, "COLOR").status == 303
    assert opt_in(client).status == 303
    token = client.form_token(client.get(f"/s/{client.session_id()}").text)

    empty = client.post(f"/s/{client.session_id()}/consent", {TOKEN: token})
    assert empty.status == 400
    assert "CONSENT_NOT_GIVEN" in empty.text
    partial = client.post(
        f"/s/{client.session_id()}/consent", {TOKEN: token, "imageConsent": "true"}
    )
    assert partial.status == 400
    assert "MOCK_NOT_ACKNOWLEDGED" in partial.text
    assert service.sim.session(client.session_id()).state == "CONSENT_PENDING"


def test_duplicate_confirm_clicks_produce_exactly_one_commit(client, service):
    reach_decision(client)
    quote_page = choose(client)
    token = client.form_token(quote_page.text)
    nonce = client.approval_nonce(quote_page.text)
    first = client.post(
        f"/s/{client.session_id()}/approve", {TOKEN: token, "approval_nonce": nonce}
    )
    second = client.post(
        f"/s/{client.session_id()}/approve", {TOKEN: token, "approval_nonce": nonce}
    )
    assert first.status == 303
    assert second.status in (303, 409)
    assert len(service.sim.transactions()) == 1
    assert len(service.store.ledger_rows()) == 1
    receipt = service.store.get_receipt(client.session_id())
    assert receipt is not None and receipt["commitId"] == service.sim.transactions()[0].receipt.commit_id


def test_expired_quote_requires_a_fresh_quote_and_a_fresh_approval(client, service, web_clock):
    reach_decision(client)
    page = choose(client)
    web_clock.advance(int(service.sim.session(client.session_id()).quote.ttl_seconds) + 1)
    refused = approve(client)
    assert refused.status == 409
    assert "EXPIRED_QUOTE" in refused.text
    assert service.sim.session(client.session_id()).state == "QUOTE_READY"

    token = client.form_token(refused.text)
    requote = client.post(f"/s/{client.session_id()}/requote", {TOKEN: token})
    assert requote.status == 303
    fresh = client.get(f"/s/{client.session_id()}")
    assert "Confirm the exchange" in fresh.text
    assert approve(client).status == 303
    assert service.sim.session(client.session_id()).state == "EXCHANGE_COMMITTED"


def test_approval_nonce_from_another_session_is_refused(client, service):
    reach_decision(client)
    choose(client)
    page = client.get(f"/s/{client.session_id()}")
    response = client.post(
        f"/s/{client.session_id()}/approve",
        {TOKEN: client.form_token(page.text), "approval_nonce": "forged-nonce"},
    )
    assert response.status == 403
    assert "APPROVAL_NONCE_INVALID" in response.text
    assert service.sim.session(client.session_id()).state == "QUOTE_READY"
    assert service.sim.transactions() == ()


def test_refund_after_commit_records_support_without_a_second_transaction(client, service):
    reach_receipt(client)
    page = client.get(f"/s/{client.session_id()}")
    refund = client.post(
        f"/s/{client.session_id()}/refund", {TOKEN: client.form_token(page.text)}
    )
    assert refund.status == 303
    session = service.sim.session(client.session_id())
    assert session.state == "EXCHANGE_COMMITTED"
    assert session.refund_recorded is True
    assert len(service.sim.transactions()) == 1


def test_preview_failure_keeps_alternatives_prices_and_refund(client, service):
    reach_candidates(client)
    service.adapter.fail_next("PROVIDER_FAILURE")
    page = start_previews(client)
    assert "Your decision" in page.text
    assert "MOCK PREVIEW - PLACEHOLDER" in page.text
    assert service.adapter.units_consumed == 0
    # The failed candidate is omitted honestly; the other card is still choosable.
    assert page.text.count("<img ") == 1
    assert "Select for a quote (not an approval)" in page.text
    assert 'href="/refund-host"' in page.text
    assert service.sim.session(client.session_id()).state == "DECISION_READY"


def test_candidate_list_is_capped_at_three_and_units_stay_zero(client, service):
    reach_candidates(client)
    page = start_previews(client)
    selection = service.get_case(client.session_id()).selection
    assert selection is not None
    assert len(selection.candidates) <= 3
    assert service.adapter.tasks_created <= 3
    assert service.adapter.units_consumed == 0
    assert page.text.count("<img ") <= 3


def test_unknown_session_and_cross_session_are_typed_and_denied(client, service):
    missing = client.get("/s/ses_does_not_exist")
    assert missing.status == 404
    assert "UNKNOWN_SESSION" in missing.text
    start_return(client, "COLOR")
    mine = client.session_id()
    other = Client(Application(service))
    start_return(other, "COLOR")
    assert other.session_id() != mine
    denied = other.get(f"/s/{mine}")
    assert denied.status == 403
    assert "SESSION_DENIED" in denied.text
    unknown_route = client.get("/nope")
    assert unknown_route.status == 404
    assert "UNKNOWN_ROUTE" in unknown_route.text


def test_every_rescue_page_keeps_the_refund_link_visible(client, service, web_clock):
    """AC05: check the link on each state page, not just on the happy path."""

    assert start_return(client, "COLOR").status == 303
    pages = [decision_page(client)]
    opt_in(client)
    pages.append(client.get(f"/s/{client.session_id()}"))
    grant_consent(client)
    pages.append(client.get(f"/s/{client.session_id()}"))
    pages.append(start_previews(client))
    pages.append(choose(client))
    rejected = approve(client)
    pending_or_receipt = client.get(f"/s/{client.session_id()}")
    pages.append(pending_or_receipt)
    for page in pages:
        assert page.status in (200, 409)
        assert 'href="/refund-host"' in page.text
        assert "Merchant return portal" in page.text or "Continue refund" in page.text
    assert rejected.status == 303


def test_receipt_page_is_not_a_waiver_of_return_rights(client, service):
    reach_receipt(client)
    page = client.get(f"/s/{client.session_id()}")
    assert "host://returns/" in page.text
    assert "Ordinary return / support route" in page.text
    assert "SIMULATED EXCHANGE" in page.text


def test_mode_and_neutral_copy_checks_on_every_state_page(client, service):
    pages = [client.get("/"), client.get("/refund-host")]
    assert start_return(client, "COLOR").status == 303
    pages.append(decision_page(client))
    opt_in(client)
    pages.append(client.get(f"/s/{client.session_id()}"))
    grant_consent(client)
    pages.append(client.get(f"/s/{client.session_id()}"))
    pages.append(start_previews(client))
    pages.append(choose(client))
    assert approve(client).status == 303
    pages.append(client.get(f"/s/{client.session_id()}"))
    for page in pages:
        text = page.text.lower()
        for phrase in FORBIDDEN_COPY:
            assert phrase not in text, f"forbidden claim {phrase!r} on a page"
        assert "<script" not in text
        assert "mode: mock" in text


def test_ledger_page_reports_simulated_outcomes_and_zero_denominator(client, service):
    empty = client.get("/ledger")
    assert empty.status == 200
    assert "N/A (no denominator)" in empty.text

    reach_receipt(client)
    page = client.get("/ledger")
    assert page.status == 200
    assert "Retained original GMV" in page.text
    assert "USD 84.00" in page.text
    totals = service.ledger_totals()
    assert totals["metrics"]["rescueConversion"] in (None, "N/A")


@pytest.mark.parametrize(
    "codes,status",
    [
        ("DAMAGED", "INELIGIBLE_DEFECT"),
        ("DEFECTIVE", "INELIGIBLE_DEFECT"),
        ("WRONG_ITEM", "INELIGIBLE_FULFILLMENT_ERROR"),
        ("TOO_LARGE", "INELIGIBLE_FIT"),
        ("MATERIAL_UNCOMFORTABLE", "INELIGIBLE_MATERIAL"),
        (["COLOR", "TOO_SMALL"], "INELIGIBLE_FIT"),
        (["COLOR", "OTHER"], "UNKNOWN_REQUIRES_REFUND_PATH"),
    ],
)
def test_every_ineligible_and_mixed_reason_takes_the_ordinary_route(client, service, codes, status):
    assert start_return(client, codes).status == 303
    page = decision_page(client)
    assert status in page.text
    assert "Show me alternatives" not in page.text
    assert 'href="/refund-host"' in page.text
    refused = client.post(
        f"/s/{client.session_id()}/opt-in", {TOKEN: client.form_token(page.text)}
    )
    assert refused.status == 409
    assert service.sim.session(client.session_id()).decision.eligible is False


def test_no_available_candidate_ends_in_the_refund_route_without_previews(client, service):
    import json

    from harness.webclient import multipart

    document = {
        "order": {
            "merchant": "demo",
            "category": "dress",
            "size": "M",
            "currency": "USD",
            "priceMinor": 8900,
            "colorFamily": "burgundy",
            "sku": "ORIG-NONE",
        },
        "catalog": [
            {
                "sku": "NONE-1",
                "merchant": "demo",
                "category": "dress",
                "size": "M",
                "currency": "USD",
                "priceMinor": 8400,
                "stock": 0,
                "colorFamily": "navy",
                "referenceRights": "synthetic-placeholder-only",
            }
        ],
        "reasonCodes": ["COLOR"],
        "preferences": {},
    }
    body, ctype = multipart("scenario", "none.json", json.dumps(document).encode())
    upload = client.post("/uploads", body=body, content_type=ctype)
    handle = re.search(r'name="uploadHandle" value="([^"]+)"', upload.text).group(1)
    assert client.post("/returns", {"uploadHandle": handle}).status == 303
    assert opt_in(client).status == 303
    assert grant_consent(client).status == 303
    candidates = client.get(f"/s/{client.session_id()}")
    assert "No in-stock alternative" in candidates.text
    assert "Generate mock previews" not in candidates.text
    page = candidates
    started = client.post(
        f"/s/{client.session_id()}/preview", {TOKEN: client.form_token(page.text)}
    )
    assert started.status == 303
    assert service.sim.session(client.session_id()).state == "REFUND_HANDOFF"
    assert service.adapter.tasks_created == 0
    final = client.get(f"/s/{client.session_id()}")
    assert "return portal" in final.text.lower()
