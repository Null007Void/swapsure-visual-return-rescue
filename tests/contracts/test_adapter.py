"""YouCam adapter boundary — contract surface, typed failures, absent live path.

T27 is the centrepiece: a "live" flag, environment variable or policy edit does
not create a transport, because no transport exists in this tree.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from adapters.youcam_stub import (
    ALLOWED_MEDIA_HOSTS,
    MAX_MEDIA_BYTES,
    PROVIDER_FAILURE_CODES,
    AdapterError,
    FailureCode,
    LiveTransportAbsent,
    MockPreviewAdapter,
    adapter_for_mode,
    is_permitted_media_url,
    live_transport,
    live_transport_available,
)
from domain.preview import PreviewPlan
from specs import policy as load_policy


@pytest.fixture
def adapter(catalog):
    return MockPreviewAdapter(clock=lambda: catalog.clock)


def plan_for(session_id: str, candidates, deadline, mode: str = "MOCK"):
    return PreviewPlan(
        session_id=session_id,
        consent_version="consent-v1",
        candidate_ids=tuple(candidates),
        provider_version="mock-v1",
        estimated_units=0,
        authorization_ref=None,
        deadline=deadline,
        mode=mode,
    )


# --------------------------------------------------------------------------- #
# Surface
# --------------------------------------------------------------------------- #
def test_contract_surface_is_present(adapter):
    for method in ("prepare_media", "create_preview", "poll_preview", "discard_local_media"):
        assert callable(getattr(adapter, method))


def test_failure_vocabulary_matches_the_contract():
    assert {code.value for code in PROVIDER_FAILURE_CODES} == {
        "INVALID_INPUT",
        "AUTH_FAILURE",
        "RATE_LIMIT",
        "BUDGET_EXHAUSTED",
        "TIMEOUT",
        "PROVIDER_FAILURE",
        "INVALID_RESULT",
    }


# --------------------------------------------------------------------------- #
# MOCK: zero units, bounded tasks
# --------------------------------------------------------------------------- #
def test_mock_consumes_zero_units(adapter, catalog):
    tasks = adapter.create_preview(plan_for("ses-1", ["NAVY-84", "GREEN-99"], catalog.clock + timedelta(seconds=60)))
    assert len(tasks) == 2
    assert adapter.units_consumed == 0
    assert all(task.mode == "MOCK" for task in tasks)
    assert all(task.status == "SUCCEEDED" for task in tasks)


def test_mock_plan_may_not_claim_units(catalog):
    with pytest.raises(ValueError):
        PreviewPlan(
            session_id="ses-1",
            consent_version="consent-v1",
            candidate_ids=("NAVY-84",),
            provider_version="mock-v1",
            estimated_units=2,
            authorization_ref=None,
            deadline=catalog.clock + timedelta(seconds=60),
        )


def test_plan_carries_at_most_three_candidates(catalog):
    with pytest.raises(ValueError):
        plan_for("ses-1", ["A", "B", "C", "D"], catalog.clock + timedelta(seconds=60))


def test_session_task_cap_is_enforced(adapter, catalog):
    deadline = catalog.clock + timedelta(seconds=60)
    adapter.create_preview(plan_for("ses-1", ["A", "B", "C"], deadline))
    with pytest.raises(AdapterError) as excinfo:
        adapter.create_preview(plan_for("ses-1", ["D"], deadline))
    assert excinfo.value.code is FailureCode.BUDGET_EXHAUSTED


def test_plan_deadline_blocks_creation(catalog):
    adapter = MockPreviewAdapter(clock=lambda: catalog.clock + timedelta(seconds=120))
    with pytest.raises(AdapterError) as excinfo:
        adapter.create_preview(
            plan_for("ses-1", ["NAVY-84"], catalog.clock + timedelta(seconds=60))
        )
    assert excinfo.value.code is FailureCode.TIMEOUT


# --------------------------------------------------------------------------- #
# Typed provider failures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", PROVIDER_FAILURE_CODES, ids=[code.value for code in PROVIDER_FAILURE_CODES])
def test_provider_failures_are_normalised(adapter, catalog, code):
    adapter.fail_next(code)
    with pytest.raises(AdapterError) as excinfo:
        adapter.create_preview(
            plan_for("ses-1", ["NAVY-84"], catalog.clock + timedelta(seconds=60))
        )
    assert excinfo.value.code is code


def test_failure_injection_is_consumed_once(adapter, catalog):
    adapter.fail_next(FailureCode.RATE_LIMIT)
    deadline = catalog.clock + timedelta(seconds=60)
    with pytest.raises(AdapterError):
        adapter.create_preview(plan_for("ses-1", ["NAVY-84"], deadline))
    assert len(adapter.create_preview(plan_for("ses-1", ["NAVY-84"], deadline))) == 1


# --------------------------------------------------------------------------- #
# Media handling
# --------------------------------------------------------------------------- #
def test_media_requires_recorded_consent(adapter):
    with pytest.raises(AdapterError) as excinfo:
        adapter.prepare_media(
            session_id="ses-1",
            candidate_id="NAVY-84",
            payload=b"jpegbytes",
            content_type="image/jpeg",
            consent_version=None,
        )
    assert excinfo.value.code is FailureCode.CONSENT_REQUIRED


def test_invalid_media_is_rejected_without_retention(adapter):
    cases = {
        "empty": (b"", "image/jpeg"),
        "oversized": (b"x" * (MAX_MEDIA_BYTES + 1), "image/jpeg"),
        "wrong-type": (b"gifbytes", "image/gif"),
        "not-bytes": ("not-bytes", "image/jpeg"),
    }
    for label, (payload, content_type) in cases.items():
        with pytest.raises(AdapterError) as excinfo:
            adapter.prepare_media(
                session_id="ses-1",
                candidate_id="NAVY-84",
                payload=payload,
                content_type=content_type,
                consent_version="consent-v1",
            )
        assert excinfo.value.code is FailureCode.INVALID_INPUT, label
    assert adapter._media == {}


def test_media_handle_is_opaque_and_expires(adapter):
    ref = adapter.prepare_media(
        session_id="ses-1",
        candidate_id="NAVY-84",
        payload=b"jpegbytes",
        content_type="image/jpeg",
        consent_version="consent-v1",
    )
    assert ref.handle.startswith("media_")
    assert "http" not in ref.handle and "/" not in ref.handle
    assert ref.expires_at > ref.created_at


def test_deleted_media_cannot_be_polled(adapter, catalog):
    deadline = catalog.clock + timedelta(seconds=60)
    task = adapter.create_preview(plan_for("ses-1", ["NAVY-84"], deadline))[0]
    adapter.discard_local_media(task.media_handle, session_id="ses-1")
    with pytest.raises(AdapterError) as excinfo:
        adapter.poll_preview(task.task_id, session_id="ses-1")
    assert excinfo.value.code is FailureCode.LATE_RESULT


def test_cross_session_polling_is_denied(adapter, catalog):
    deadline = catalog.clock + timedelta(seconds=60)
    task = adapter.create_preview(plan_for("ses-1", ["NAVY-84"], deadline))[0]
    with pytest.raises(AdapterError) as excinfo:
        adapter.poll_preview(task.task_id, session_id="ses-2")
    assert excinfo.value.code is FailureCode.CROSS_SESSION


def test_unknown_task_is_invalid_result(adapter):
    with pytest.raises(AdapterError) as excinfo:
        adapter.poll_preview("task_missing", session_id="ses-1")
    assert excinfo.value.code is FailureCode.INVALID_RESULT


def test_polling_after_the_deadline_times_out(catalog):
    clock_holder = {"now": catalog.clock}
    adapter = MockPreviewAdapter(clock=lambda: clock_holder["now"])
    task = adapter.create_preview(
        plan_for("ses-1", ["NAVY-84"], catalog.clock + timedelta(seconds=60))
    )[0]
    clock_holder["now"] = catalog.clock + timedelta(seconds=61)
    with pytest.raises(AdapterError) as excinfo:
        adapter.poll_preview(task.task_id, session_id="ses-1")
    assert excinfo.value.code is FailureCode.TIMEOUT


# --------------------------------------------------------------------------- #
# Results and mode labelling
# --------------------------------------------------------------------------- #
def test_partial_failure_is_reported_honestly(adapter, catalog):
    deadline = catalog.clock + timedelta(seconds=60)
    tasks = adapter.create_preview(plan_for("ses-1", ["A", "B", "C"], deadline))
    tasks = (tasks[0], adapter.mark_failed(tasks[1].task_id, FailureCode.PROVIDER_FAILURE), tasks[2])
    result = adapter.build_result(session_id="ses-1", tasks=tasks, created_at=catalog.clock)
    assert result.status == "PARTIAL"
    assert result.mode == "MOCK"
    assert result.succeeded_candidate_ids == ("A", "C")


def test_all_failures_are_reported_as_failed(adapter, catalog):
    deadline = catalog.clock + timedelta(seconds=60)
    tasks = adapter.create_preview(plan_for("ses-1", ["A"], deadline))
    failed = (adapter.mark_failed(tasks[0].task_id, FailureCode.TIMEOUT),)
    result = adapter.build_result(
        session_id="ses-1", tasks=failed, created_at=catalog.clock, failure_code=FailureCode.TIMEOUT
    )
    assert result.status == "FAILED"
    assert result.succeeded_candidate_ids == ()
    assert result.failure_code == "TIMEOUT"


def test_recorded_mode_requires_provenance(catalog):
    with pytest.raises(AdapterError) as excinfo:
        MockPreviewAdapter(clock=lambda: catalog.clock, mode="RECORDED")
    assert excinfo.value.code is FailureCode.INVALID_INPUT


def test_recorded_mode_is_labelled(catalog):
    adapter = MockPreviewAdapter(
        clock=lambda: catalog.clock,
        mode="RECORDED",
        recorded_provenance={"source": "synthetic", "rights": "owner-owned", "captured": "2026-10-25"},
    )
    tasks = adapter.create_preview(
        plan_for("ses-1", ["A"], catalog.clock + timedelta(seconds=60), mode="RECORDED")
    )
    result = adapter.build_result(session_id="ses-1", tasks=tasks, created_at=catalog.clock)
    assert result.mode == "RECORDED"


# --------------------------------------------------------------------------- #
# T27 — the live path does not exist
# --------------------------------------------------------------------------- #
def test_t27_live_transport_is_absent():
    assert live_transport_available() is False
    with pytest.raises(LiveTransportAbsent):
        live_transport()
    with pytest.raises(LiveTransportAbsent):
        adapter_for_mode("LIVE", clock=lambda: None)
    with pytest.raises(LiveTransportAbsent):
        MockPreviewAdapter(clock=lambda: None, mode="LIVE")


def test_t27_environment_flags_do_not_create_a_transport(monkeypatch):
    for name in ("YOUCAM_LIVE", "YOUCAM_API_KEY", "YOUCAM_ENDPOINT", "LIVE_API_ALLOWED"):
        monkeypatch.setenv(name, "1")
    assert live_transport_available() is False
    with pytest.raises(LiveTransportAbsent):
        adapter_for_mode("LIVE", clock=lambda: None)


def test_t27_a_policy_flag_alone_is_not_authorization(catalog):
    """Even if policy.json said liveApiAllowed, there is still no transport."""

    permissive = dict(load_policy())
    permissive["liveApiAllowed"] = True
    permissive["mode"] = "LIVE"
    adapter = MockPreviewAdapter(clock=lambda: catalog.clock, policy=permissive)
    assert adapter.units_consumed == 0
    with pytest.raises(LiveTransportAbsent):
        live_transport()
    assert not hasattr(MockPreviewAdapter, "endpoint")
    assert not hasattr(adapter, "endpoint")
    assert not hasattr(adapter, "base_url")


def test_live_policy_caps_are_zero():
    policy = load_policy()
    assert policy["mode"] == "MOCK"
    assert policy["liveApiAllowed"] is False
    assert policy["liveCallCap"] == 0
    assert policy["liveUnitCap"] == 0
    assert policy["secretsAllowed"] is False
    assert policy["deploymentAllowed"] is False
    assert policy["submissionAllowed"] is False


# --------------------------------------------------------------------------- #
# T23 surface — media destination predicate is fail-closed
# --------------------------------------------------------------------------- #
def test_t23_no_live_media_destination_is_approved():
    assert ALLOWED_MEDIA_HOSTS == frozenset()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x.jpg",
        "https://localhost/x.jpg",
        "https://127.0.0.1/x.jpg",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/x.jpg",
        "https://192.168.1.10/x.jpg",
        "https://[::1]/x.jpg",
        "https://0.0.0.0/x.jpg",
        "https://user:pass@example.com/x.jpg",
        "https://example.com:8080/x.jpg",
        "file:///etc/passwd",
        "https://example.com/x.jpg",
        "",
        None,
    ],
)
def test_t23_unsafe_or_unapproved_media_urls_are_rejected(url):
    assert is_permitted_media_url(url) is False
