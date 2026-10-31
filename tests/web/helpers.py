"""Shared browser-journey helpers for the Phase C tests."""

from __future__ import annotations

from typing import Any

from harness.webclient import Client, Response

TOKEN = "request_token"


def start_return(client: Client, reasons: Any = "COLOR", **extra: Any) -> Response:
    return client.post("/returns", {"reason": reasons, **extra})


def decision_page(client: Client, session_id: str | None = None) -> Response:
    return client.get(f"/s/{session_id or client.session_id()}")


def opt_in(client: Client) -> Response:
    page = decision_page(client)
    return client.post(
        f"/s/{client.session_id()}/opt-in",
        {TOKEN: client.form_token(page.text)},
    )


def grant_consent(client: Client) -> Response:
    page = client.get(f"/s/{client.session_id()}")
    return client.post(
        f"/s/{client.session_id()}/consent",
        {
            TOKEN: client.form_token(page.text),
            "imageConsent": "true",
            "mockAcknowledged": "true",
        },
    )


def reach_candidates(client: Client, reasons: Any = "COLOR") -> Response:
    assert start_return(client, reasons).status == 303
    assert opt_in(client).status == 303
    assert grant_consent(client).status == 303
    return client.get(f"/s/{client.session_id()}")


def start_previews(client: Client) -> Response:
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(
        f"/s/{client.session_id()}/preview", {TOKEN: client.form_token(page.text)}
    ).status == 303
    return client.get(f"/s/{client.session_id()}")


def reach_decision(client: Client, reasons: Any = "COLOR") -> Response:
    reach_candidates(client, reasons)
    return start_previews(client)


def choose(client: Client, sku: str = "NAVY-84") -> Response:
    page = client.get(f"/s/{client.session_id()}")
    assert client.post(
        f"/s/{client.session_id()}/choose",
        {TOKEN: client.form_token(page.text), "sku": sku},
    ).status == 303
    return client.get(f"/s/{client.session_id()}")


def approve(client: Client) -> Response:
    page = client.get(f"/s/{client.session_id()}")
    return client.post(
        f"/s/{client.session_id()}/approve",
        {
            TOKEN: client.form_token(page.text),
            "approval_nonce": client.approval_nonce(page.text),
        },
    )


def reach_receipt(client: Client) -> Response:
    reach_decision(client)
    quote_page = choose(client)
    assert "Confirm the exchange" in quote_page.text
    assert approve(client).status == 303
    return client.get(f"/s/{client.session_id()}")
