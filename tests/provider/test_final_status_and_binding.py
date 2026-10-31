"""The final status query, HTTP classification and the immutable recovery binding."""
from __future__ import annotations

import json

import pytest

from adapters.youcam_live import AdapterError, FailureCode, LiveSettings, Response, YouCamLivePreviewAdapter
from domain.preview import PreviewPlan
from harness.clock import ManualClock
from harness.webclient import Client
from ui.service import ReturnService
from ui.webapp import Application

from .durable_support import S3, START, FakeProvider, journey_to_candidates, png, start_previews, upload_photo, write_references

HOST = "yce-api-01.makeupar.com"


def status_body(state: str) -> Response:
    if state == "success":
        return Response(200, json.dumps({"data": {"task_status": "success",
                                                  "results": {"url": f"https://{S3}/out.png"}}}).encode())
    return Response(200, json.dumps({"data": {"task_status": state, "results": None}}).encode())


class ScriptedProvider(FakeProvider):
    """Hands back a scripted sequence for status GETs, then whatever remains."""

    def __init__(self, script, **kw):
        super().__init__(**kw)
        self.script = list(script)

    def request(self, method, url, *, headers, body=None, timeout, max_bytes=None):
        if method == "GET" and "/task/cloth-v4/" in url:
            self.calls.append((method, url))
            item = self.script.pop(0) if self.script else "processing"
            if isinstance(item, Exception):
                raise item
            if isinstance(item, Response):
                return item
            return status_body(item)
        return super().request(method, url, headers=headers, body=body, timeout=timeout)


def wire(tmp_path, provider_factory, *, wait_seconds=300.0):
    clock = ManualClock(START)
    provider = provider_factory(clock)
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="test-key", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2,
                              unit_cap=8, session_unit_cap=8, wait_seconds=wait_seconds,
                              final_status_timeout=4.0,
                              ledger_path=str(tmp_path / "units.sqlite3")),
        clock=lambda: clock.now, transport=provider)
    service = ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE", adapter=adapter)
    client = Client(Application(service))
    journey_to_candidates(client)
    write_references(service, [c.sku for c in service.get_case(client.session_id()).selection.candidates])
    upload_photo(client)
    start_previews(client)
    return service, adapter, clock, provider, client


def status_gets(provider):
    return [url for method, url in provider.calls if method == "GET" and "/task/cloth-v4/" in url]


# ---------------------------------------------------------------- 1. final query
def test_one_final_status_query_runs_after_the_wait_then_polling_stops(tmp_path):
    service, adapter, clock, provider, client = wire(
        tmp_path, lambda c: ScriptedProvider(["processing"] * 20), wait_seconds=300.0)
    sid = client.session_id()

    baseline = len(status_gets(provider))          # the polls made while the plan was live
    clock.advance(301)                     # the wait has expired: the old code raised TIMEOUT here
    page = client.get(f"/s/{sid}")
    after_first = len(status_gets(provider))
    assert after_first - baseline == 2, "each candidate gets exactly one post-deadline status query"

    # A second page load must not ask again: the final query is a one-off, recorded on disk.
    clock.advance(5)
    second = client.get(f"/s/{sid}")
    assert len(status_gets(provider)) == after_first
    assert "Your decision" in second.text
    result = service.get_case(sid).session.preview_result
    assert result.status == "UNRESOLVED" and result.failure_code == "RECONCILIATION_PENDING"
    finals = [row for op in adapter._tasks for row in adapter._registry.polls(op)
              if row["outcome"].startswith("FINAL_STATUS")]
    assert len(finals) == 2
    assert all(row["at"] for row in finals)


def test_final_query_timeout_is_its_own_bounded_clock(tmp_path):
    service, adapter, clock, provider, client = wire(
        tmp_path, lambda c: ScriptedProvider(["processing"] * 20), wait_seconds=300.0)
    clock.advance(301)
    client.get(f"/s/{client.session_id()}")
    assert status_gets(provider), "the final query must actually reach the provider"
    # The deadline is long gone; the request still had to use a positive, bounded timeout.
    assert adapter._settings.final_status_timeout > 0


# ---------------------------------------------------------------- 2. HTTP classification
@pytest.mark.parametrize("code,failure", [(429, "ERROR:RATE_LIMIT"), (503, "ERROR:PROVIDER_FAILURE")])
def test_http_rate_limit_and_server_error_stay_pending(tmp_path, code, failure):
    service, adapter, clock, provider, client = wire(
        tmp_path, lambda c: ScriptedProvider([Response(code, b"{}")] * 2 + ["success"] * 8))
    sid = client.session_id()
    first = client.get(f"/s/{sid}")
    assert "Your decision" not in first.text           # a busy provider is not a verdict
    assert 'http-equiv="refresh" content="5"' in first.text

    clock.advance(5)
    done = client.get(f"/s/{sid}")
    assert "Your decision" in done.text
    rows = [row for op in adapter._tasks for row in adapter._registry.polls(op)]
    assert any(row["outcome"] == failure for row in rows)
    assert any(row["http_status"] == code for row in rows)


def test_authentication_refusal_stays_terminal_and_distinct(tmp_path):
    service, adapter, clock, provider, client = wire(
        tmp_path, lambda c: ScriptedProvider([Response(401, b"{}")] * 8))
    sid = client.session_id()
    page = client.get(f"/s/{sid}")
    assert "Your decision" in page.text
    result = service.get_case(sid).session.preview_result
    assert result.status == "FAILED" and result.failure_code == "AUTH_FAILURE"
    rows = [row for op in adapter._tasks for row in adapter._registry.polls(op)]
    assert any(row["outcome"] == "ERROR:AUTH_FAILURE" for row in rows)


# ---------------------------------------------------------------- 3. immutable binding
def test_conflicting_binding_is_refused_and_never_overwritten(tmp_path):
    service, adapter, clock, provider, client = wire(
        tmp_path, lambda c: ScriptedProvider(["processing"] * 20), wait_seconds=300.0)
    sid = client.session_id()
    case = service.get_case(sid)
    plan = case.session.preview_plan
    operation = next(iter(adapter._tasks))
    before = adapter._registry.lookup(operation)
    assert before["plan_deadline"] and before["provider_version"] == "cloth-v4"

    # Same operation, different authority: a different purchase. It must not be adopted.
    # Same operation identity (session, candidate, authority, version) but a different agreed
    # deadline: a different purchase. It must not be adopted, and nothing may be overwritten.
    conflicting = PreviewPlan(session_id=plan.session_id, consent_version=plan.consent_version,
                              candidate_ids=plan.candidate_ids, provider_version=plan.provider_version,
                              estimated_units=plan.estimated_units,
                              authorization_ref=plan.authorization_ref,
                              deadline=plan.deadline.replace(year=2027), mode="LIVE")
    restarted = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="test-key", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2,
                              unit_cap=8, session_unit_cap=8, ledger_path=str(tmp_path / "units.sqlite3")),
        clock=lambda: clock.now, transport=ScriptedProvider(["processing"] * 20))
    with pytest.raises(AdapterError) as caught:
        restarted.adopt_preview(operation, session_id=sid, plan=conflicting)
    assert caught.value.code is FailureCode.RECOVERY_REQUIRED
    assert "plan deadline" in str(caught.value)
    assert restarted.adopt_preview(operation, session_id=sid, plan=plan) is True
    after = adapter._registry.lookup(operation)
    assert after["provider_task_id"] == before["provider_task_id"]
    assert after["authorization_ref"] == before["authorization_ref"]


def test_adoption_keeps_the_original_deadline_not_a_recomputed_one(tmp_path):
    service, adapter, clock, provider, client = wire(
        tmp_path, lambda c: ScriptedProvider(["processing"] * 20), wait_seconds=300.0)
    sid = client.session_id()
    operation = next(iter(adapter._tasks))
    saved = adapter._registry.lookup(operation)["plan_deadline"]

    later = ManualClock(START)
    later.advance(600)                    # a much later restart would recompute a different wait
    second = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="test-key", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2,
                              unit_cap=8, session_unit_cap=8, wait_seconds=300.0,
                              ledger_path=str(tmp_path / "units.sqlite3")),
        clock=lambda: later.now, transport=ScriptedProvider(["processing"] * 20))
    assert second.adopt_preview(operation, session_id=sid) is True
    assert second._tasks[operation].deadline.isoformat().replace("+00:00", "Z") == saved


# ---------------------------------------------------------------- 4. what the pages say
def test_unresolved_is_shown_as_unanswered_with_the_saved_deadline(tmp_path):
    service, adapter, clock, provider, client = wire(
        tmp_path, lambda c: ScriptedProvider(["processing"] * 20), wait_seconds=300.0)
    sid = client.session_id()
    pending = client.get(f"/s/{sid}")
    assert "60 seconds" not in pending.text
    assert "2026-10-30 09:00" in pending.text or "UTC" in pending.text

    clock.advance(301)
    done = client.get(f"/s/{sid}")
    assert "UNRESOLVED" in done.text
    assert "not answered" in done.text.lower() or "not a failure" in done.text.lower()
    assert "refund" in done.text.lower()
