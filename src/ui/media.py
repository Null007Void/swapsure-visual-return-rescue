"""Local media boundary: synthetic placeholders, scoped handles, TTL, deletion.

Nothing here is a YouCam result.  A preview artefact is a locally generated SVG
card that says so, written outside any public/static directory and served only
through a session-scoped route.  The adapter's opaque handle names the file; the
shopper's session must match the row before a byte is returned.

Deletion is local and retriable: a failed unlink is recorded as
``PENDING_RETRY`` in the persistent deletion ledger and retried by ``sweep`` on a
later start.  Provider-side deletion is reported as a separate, unverified status
because this build has no provider.
"""

from __future__ import annotations

import hashlib
import html
import os
import pathlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from specs import policy as load_policy
from ui.store import Store, iso, parse_iso

MEDIA_CONTENT_TYPE = "image/svg+xml"
PROVIDER_STATUS_MOCK = "NOT_APPLICABLE_MOCK"
PROVIDER_STATUS_UNVERIFIED = "PROVIDER_RETENTION_UNVERIFIED"


class MediaError(Exception):
    """A typed media access/purge failure with an HTTP status class."""

    def __init__(self, code: str, message: str, *, status: int) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def opaque_handle(session_id: str, candidate_id: str) -> str:
    payload = f"{session_id}\x1f{candidate_id}\x1f{secrets.token_hex(8)}"
    return "media_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _svg(sku: str, session_id: str) -> bytes:
    label = html.escape(sku, quote=True)
    session = html.escape(session_id, quote=True)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="640" '
        'viewBox="0 0 480 640" role="img" '
        'aria-label="Mock placeholder card for SKU ' + label + '. Not a YouCam result.">\n'
        '  <rect width="480" height="640" fill="#f4f1ea"/>\n'
        '  <rect x="24" y="24" width="432" height="592" fill="none" '
        'stroke="#7a7368" stroke-width="3" stroke-dasharray="10 8"/>\n'
        '  <text x="240" y="150" font-family="monospace" font-size="30" '
        'text-anchor="middle" fill="#1d1b18">MOCK PREVIEW</text>\n'
        '  <text x="240" y="196" font-family="monospace" font-size="18" '
        'text-anchor="middle" fill="#1d1b18">PLACEHOLDER</text>\n'
        '  <text x="240" y="232" font-family="monospace" font-size="16" '
        'text-anchor="middle" fill="#514b42">NOT A YOUCAM RESULT</text>\n'
        '  <text x="240" y="320" font-family="monospace" font-size="26" '
        'text-anchor="middle" fill="#1d1b18">' + label + '</text>\n'
        '  <text x="240" y="380" font-family="sans-serif" font-size="15" '
        'text-anchor="middle" fill="#514b42">Garment outline omitted.</text>\n'
        '  <text x="240" y="404" font-family="sans-serif" font-size="15" '
        'text-anchor="middle" fill="#514b42">No photo of you is processed.</text>\n'
        '  <text x="240" y="470" font-family="sans-serif" font-size="14" '
        'text-anchor="middle" fill="#514b42">Visual preview only -</text>\n'
        '  <text x="240" y="492" font-family="sans-serif" font-size="14" '
        'text-anchor="middle" fill="#514b42">not a fit or sizing guarantee</text>\n'
        '  <text x="240" y="580" font-family="monospace" font-size="11" '
        'text-anchor="middle" fill="#6b6459">' + session[:26] + '</text>\n'
        '</svg>\n'
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class MediaView:
    handle: str
    session_id: str
    candidate_id: str
    created_at: datetime
    expires_at: datetime
    local_status: str
    provider_status: str
    path: pathlib.Path

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MediaView":
        return cls(
            handle=str(row["handle"]),
            session_id=str(row["session_id"]),
            candidate_id=str(row["candidate_id"]),
            created_at=parse_iso(str(row["created_at"])),
            expires_at=parse_iso(str(row["expires_at"])),
            local_status=str(row["local_status"]),
            provider_status=str(row["provider_status"]),
            path=pathlib.Path(str(row["path"])),
        )


class MediaManager:
    def __init__(
        self,
        root: str | pathlib.Path,
        store: Store,
        *,
        clock: Callable[[], datetime],
        unlink: Callable[[str], None] | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self._clock = clock
        self._unlink = unlink or os.unlink
        self.ttl_seconds = (
            int(load_policy()["localMediaTtlSeconds"]) if ttl_seconds is None else int(ttl_seconds)
        )

    # -- creation -------------------------------------------------------- #
    def create_placeholder(
        self,
        *,
        session_id: str,
        candidate_id: str,
        sku: str,
        now: datetime | None = None,
    ) -> MediaView:
        created = now or self._clock()
        handle = opaque_handle(session_id, candidate_id)
        path = self.root / f"{handle}.svg"
        payload = _svg(sku, session_id)
        path.write_bytes(payload)
        expires = created + timedelta(seconds=self.ttl_seconds)
        row = {
            "handle": handle,
            "session_id": session_id,
            "candidate_id": candidate_id,
            "path": str(path),
            "content_type": MEDIA_CONTENT_TYPE,
            "byte_size": len(payload),
            "created_at": iso(created),
            "expires_at": iso(expires),
            "deleted_at": None,
            "local_status": "PRESENT",
            "provider_status": PROVIDER_STATUS_MOCK,
            "attempts": 0,
            "last_error": None,
        }
        self.store.add_media(row)
        return MediaView.from_row(row)

    # -- reads ----------------------------------------------------------- #
    def resolve(self, handle: str, *, session_id: str) -> tuple[MediaView, bytes]:
        """Return the artefact only for its own live, unexpired session."""

        row = self.store.get_media(handle)
        if row is None:
            raise MediaError("UNKNOWN_MEDIA", "no such media handle", status=404)
        view = MediaView.from_row(row)
        if view.session_id != session_id:
            # Deliberately indistinguishable from a missing handle to a caller
            # outside the owning session, but typed so the app can log the attempt.
            raise MediaError("MEDIA_SESSION_DENIED", "media belongs to another session", status=403)
        if view.local_status != "PRESENT":
            raise MediaError("MEDIA_DELETED", "media was deleted and cannot be redisplayed", status=410)
        now = self._clock()
        if now >= view.expires_at:
            self.purge(handle, session_id=session_id, reason="TTL_EXPIRED")
            raise MediaError("MEDIA_EXPIRED", "media expired and was removed", status=410)
        try:
            payload = view.path.read_bytes()
        except FileNotFoundError:
            raise MediaError("MEDIA_UNAVAILABLE", "media file is missing", status=410) from None
        return view, payload

    def list_session(self, session_id: str) -> list[MediaView]:
        return [MediaView.from_row(row) for row in self.store.list_media(session_id)]

    # -- deletion -------------------------------------------------------- #
    def purge(self, handle: str, *, session_id: str, reason: str) -> dict[str, Any]:
        row = self.store.get_media(handle)
        if row is None:
            return {
                "handle": handle,
                "localStatus": "UNKNOWN",
                "providerStatus": PROVIDER_STATUS_UNVERIFIED,
            }
        if str(row["session_id"]) != session_id:
            raise MediaError("MEDIA_SESSION_DENIED", "media belongs to another session", status=403)
        if str(row["local_status"]) not in ("PRESENT", "PENDING_RETRY"):
            return {
                "handle": handle,
                "localStatus": str(row["local_status"]),
                "providerStatus": str(row["provider_status"]),
            }

        now = self._clock()
        attempts = int(row["attempts"]) + 1
        path = pathlib.Path(str(row["path"]))
        status, error = "DELETED", None
        try:
            self._unlink(str(path))
        except FileNotFoundError:
            status, error = "DELETED", None
        except OSError as exc:
            status, error = "PENDING_RETRY", f"{type(exc).__name__}: {exc}"
            self.store.mark_media(
                handle,
                local_status=status,
                attempts=attempts,
                last_error=error,
                provider_status=PROVIDER_STATUS_UNVERIFIED,
            )
            self.store.record_deletion(
                handle, session_id, now, status, PROVIDER_STATUS_UNVERIFIED, f"{reason}: {error}"
            )
            return {
                "handle": handle,
                "localStatus": status,
                "providerStatus": PROVIDER_STATUS_UNVERIFIED,
                "error": error,
            }

        self.store.mark_media(
            handle,
            deleted_at=iso(now),
            local_status=status,
            attempts=attempts,
            last_error=None,
            provider_status=PROVIDER_STATUS_MOCK,
        )
        self.store.record_deletion(handle, session_id, now, status, PROVIDER_STATUS_MOCK, reason)
        return {"handle": handle, "localStatus": status, "providerStatus": PROVIDER_STATUS_MOCK}

    def purge_session(self, session_id: str, *, reason: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for view in self.list_session(session_id):
            if view.local_status == "PRESENT":
                results.append(self.purge(view.handle, session_id=session_id, reason=reason))
        return results

    def sweep(self, now: datetime | None = None) -> dict[str, int]:
        """Expire TTL rows and retry persisted deletions.  Runs on every start."""

        moment = now or self._clock()
        expired = 0
        retried = 0
        failed = 0
        for row in self.store.list_media_all():
            view = MediaView.from_row(row)
            if view.local_status == "PRESENT" and moment >= view.expires_at:
                result = self.purge(view.handle, session_id=view.session_id, reason="TTL_EXPIRED")
                expired += 1
                if result.get("localStatus") == "PENDING_RETRY":
                    failed += 1
            elif view.local_status == "PENDING_RETRY":
                retried += 1
                result = self.purge(view.handle, session_id=view.session_id, reason="RETRY")
                if result.get("localStatus") == "PENDING_RETRY":
                    failed += 1
        return {"expired": expired, "retried": retried, "failed": failed}
