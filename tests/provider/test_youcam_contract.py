"""Contract, identity, reservation and destination regressions (review findings 3-6).

Pinned to the official documented shapes: the success body is
`{"data": {"results": {"url": ...}, "task_status": "success"}}`, file registration returns
`data.files[].file_id` + `requests[].{method,url,headers}`, task creation returns `data.task_id`.
"""
from __future__ import annotations

import json
import socket
import struct
from datetime import datetime, timedelta, timezone

import pytest

from adapters.youcam_live import (ConsentGrant, LiveSettings, Response, Transport,
                                  YouCamLivePreviewAdapter)
from adapters.youcam_stub import AdapterError, FailureCode, PreviewTask
from domain.preview import PreviewPlan
from tests.provider.images import (jpeg, jpeg_header_only, png, png_header_only, png_noise, png_truncated)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
HOST = "yce-api-01.makeupar.com"
S3 = "yce-us.s3-accelerate.amazonaws.com"


def ok(body, status=200):
    return Response(status, json.dumps(body).encode())


def registration(file_id="f1"):
    return ok({"data": {"files": [{"file_id": file_id,
                                   "requests": [{"method": "PUT",
                                                 "url": f"https://{S3}/u/{file_id}",
                                                 "headers": {"Content-Type": "image/jpg"}}]}]}})


class Fake(Transport):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body=None, timeout, max_bytes=None):
        self.calls.append({"method": method, "url": url, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def consent(**over):
    base = dict(session_id="ses_1", generation=1, version="v1",
                granted_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=30))
    base.update(over)
    return ConsentGrant(**base)


def adapter(tmp_path, responses, *, clock=lambda: NOW, **over):
    base = dict(api_key="k", authorization_ref="OWNER-D-1",
                allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2, unit_cap=10,
                session_unit_cap=10, ledger_path=str(tmp_path / "units.sqlite3"))
    base.update(over)
    return YouCamLivePreviewAdapter(settings=LiveSettings(**base), clock=clock,
                                    transport=Fake(responses))


def prepare(a, sku="cand-0", grant=None):
    return a.prepare_media(session_id="ses_1", candidate_id=sku, payload=png(),
                           content_type="image/png", consent=grant or consent(),
                           reference_rights="licensed:demo", reference_payload=png(), garment_category="full_body",)


def plan(skus=("cand-0",), *, ref="OWNER-D-1", version="cloth-v4"):
    return PreviewPlan(session_id="ses_1", consent_version="v1", candidate_ids=tuple(skus),
                       provider_version=version, estimated_units=2 * len(skus),
                       authorization_ref=ref, deadline=NOW + timedelta(seconds=60), mode="LIVE")


HAPPY = [registration("s1"), Response(204, b""), registration("r1"), Response(204, b""),
         ok({"data": {"task_id": "p1"}})]


def test_official_success_body_is_an_object_with_url(tmp_path):
    poll = ok({"status": 200, "data": {"error": None, "results": {"url": f"https://{S3}/out.png"},
                                       "task_status": "success"}})
    a = adapter(tmp_path, HAPPY + [poll])
    prepare(a)
    task = a.create_preview(plan())[0]
    done = a.poll_preview(task.task_id, session_id="ses_1")
    assert done.status == "SUCCEEDED" and done.media_handle == f"https://{S3}/out.png"


def test_list_shaped_results_are_rejected_as_malformed(tmp_path):
    poll = ok({"data": {"results": [{"url": f"https://{S3}/out.png"}], "task_status": "success"}})
    a = adapter(tmp_path, HAPPY + [poll])
    prepare(a)
    task = a.create_preview(plan())[0]
    with pytest.raises(AdapterError) as exc:
        a.poll_preview(task.task_id, session_id="ses_1")
    assert exc.value.code is FailureCode.INVALID_RESULT


def test_pending_statuses_are_preserved_not_failed(tmp_path):
    poll = ok({"data": {"results": None, "task_status": "processing"}})
    a = adapter(tmp_path, HAPPY + [poll])
    prepare(a)
    task = a.create_preview(plan())[0]
    assert a.poll_preview(task.task_id, session_id="ses_1").status == "PENDING"


def test_reordered_and_restarted_requests_cannot_mint_extra_tasks(tmp_path):
    """Identity is per (session, candidate, authority, version), so order and restarts are inert."""
    first = adapter(tmp_path, HAPPY + HAPPY)
    prepare(first, "cand-0")
    prepare(first, "cand-1")
    first.create_preview(plan(("cand-0", "cand-1")))
    assert first.units_consumed == 4
    # A restarted process, same durable ledger, the candidates in the opposite order.
    second = adapter(tmp_path, [])                 # no scripted responses: nothing may be sent
    prepare(second, "cand-1")
    prepare(second, "cand-0")
    adopted = second.create_preview(plan(("cand-1", "cand-0")))
    # Recovery, not a second purchase: both operations are adopted from the durable record.
    assert len(adopted) == 2
    assert all(task.status == "PENDING" and task.task_id for task in adopted)
    assert second.tasks_created == 0
    assert second.units_consumed == 4


def test_redirect_after_task_post_retains_the_allowance(tmp_path):
    a = adapter(tmp_path, HAPPY[:4] + [Response(302, b"", {"location": "https://evil.example/x"})])
    prepare(a)
    with pytest.raises(AdapterError):
        a.create_preview(plan())
    assert a.units_consumed == 2, "a redirected task POST may have been billed"
    assert [r["state"] for r in a.outstanding_reservations()] == ["UNCERTAIN"]


def test_predispatch_destination_refusal_is_free(tmp_path):
    """A refusal made before transmission releases the hold and never reaches the provider.

    The fixture queue is empty, so any attempt to send would raise IndexError instead of the
    AdapterError this asserts.
    """
    a = adapter(tmp_path, [])
    ref = prepare(a)
    a._consent[ref.handle] = consent(revoked_at=NOW - timedelta(seconds=1))
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan())
    assert exc.value.code is FailureCode.CONSENT_REQUIRED
    assert a._transport.calls == []
    assert a.units_consumed == 0


def test_plan_authority_must_match_the_configured_authority(tmp_path):
    a = adapter(tmp_path, HAPPY)
    prepare(a)
    with pytest.raises(AdapterError) as exc:
        a.create_preview(plan(ref="SOMEONE-ELSE"))
    assert exc.value.code is FailureCode.AUTH_FAILURE
    assert a.units_consumed == 0


def test_terminal_success_still_requires_current_consent(tmp_path):
    poll = ok({"data": {"results": {"url": f"https://{S3}/out.png"}, "task_status": "success"}})
    a = adapter(tmp_path, HAPPY + [poll])
    ref = prepare(a)
    task = a.create_preview(plan())[0]
    done = a.poll_preview(task.task_id, session_id="ses_1")
    assert done.status == "SUCCEEDED"
    a._consent[ref.handle] = consent(revoked_at=NOW - timedelta(seconds=1))
    with pytest.raises(AdapterError) as exc:
        a.poll_preview(task.task_id, session_id="ses_1")
    assert exc.value.code is FailureCode.CONSENT_REQUIRED
    with pytest.raises(AdapterError):
        a.build_result(session_id="ses_1", plan=plan(), tasks=(done,))


def test_revocation_survives_media_deletion(tmp_path):
    a = adapter(tmp_path, HAPPY)
    ref = prepare(a)
    a.discard_local_media(ref.handle, session_id="ses_1")
    assert a._consent.get(ref.handle) is None
    # With the media gone there is nothing to send, and no consent is left behind to authorise it.
    with pytest.raises(AdapterError):
        a.create_preview(plan())


def test_destination_resolving_to_a_private_address_is_refused_at_connect(tmp_path, monkeypatch):
    real = Transport()
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                          ("127.0.0.1", 443))])
    with pytest.raises(AdapterError) as exc:
        real.request("POST", f"https://{HOST}/s2s/v2.0/file", headers={}, body=b"{}", timeout=1)
    assert exc.value.code is FailureCode.DISALLOWED_DESTINATION


def test_oversized_error_body_is_bounded(tmp_path):
    from adapters.youcam_live import MAX_RESPONSE_BYTES
    a = adapter(tmp_path, [Response(400, b"x" * (MAX_RESPONSE_BYTES + 1))])
    prepare(a)
    with pytest.raises(AdapterError):
        a.create_preview(plan())


def test_deadline_is_not_padded_to_a_minimum_timeout(tmp_path):
    seen = {}

    class Spy(Fake):
        def request(self, method, url, *, headers, body=None, timeout, max_bytes=None):
            seen["timeout"] = timeout
            return super().request(method, url, headers=headers, body=body, timeout=timeout)

    a = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="k", authorization_ref="OWNER-D-1",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2, unit_cap=10,
                              session_unit_cap=10, request_timeout=30.0,
                              ledger_path=str(tmp_path / "u3.sqlite3")),
        clock=lambda: NOW, transport=Spy(HAPPY))
    prepare(a)
    tight = PreviewPlan(session_id="ses_1", consent_version="v1", candidate_ids=("cand-0",),
                        provider_version="cloth-v4", estimated_units=2, authorization_ref="OWNER-D-1",
                        deadline=NOW + timedelta(seconds=0.2), mode="LIVE")
    a.create_preview(tight)
    assert seen["timeout"] < 30.0 and seen["timeout"] <= 0.2
