"""Money-adjacent guards: no photo, double submission, and an exhausted allowance."""
from __future__ import annotations

from harness.webclient import Client
from web.test_repeat_and_photo import TOKEN, _token, ledger_rows, reach_decision
from web.test_public_live_journey import FakeProvider, build, journey_to_candidates, upload_photo


def test_generation_without_a_photo_is_refused_before_any_task(tmp_path):
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider)
    client = Client(app)
    journey_to_candidates(client)
    sid = client.session_id()
    response = client.post(f"/s/{sid}/preview",
                           {TOKEN: _token(client), "sku": service.get_case(sid).selection.candidates[0].sku})
    assert response.status == 409
    assert "PHOTO_REQUIRED" in response.text
    assert provider.task_calls() == []
    assert ledger_rows(tmp_path / "units.sqlite3") == []


def test_double_submission_creates_one_task_and_one_reservation(tmp_path):
    client, service, provider, adapter, clock, first, _ = reach_decision(tmp_path)
    sid = client.session_id()
    rows_before = ledger_rows(tmp_path / "units.sqlite3")
    tasks_before = len(provider.task_calls())

    assert client.post(f"/s/{sid}/again", {TOKEN: _token(client)}).status == 303
    token = _token(client)
    first_post = client.post(f"/s/{sid}/preview", {TOKEN: token, "sku": first})
    second_post = client.post(f"/s/{sid}/preview", {TOKEN: token, "sku": first})
    assert first_post.status == 409 or second_post.status == 409
    assert ledger_rows(tmp_path / "units.sqlite3") == rows_before
    assert len(provider.task_calls()) == tasks_before


def test_exhausted_allowance_is_never_reused_by_try_another(tmp_path):
    """One task of allowance: a second garment is refused, and no unit is spent on the attempt."""
    provider = FakeProvider()
    app, service, adapter, clock = build(tmp_path, provider, unit_cap=2)
    client = Client(app)
    journey_to_candidates(client)
    sid = client.session_id()
    write = service.data_dir / "references"
    from web.test_public_live_journey import png, write_references

    write_references(service, [c.sku for c in service.get_case(sid).selection.candidates])
    assert upload_photo(client).status == 303
    first = service.get_case(sid).selection.candidates[0].sku
    assert client.post(f"/s/{sid}/preview", {TOKEN: _token(client), "sku": first}).status == 303
    client.get(f"/s/{sid}")

    rows = ledger_rows(tmp_path / "units.sqlite3")
    assert len(rows) == 1 and rows[0][2] == 2

    assert client.post(f"/s/{sid}/again", {TOKEN: _token(client)}).status == 303
    remaining = [c.sku for c in service.get_case(sid).selection.candidates if c.sku != first]
    if not remaining:
        return
    refused = client.post(f"/s/{sid}/preview", {TOKEN: _token(client), "sku": remaining[0]})
    assert refused.status == 409, refused.text[:200]
    assert "ALLOWANCE_EXHAUSTED" in refused.text
    assert ledger_rows(tmp_path / "units.sqlite3") == rows, "the exhausted allowance is not reused"
    assert len(provider.task_calls()) == 1
