"""Accessibility and content checks for the decision, refund and consent flows.

T31/T32 evidence, asserted on the rendered HTML a keyboard or screen-reader user
receives: landmarks, a skip link, programmatic labels, live status regions, a
focus target, text-bearing buttons, no auto-submit, and neutral copy.
"""

from __future__ import annotations

import re

import pytest

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


def _pages(client, service):
    pages = [("home", client.get("/")), ("refund-host", client.get("/refund-host"))]
    assert start_return(client, "COLOR").status == 303
    pages.append(("decision", decision_page(client)))
    opt_in(client)
    pages.append(("consent", client.get(f"/s/{client.session_id()}")))
    grant_consent(client)
    pages.append(("candidates", client.get(f"/s/{client.session_id()}")))
    pages.append(("previews", start_previews(client)))
    pages.append(("quote", choose(client)))
    pages.append(("receipt", client.get(f"/s/{client.session_id()}")))
    return pages


def test_landmarks_skip_link_and_focus_target_on_every_page(client, service):
    for name, page in _pages(client, service):
        body = page.text
        assert '<a class="skip" href="#main">' in body, name
        assert '<main id="main" tabindex="-1">' in body, name
        assert 'role="banner"' in body, name
        assert 'role="contentinfo"' in body, name
        assert 'aria-label="Primary"' in body, name
        assert body.count("<h1") == 1, name


def test_every_input_has_a_programmatic_label(client, service):
    for name, page in _pages(client, service):
        ids = set(re.findall(r'<input[^>]*\bid="([^"]+)"', page.text))
        ids |= set(re.findall(r'<textarea[^>]*\bid="([^"]+)"', page.text))
        for field_id in ids:
            assert f'<label for="{field_id}"' in page.text, (name, field_id)
        # Checkboxes are wrapped in their own label element.
        for match in re.finditer(r"<label[^>]*>\s*<input[^>]*type=\"checkbox\"", page.text):
            assert match


def test_status_regions_are_live_and_decisions_are_announced(client, service):
    reach_decision(client)
    decision = client.get(f"/s/{client.session_id()}")
    assert 'role="status"' in decision.text
    assert 'aria-live="polite"' in decision.text
    quote = choose(client)
    assert 'role="status"' in quote.text
    assert approve(client).status == 303
    receipt = client.get(f"/s/{client.session_id()}")
    assert 'role="status"' in receipt.text
    assert "SIMULATED EXCHANGE" in receipt.text


def test_refund_control_is_visible_keyboard_reachable_and_never_hidden(client, service):
    for name, page in _pages(client, service):
        assert re.search(r'<a class="refund" href="/refund-host"', page.text), name
        assert "Continue refund (host returns)" in page.text, name
        assert "display:none" not in page.text.replace(" ", ""), name
        assert "visibility:hidden" not in page.text, name
        assert "tabindex=\"-1\"" in page.text  # only the main focus target


def test_buttons_have_text_and_forms_do_not_auto_submit(client, service):
    for name, page in _pages(client, service):
        for button in re.findall(r"<button[^>]*>(.*?)</button>", page.text, re.S):
            assert button.strip(), name
        assert "<script" not in page.text.lower(), name
        assert "autofocus" not in page.text.lower(), name
        assert "onclick=" not in page.text.lower(), name


def test_quote_approval_names_the_exact_amounts_and_is_not_preselected(client, service):
    reach_decision(client)
    quote = choose(client)
    assert "Approve exchange: refund USD 5.00 and exchange for NAVY-84" in quote.text
    assert "Selecting an alternative was not approval" in quote.text
    assert "checked" not in quote.text.lower()  # nothing pre-consented


def test_consent_controls_start_unchecked_and_are_separate(client, service):
    assert start_return(client, "COLOR").status == 303
    opt_in(client)
    page = client.get(f"/s/{client.session_id()}")
    assert page.text.count('type="checkbox"') == 2
    assert 'value="true"' in page.text
    assert "checked" not in page.text.lower()
    assert "I consent to image processing" in page.text
    assert "MOCK placeholder, not a YouCam result" in page.text


def test_refund_page_is_reachable_without_reading_comprehension_of_rescue(client, service):
    """T31: the refund path is one link from every rescue state page."""

    assert start_return(client, "TOO_SMALL").status == 303
    page = decision_page(client)
    link = re.search(r'<a class="refund" href="([^"]+)"', page.text)
    assert link and link.group(1) == "/refund-host"
    target = client.get(link.group(1))
    assert target.status == 200
    assert "Merchant return portal" in target.text
