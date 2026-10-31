"""Preview plan and result value objects — `docs/05-decision-contracts.md`.

These are transport-neutral values.  The adapter that produces them is injected;
nothing here dispatches work or touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

MODES: tuple[str, ...] = ("MOCK", "RECORDED", "LIVE")
# UNRESOLVED means the provider never answered inside the wait: it is not a failure, and the
# task id survives so the same task can be queried later.
PREVIEW_STATUSES: tuple[str, ...] = ("SUCCEEDED", "PARTIAL", "FAILED", "UNRESOLVED")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PreviewPlan:
    """At most three candidates, bounded by a deadline and an explicit budget."""

    session_id: str
    consent_version: str
    candidate_ids: tuple[str, ...]
    provider_version: str
    estimated_units: int
    authorization_ref: str | None
    deadline: datetime
    mode: str = "MOCK"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown preview mode {self.mode!r}")
        if self.mode == "LIVE" and not self.authorization_ref:
            # Phase D boundary: a LIVE plan is constructible only when it carries an
            # owner authorization reference. There is no boolean switch; the adapter
            # factory separately requires credentials, an allow-list and a unit cap.
            raise ValueError("LIVE preview plans require an owner authorization reference")
        if not self.candidate_ids:
            raise ValueError("a preview plan needs at least one candidate")
        if len(self.candidate_ids) > 3:
            raise ValueError("a preview plan carries at most three candidates")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate ids must be distinct")
        if not isinstance(self.estimated_units, int) or isinstance(self.estimated_units, bool):
            raise ValueError("estimated_units must be an integer")
        if self.estimated_units < 0:
            raise ValueError("estimated_units must be non-negative")
        if self.mode != "LIVE" and self.estimated_units != 0:
            raise ValueError("MOCK and RECORDED plans consume zero provider units")
        if self.deadline.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        if self.mode == "LIVE" and not self.authorization_ref:  # pragma: no cover
            raise ValueError("LIVE requires an owner authorization reference")

    def as_dict(self) -> dict[str, object]:
        return {
            "sessionId": self.session_id,
            "consentVersion": self.consent_version,
            "candidateIds": list(self.candidate_ids),
            "providerVersion": self.provider_version,
            "estimatedUnits": self.estimated_units,
            "authorizationRef": self.authorization_ref,
            "deadline": _iso(self.deadline),
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """Provider-neutral result.  A partial or failed run is still a result.

    ``media_handles`` is positionally aligned with ``candidate_ids``; a candidate
    whose preview failed holds ``None``.  Alignment matters: a compacted list would
    silently attribute one candidate's image to another.
    """

    session_id: str
    mode: str
    status: str
    candidate_ids: tuple[str, ...]
    task_ids: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    media_handles: tuple[str | None, ...] = ()
    failure_code: str | None = None
    # Content digest of the shopper photo this result was produced for.
    photo_version: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown preview mode {self.mode!r}")
        if self.status not in PREVIEW_STATUSES:
            raise ValueError(f"unknown preview status {self.status!r}")
        if len(self.task_ids) != len(self.candidate_ids):
            raise ValueError("every candidate needs exactly one task id")
        if self.media_handles and len(self.media_handles) != len(self.candidate_ids):
            raise ValueError("media handles must be aligned with candidate ids")
        if self.status in ("FAILED", "UNRESOLVED") and any(self.media_handles):
            raise ValueError("a failed run must not carry media handles")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.mode == "LIVE" and self.status in ("SUCCEEDED", "PARTIAL") and not any(self.media_handles):
            # Phase D: a live success must carry provider-returned media, never a placeholder.
            raise ValueError("a live preview success must carry provider media handles")
        if self.mode != "LIVE" and any(
                isinstance(h, str) and h.startswith("https://") for h in self.media_handles):
            raise ValueError("provider media cannot appear in a non-live result")

    @property
    def succeeded_candidate_ids(self) -> tuple[str, ...]:
        if self.status == "FAILED":
            return ()
        if not self.media_handles:
            return ()
        return tuple(
            candidate
            for candidate, handle in zip(self.candidate_ids, self.media_handles)
            if handle is not None
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "sessionId": self.session_id,
            "mode": self.mode,
            "status": self.status,
            "candidateIds": list(self.candidate_ids),
            "taskIds": list(self.task_ids),
            "createdAt": _iso(self.created_at),
            "expiresAt": _iso(self.expires_at),
            "mediaHandleCount": sum(1 for handle in self.media_handles if handle is not None),
            "failureCode": self.failure_code,
        }
