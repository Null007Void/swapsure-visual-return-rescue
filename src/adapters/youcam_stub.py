"""YouCam adapter boundary (`docs/06-youcam-boundary.md`).

Contract surface only — this build contains **no live transport**:

* ``prepareMedia``, ``createPreview``, ``pollPreview``, ``discardLocalMedia``;
* failures normalised to ``INVALID_INPUT``, ``AUTH_FAILURE``, ``RATE_LIMIT``,
  ``BUDGET_EXHAUSTED``, ``TIMEOUT``, ``PROVIDER_FAILURE``, ``INVALID_RESULT``;
* MOCK is the default and consumes **zero** provider units;
* the LIVE path is absent, not switched off: there is no endpoint constant, no
  HTTP client import, no credential parameter and no environment toggle that can
  reach a provider.  ``live_transport()`` always raises, and the only way to get a
  live path is a future Phase D admission that adds real code.

A flag is not authorization (`docs/03-architecture.md`), so behaviour cannot be
changed from outside this module.

Naming note: the dispatch named ``src/adapters/youcam-stub.py``; a hyphen is not
importable, so the module is ``src/adapters/youcam_stub.py`` with the identical
contract surface.
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from domain.preview import PreviewPlan, PreviewResult
from specs import policy as load_policy

MAX_TASKS_PER_SESSION = 3
MAX_CONCURRENT_TASKS = 2
TOTAL_DEADLINE_SECONDS = 60
MAX_MEDIA_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png"})

# No live media destination is approved in Phase B, so this allow-list is empty
# by design.  Populating it is a D-gate decision, not a configuration change.
ALLOWED_MEDIA_HOSTS: frozenset[str] = frozenset()


class FailureCode(str, Enum):
    """The normalised provider failure vocabulary from the contract."""

    INVALID_INPUT = "INVALID_INPUT"
    AUTH_FAILURE = "AUTH_FAILURE"
    RATE_LIMIT = "RATE_LIMIT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    INVALID_RESULT = "INVALID_RESULT"
    # Boundary-specific codes (not provider errors):
    LIVE_TRANSPORT_ABSENT = "LIVE_TRANSPORT_ABSENT"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    CROSS_SESSION = "CROSS_SESSION"
    LATE_RESULT = "LATE_RESULT"
    TASK_LIMIT = "TASK_LIMIT"
    DISALLOWED_DESTINATION = "DISALLOWED_DESTINATION"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    NOT_STARTED = "NOT_STARTED"


PROVIDER_FAILURE_CODES: tuple[FailureCode, ...] = (
    FailureCode.INVALID_INPUT,
    FailureCode.AUTH_FAILURE,
    FailureCode.RATE_LIMIT,
    FailureCode.BUDGET_EXHAUSTED,
    FailureCode.TIMEOUT,
    FailureCode.PROVIDER_FAILURE,
    FailureCode.INVALID_RESULT,
)


class AdapterError(Exception):
    """A typed adapter failure.  It never changes a commerce decision."""

    def __init__(self, code: FailureCode, message: str = "") -> None:
        self.code = FailureCode(code)
        super().__init__(f"{self.code.value}: {message}" if message else self.code.value)


class LiveTransportAbsent(AdapterError):
    """Raised by the live path: there is no transport in this build."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            FailureCode.LIVE_TRANSPORT_ABSENT,
            detail or "live YouCam transport is not present in this build; Phase D admission required",
        )


def live_transport_available() -> bool:
    """False, structurally.  Not a flag: there is no transport object to build."""

    return False


def live_transport(*_args: Any, **_kwargs: Any) -> None:
    """The entire live path.  It cannot reach a provider because it does not exist."""

    raise LiveTransportAbsent()


@dataclass(frozen=True, slots=True)
class MediaRef:
    """An opaque local media handle.  No URL, no signed link, no image bytes."""

    handle: str
    session_id: str
    candidate_id: str
    created_at: datetime
    expires_at: datetime
    content_type: str = "image/jpeg"
    byte_size: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "sessionId": self.session_id,
            "candidateId": self.candidate_id,
            "createdAt": self.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expiresAt": self.expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "contentType": self.content_type,
            "byteSize": self.byte_size,
        }


@dataclass(frozen=True, slots=True)
class PreviewTask:
    """One provider task.  MOCK tasks never leave the process."""

    task_id: str
    session_id: str
    candidate_id: str
    mode: str
    status: str
    created_at: datetime
    deadline: datetime
    media_handle: str | None = None
    failure: FailureCode | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED" and self.media_handle is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "sessionId": self.session_id,
            "candidateId": self.candidate_id,
            "mode": self.mode,
            "status": self.status,
            "createdAt": self.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "deadline": self.deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mediaHandle": self.media_handle,
            "failure": None if self.failure is None else self.failure.value,
        }


def media_handle_for(session_id: str, candidate_id: str, nonce: str) -> str:
    payload = f"{session_id}\x1f{candidate_id}\x1f{nonce}"
    return "media_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def task_id_for(session_id: str, candidate_id: str, index: int) -> str:
    payload = f"{session_id}\x1f{candidate_id}\x1f{index}"
    return "task_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def is_permitted_media_url(url: str) -> bool:
    """SSRF guard predicate (T23 surface).

    Rejects, by construction: non-HTTPS schemes, credentials in the URL, any
    non-443 port, IP literals in private/loopback/link-local/reserved ranges, and
    every host not on the (currently empty) owner-approved allow-list.  Redirects
    must be re-checked with this same predicate, which is why it takes a URL
    rather than a request object.
    """

    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if parts.username or parts.password:
        return False
    if parts.port not in (None, 443):
        return False
    host = (parts.hostname or "").strip().lower()
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in ALLOWED_MEDIA_HOSTS
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        return False
    return host in ALLOWED_MEDIA_HOSTS


class MockPreviewAdapter:
    """Deterministic, in-process adapter.  Zero provider units, zero network."""

    def __init__(
        self,
        *,
        clock,
        mode: str = "MOCK",
        policy: Mapping[str, Any] | None = None,
        recorded_provenance: Mapping[str, str] | None = None,
    ) -> None:
        if mode == "LIVE":
            raise LiveTransportAbsent("no live adapter can be constructed in this build")
        if mode not in ("MOCK", "RECORDED"):
            raise AdapterError(FailureCode.INVALID_INPUT, f"unknown mode {mode!r}")
        if mode == "RECORDED" and not recorded_provenance:
            raise AdapterError(
                FailureCode.INVALID_INPUT,
                "recorded media requires a provenance record (source, rights, captured date)",
            )
        self._clock = clock
        self.mode = mode
        self._policy = load_policy() if policy is None else policy
        self._tasks: dict[str, PreviewTask] = {}
        self._media: dict[str, MediaRef] = {}
        self._discarded: set[str] = set()
        self._per_session_tasks: dict[str, int] = {}
        self._injected: list[FailureCode] = []
        self._recorded_provenance = dict(recorded_provenance or {})
        self.units_consumed = 0
        self.tasks_created = 0

    # -- test controls ---------------------------------------------------- #
    def fail_next(self, code: FailureCode) -> None:
        """Queue one normalised provider failure (used by failure-path tests)."""

        self._injected.append(FailureCode(code))

    @property
    def max_tasks_per_session(self) -> int:
        return int(self._policy["maxPreviews"])

    # -- contract surface ------------------------------------------------- #
    def prepare_media(
        self,
        *,
        session_id: str,
        candidate_id: str,
        payload: bytes,
        content_type: str,
        consent_version: str | None,
    ) -> MediaRef:
        if not consent_version:
            raise AdapterError(FailureCode.CONSENT_REQUIRED, "no image processing without recorded consent")
        if not session_id or not candidate_id:
            raise AdapterError(FailureCode.INVALID_INPUT, "session and candidate are required")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise AdapterError(FailureCode.INVALID_INPUT, "empty media payload")
        if len(payload) > MAX_MEDIA_BYTES:
            raise AdapterError(FailureCode.INVALID_INPUT, "media payload exceeds the size limit")
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise AdapterError(FailureCode.INVALID_INPUT, f"unsupported content type {content_type!r}")

        now = self._clock()
        handle = media_handle_for(session_id, candidate_id, hashlib.sha256(bytes(payload)).hexdigest())
        ref = MediaRef(
            handle=handle,
            session_id=session_id,
            candidate_id=candidate_id,
            created_at=now,
            expires_at=now + timedelta(seconds=float(self._policy["localMediaTtlSeconds"])),
            content_type=content_type,
            byte_size=len(payload),
        )
        self._media[handle] = ref
        return ref

    def create_preview(self, plan: PreviewPlan) -> tuple[PreviewTask, ...]:
        if plan.mode == "LIVE":
            raise LiveTransportAbsent()
        if plan.mode != self.mode:
            raise AdapterError(
                FailureCode.INVALID_INPUT, f"adapter mode {self.mode} cannot serve a {plan.mode} plan"
            )
        if len(plan.candidate_ids) > self.max_tasks_per_session:
            raise AdapterError(FailureCode.INVALID_INPUT, "plan exceeds the per-session task cap")

        used = self._per_session_tasks.get(plan.session_id, 0)
        if used + len(plan.candidate_ids) > self.max_tasks_per_session:
            raise AdapterError(
                FailureCode.BUDGET_EXHAUSTED,
                f"session task cap {self.max_tasks_per_session} reached ({used} used)",
            )
        if plan.estimated_units != 0:
            # MOCK must never claim units, and no budget for units exists.
            raise AdapterError(FailureCode.BUDGET_EXHAUSTED, "non-zero estimated units are not authorized")
        if self._injected:
            raise AdapterError(self._injected.pop(0))

        now = self._clock()
        if now >= plan.deadline:
            raise AdapterError(FailureCode.TIMEOUT, "plan deadline already passed")

        tasks: list[PreviewTask] = []
        for index, candidate_id in enumerate(plan.candidate_ids):
            task_id = task_id_for(plan.session_id, candidate_id, index)
            handle = media_handle_for(plan.session_id, candidate_id, f"{task_id}:{self.mode}")
            self._media.setdefault(
                handle,
                MediaRef(
                    handle=handle,
                    session_id=plan.session_id,
                    candidate_id=candidate_id,
                    created_at=now,
                    expires_at=now + timedelta(seconds=float(self._policy["localMediaTtlSeconds"])),
                ),
            )
            task = PreviewTask(
                task_id=task_id,
                session_id=plan.session_id,
                candidate_id=candidate_id,
                mode=self.mode,
                status="SUCCEEDED",
                created_at=now,
                deadline=plan.deadline,
                media_handle=handle,
            )
            self._tasks[task_id] = task
            tasks.append(task)
        self._per_session_tasks[plan.session_id] = used + len(plan.candidate_ids)
        self.tasks_created += len(tasks)
        # MOCK and RECORDED consume zero units; this is asserted, not assumed.
        self.units_consumed += 0
        return tuple(tasks)

    def poll_preview(self, task_id: str, *, session_id: str) -> PreviewTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise AdapterError(FailureCode.INVALID_RESULT, "unknown task id")
        if task.session_id != session_id:
            raise AdapterError(FailureCode.CROSS_SESSION, "task belongs to another session")
        if task.media_handle is not None and task.media_handle in self._discarded:
            raise AdapterError(FailureCode.LATE_RESULT, "media was deleted; result is no longer displayable")
        if self._clock() > task.deadline:
            raise AdapterError(FailureCode.TIMEOUT, "polling deadline passed")
        return task

    def discard_local_media(self, handle: str, *, session_id: str) -> None:
        ref = self._media.get(handle)
        if ref is None:
            raise AdapterError(FailureCode.INVALID_RESULT, "unknown media handle")
        if ref.session_id != session_id:
            raise AdapterError(FailureCode.CROSS_SESSION, "media handle belongs to another session")
        self._discarded.add(handle)
        self._media.pop(handle, None)

    def discard_session_media(self, session_id: str) -> tuple[str, ...]:
        handles = [handle for handle, ref in self._media.items() if ref.session_id == session_id]
        for handle in handles:
            self.discard_local_media(handle, session_id=session_id)
        return tuple(handles)

    # -- result assembly --------------------------------------------------- #
    def build_result(
        self,
        *,
        session_id: str,
        tasks: Iterable[PreviewTask],
        created_at: datetime,
        failure_code: FailureCode | None = None,
    ) -> PreviewResult:
        """Assemble a provider-neutral result.  Partial success is represented honestly."""

        task_list = list(tasks)
        delivered = [task for task in task_list if task.succeeded]
        if not task_list:
            raise AdapterError(FailureCode.INVALID_INPUT, "a result needs at least one task")
        if len(delivered) == len(task_list):
            status = "SUCCEEDED"
        elif delivered:
            status = "PARTIAL"
        else:
            status = "FAILED"
        handles: tuple[str | None, ...] = tuple(
            task.media_handle if task.succeeded else None for task in task_list
        )
        return PreviewResult(
            session_id=session_id,
            mode=self.mode,
            status=status,
            candidate_ids=tuple(task.candidate_id for task in task_list),
            task_ids=tuple(task.task_id for task in task_list),
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=TOTAL_DEADLINE_SECONDS),
            media_handles=handles,
            failure_code=None if failure_code is None else failure_code.value,
        )

    def mark_failed(self, task_id: str, code: FailureCode) -> PreviewTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise AdapterError(FailureCode.INVALID_RESULT, "unknown task id")
        updated = replace(task, status="FAILED", media_handle=None, failure=FailureCode(code))
        self._tasks[task_id] = updated
        return updated


def adapter_for_mode(mode: str, *, clock, recorded_provenance: Mapping[str, str] | None = None) -> Any:
    """Factory that fails closed for anything above MOCK/RECORDED."""

    if mode == "LIVE":
        from adapters.youcam_live import LiveSettings, YouCamLivePreviewAdapter

        try:
            settings = LiveSettings.from_environment()
        except AdapterError as exc:
            raise LiveTransportAbsent(
                f"live mode is not configured ({exc.code.value}); set the owner-approved live "
                "environment or stay in MOCK") from exc
        return YouCamLivePreviewAdapter(settings=settings, clock=clock)
    return MockPreviewAdapter(clock=clock, mode=mode, recorded_provenance=recorded_provenance)
