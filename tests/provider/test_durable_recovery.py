"""Durable provider-task recovery, the wait deadline, and a page that polls itself.

Every scenario drives the normal HTTP routes; no service method is called to advance a preview.
The provider is an offline fixture: no network, no credential, no unit spent.
"""
from __future__ import annotations

import json

import pytest

from adapters.youcam_live import FailureCode, LiveSettings, TaskRegistry, YouCamLivePreviewAdapter
from harness.clock import ManualClock
from harness.webclient import Client
from ui.service import ReturnService
from ui.webapp import Application

from .durable_support import (S3, START, FakeProvider, build, journey_to_candidates, png,
                              start_previews, upload_photo, write_references)


class TimedProvider(FakeProvider):
    """Answers 'processing' until the clock reaches ``ready_at`` seconds after the first poll."""

    def __init__(self, *, ready_at: float, clock, **kw):
        super().__init__(**kw)
        self.ready_at = ready_at
        self.clock_ref = clock
        self.first_poll = None
        self.revoked = False

    def request(self, method, url, *, headers, body=None, timeout, max_bytes=None):
        if method == "GET" and "/task/cloth-v4/" in url:
            now = self.clock_ref()
            if self.first_poll is None:
                self.first_poll = now
            if (now - self.first_poll).total_seconds() < self.ready_at:
                self.calls.append((method, url))
                return _processing()
        return super().request(method, url, headers=headers, body=body, timeout=timeout)


def _processing():
    from adapters.youcam_live import Response
    return Response(200, json.dumps({"data": {"error": None, "task_status": "processing",
                                              "results": None}}).encode())


class FlakyProvider(FakeProvider):
    """First status GET fails; later ones succeed."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.failed_once = False

    def request(self, method, url, *, headers, body=None, timeout, max_bytes=None, **rest):
        if method == "GET" and "/task/cloth-v4/" in url and not self.failed_once:
            self.failed_once = True
            self.calls.append((method, url))
            from adapters.youcam_live import AdapterError
            raise AdapterError(FailureCode.TIMEOUT, "provider request failed: TimeoutError")
        return super().request(method, url, headers=headers, body=body, timeout=timeout)


def setup(tmp_path, provider_factory, *, wait_seconds=300.0, data_dir=None):
    clock = ManualClock(START)
    provider = provider_factory(clock)
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="test-key", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({"yce-api-01.makeupar.com", S3}),
                              unit_cost=2, unit_cap=8, session_unit_cap=8,
                              wait_seconds=wait_seconds,
                              ledger_path=str(tmp_path / "units.sqlite3")),
        clock=lambda: clock.now, transport=provider)
    service = ReturnService(data_dir or (tmp_path / "data"), clock=clock, preview_mode="LIVE",
                            adapter=adapter)
    return Application(service), service, adapter, clock, provider


def ready_for_previews(tmp_path, provider_factory, **kw):
    app, service, adapter, clock, provider = setup(tmp_path, provider_factory, **kw)
    client = Client(app)
    journey_to_candidates(client)
    write_references(service, [c.sku for c in service.get_case(client.session_id()).selection.candidates])
    assert upload_photo(client).status == 303
    assert start_previews(client).status == 303
    return app, service, adapter, clock, provider, client


# ------------------------------------------------------------------ 1. idle browser
def test_idle_browser_progresses_to_a_late_image(tmp_path):
    app, service, adapter, clock, provider, client = ready_for_previews(
        tmp_path, lambda c: TimedProvider(ready_at=90, clock=lambda: c.now))
    sid = client.session_id()

    pending = client.get(f"/s/{sid}")
    assert pending.status == 200
    assert 'http-equiv="refresh" content="5"' in pending.text      # it asks for itself
    assert "<img " not in pending.text
    assert "still being generated" in pending.text

    # Nothing but the page's own reload cadence: no service call, no manual navigation.
    for _ in range(3):
        clock.advance(5)
        assert client.get(f"/s/{sid}").status == 200

    clock.advance(120)                                   # the provider finishes late
    done = client.get(f"/s/{sid}")
    assert "Your decision" in done.text
    assert f"/s/{sid}/preview/" in done.text
    image = client.get(f"/s/{sid}/preview/{service.get_case(sid).session.preview_result.candidate_ids[0]}")
    assert image.status == 200 and image.headers.get("content-type") == "image/png"
    assert len(provider.task_calls()) == 2               # one task, never two
    assert adapter.units_consumed == 4


# ------------------------------------------------------------------ 2. restart after ack
def test_restart_after_acknowledgement_queries_the_same_task(tmp_path):
    clock = ManualClock(START)
    first_provider = TimedProvider(ready_at=10_000, clock=lambda: clock.now)   # never answers
    ledger = str(tmp_path / "units.sqlite3")
    adapter_a = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="test-key", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({"yce-api-01.makeupar.com", S3}),
                              unit_cost=2, unit_cap=8, session_unit_cap=8, ledger_path=ledger),
        clock=lambda: clock.now, transport=first_provider)
    service_a = ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE", adapter=adapter_a)
    client = Client(Application(service_a))
    journey_to_candidates(client)
    write_references(service_a, [c.sku for c in service_a.get_case(client.session_id()).selection.candidates])
    upload_photo(client)
    start_previews(client)
    sid = client.session_id()
    assert len(first_provider.task_calls()) == 2      # one task per candidate
    operation = _operation(adapter_a, sid)
    recorded = adapter_a._registry.lookup(operation)
    assert recorded is not None, "the provider task id must be on record before the restart"
    provider_task_id = recorded["provider_task_id"]

    # A new process: new adapter, new coordinator instance, same durable ledger and data dir.
    second_provider = FakeProvider()
    adapter_b = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="test-key", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({"yce-api-01.makeupar.com", S3}),
                              unit_cost=2, unit_cap=8, session_unit_cap=8, ledger_path=ledger),
        clock=lambda: clock.now, transport=second_provider)
    service_b = ReturnService(tmp_path / "data", clock=clock, preview_mode="LIVE", adapter=adapter_b)
    client_b = Client(Application(service_b), cookies=dict(client.cookies))
    done = client_b.get(f"/s/{sid}")
    assert "Your decision" in done.text, done.text[:300]
    assert second_provider.task_calls() == []              # nothing new was created
    assert adapter_b.tasks_created == 0
    # Every status query the second process made addressed a task id the first one recorded.
    recorded = {adapter_a._registry.lookup(op)["provider_task_id"] for op in adapter_a._tasks}
    polled = {url.rsplit("/", 1)[-1] for method, url in second_provider.calls
              if method == "GET" and "/task/cloth-v4/" in url}
    assert polled and polled <= recorded
    assert adapter_b.units_consumed == 4


def _operation(adapter, session_id):
    task_ids = list(adapter._tasks)
    return task_ids[0] if task_ids else None


# ------------------------------------------------------------------ 3. persistence failure
def test_persistence_failure_keeps_the_spend_and_never_retries(tmp_path, monkeypatch):
    from adapters.youcam_live import AdapterError, ReservationLedger

    def boom(self, **kw):
        # Exactly what the real code does when the durable write fails: keep the money counted,
        # mark the hold uncertain, and refuse to continue.
        self._retain_uncertain(kw["operation_id"], kw["now"])
        raise AdapterError(FailureCode.RECOVERY_REQUIRED,
                           "the provider task exists but could not be recorded; the reservation stays uncertain")

    monkeypatch.setattr(ReservationLedger, "confirm_with_task", boom)
    app, service, adapter, clock, provider, client = ready_for_previews(tmp_path, lambda c: FakeProvider())
    sid = client.session_id()

    page = client.get(f"/s/{sid}")
    assert "Your decision" in page.text
    assert "manual check" in page.text.lower()
    assert len(provider.task_calls()) == 2
    assert adapter.units_consumed == 4                     # the money stays spent, the hold is kept
    rows = adapter._ledger.outstanding()
    assert [row["state"] for row in rows] == ["UNCERTAIN", "UNCERTAIN"]   # held, never released
    assert adapter._ledger.committed_units() == 4


# ------------------------------------------------------------------ 4. transient poll failure
def test_transient_poll_failure_recovers_on_the_next_tick(tmp_path):
    app, service, adapter, clock, provider, client = ready_for_previews(
        tmp_path, lambda c: FlakyProvider(), wait_seconds=300.0)
    sid = client.session_id()

    still = client.get(f"/s/{sid}")
    assert still.status == 200
    assert "Your decision" not in still.text               # a transport hiccup is not a verdict
    assert 'http-equiv="refresh" content="5"' in still.text

    clock.advance(5)                        # the page refreshes every 5 seconds
    done = client.get(f"/s/{sid}")
    assert "Your decision" in done.text
    operation = _operation(adapter, sid)
    outcomes = [row["outcome"] for row in adapter._registry.polls(operation)]
    assert any(o.startswith("ERROR:") for o in outcomes)
    assert any(o == "SUCCEEDED" for o in outcomes)
    assert len(provider.task_calls()) == 2


# ------------------------------------------------------------------ 5. duplicate tabs
def test_two_tabs_poll_without_creating_a_second_task(tmp_path):
    app, service, adapter, clock, provider, client = ready_for_previews(
        tmp_path, lambda c: TimedProvider(ready_at=0, clock=lambda: c.now))
    sid = client.session_id()
    first = Client(app, cookies=dict(client.cookies))
    second = Client(app, cookies=dict(client.cookies))
    assert first.get(f"/s/{sid}").status == 200
    assert second.get(f"/s/{sid}").status == 200
    assert len(provider.task_calls()) == 2
    assert adapter.units_consumed == 4
    assert service.get_case(sid).state == "DECISION_READY"


# ------------------------------------------------------------------ 6. revoked consent
def test_revoked_consent_stops_polling_and_keeps_the_refund(tmp_path):
    app, service, adapter, clock, provider, client = ready_for_previews(
        tmp_path, lambda c: TimedProvider(ready_at=10_000, clock=lambda: c.now))
    sid = client.session_id()
    pending = client.get(f"/s/{sid}")
    assert "still being generated" in pending.text
    polls_before = len([c for c in provider.calls if "/task/cloth-v4/" in c[1]])

    panel = client.get(f"/s/{sid}/consent-panel")
    assert client.post(f"/s/{sid}/revoke", {"request_token": client.form_token(panel.text)}).status == 303

    after = client.get(f"/s/{sid}")
    assert after.status == 200
    assert "refund" in after.text.lower()
    polls_after = len([c for c in provider.calls if "/task/cloth-v4/" in c[1]])
    assert polls_after == polls_before, "revocation must stop provider polling"
    assert len(provider.task_calls()) == 2
