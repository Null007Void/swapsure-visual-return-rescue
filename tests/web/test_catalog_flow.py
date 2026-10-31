"""Bundled catalogue: browsing, selection, server-side binding, no task on browse."""
from __future__ import annotations

import pathlib

from harness.webclient import Client
from ui.service import ReturnService
from ui.webapp import Application

REPO = pathlib.Path(__file__).resolve().parents[2]
REFS = REPO / "assets" / "catalog" / "references"


def service(tmp_path):
    # MOCK previews: browsing and selecting must not reach any provider at all.
    return ReturnService(tmp_path / "data", preview_mode="MOCK")


def _to_consent(client, sid):
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/opt-in", {"request_token": client.form_token(page.text)}).status == 303
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/consent", {"request_token": client.form_token(page.text),
                                            "imageConsent": "true",
                                            "mockAcknowledged": "true"}).status == 303


def test_index_offers_both_collections(tmp_path):
    svc = service(tmp_path)
    try:
        page = Client(Application(svc)).get("/catalog")
        assert page.status == 200
        assert "/catalog/women" in page.text and "/catalog/men" in page.text
        assert "Advanced" in page.text
        assert "scenario import" in page.text.lower()
    finally:
        svc.close()


def test_collection_lists_ten_with_featured_first(tmp_path):
    svc = service(tmp_path)
    try:
        client = Client(Application(svc))
        response = client.get("/catalog/women")
        page = response.text
        assert page.count("Choose this garment") == 10
        order = [page.index(f'W01-TEAL-WRAP-DRESS'), page.index("W02-IVORY-LACE-BLOUSE"),
                 page.index("W03-BLACK-PINSTRIPE-BLAZER"), page.index("W05-LAVENDER-PLEATED-SKIRT"),
                 page.index("W04-RUST-WIDELEG-TROUSERS")]
        assert order == sorted(order), "featured five must lead"
        assert page.index("W01-TEAL-WRAP-DRESS") < page.index("W06-RED-FLORAL-MAXIDRESS")
        assert "not live-verified" in page
        assert "single garment" in page.lower()
        men = client.get("/catalog/men").text
        assert men.count("Choose this garment") == 10
        assert "M01-ASYMMETRIC-KNIT-POLO" in men
    finally:
        svc.close()


def test_thumbnail_is_the_bundled_reference_bytes(tmp_path):
    svc = service(tmp_path)
    try:
        client = Client(Application(svc))
        image = client.get("/catalog/women/thumb/W01-TEAL-WRAP-DRESS")
        assert image.status == 200
        assert image.headers.get("content-type") == "image/png"
        assert image.body == (REPO / "assets" / "catalog" / "thumbs" / "W01-TEAL-WRAP-DRESS.png").read_bytes()
        assert len(image.body) < (REFS / "W01-TEAL-WRAP-DRESS.png").stat().st_size / 4
        assert client.get("/catalog/women/thumb/NOT-A-SKU").status == 404
        assert client.get("/catalog/children").status == 404
    finally:
        svc.close()


def test_selection_starts_a_sample_return_bound_server_side(tmp_path):
    svc = service(tmp_path)
    try:
        client = Client(Application(svc))
        response = client.post("/catalog/women/select",
                               {"sku": "W01-TEAL-WRAP-DRESS",
                                "category": "lower_body",           # hostile overrides
                                "reference": "/etc/passwd"})
        assert response.status == 303
        sid = client.session_id()
        view = svc.get_case(sid)
        assert view.decision is not None and view.decision.status == "ELIGIBLE_VISUAL_RESCUE"
        assert view.selection is None          # the catalogue starts the return, not the preview
        _to_consent(client, sid)
        view = svc.get_case(sid)
        skus = [c.sku for c in view.selection.candidates]
        assert "W01-TEAL-WRAP-DRESS" in skus
        # the family rule still decides eligibility: a dress return offers dresses
        assert set(skus) <= {"W01-TEAL-WRAP-DRESS", "W06-RED-FLORAL-MAXIDRESS"}
        # and the session's case carries the sample order, not a client-supplied one
        assert view.case.order_id == "ORD-WOMEN-DRESS-SAMPLE"
        assert svc.adapter.tasks_created == 0
    finally:
        svc.close()


def test_unknown_selection_is_refused(tmp_path):
    svc = service(tmp_path)
    try:
        client = Client(Application(svc))
        assert client.post("/catalog/women/select", {"sku": "NOPE"}).status == 404
        assert client.post("/catalog/children/select", {"sku": "W01-TEAL-WRAP-DRESS"}).status == 404
        assert svc.adapter.tasks_created == 0
    finally:
        svc.close()


def test_men_collection_selects_and_keeps_its_own_order(tmp_path):
    svc = service(tmp_path)
    try:
        client = Client(Application(svc))
        assert client.post("/catalog/men/select", {"sku": "M03-TECHNICAL-JACKET"}).status == 303
        _to_consent(client, client.session_id())
        view = svc.get_case(client.session_id())
        assert view.case.order_id == "ORD-MEN-OUTERWEAR-SAMPLE"
        assert "M03-TECHNICAL-JACKET" in [c.sku for c in view.selection.candidates]
    finally:
        svc.close()
