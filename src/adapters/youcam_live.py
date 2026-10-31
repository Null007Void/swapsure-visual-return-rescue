"""LIVE YouCam AI Clothes adapter — corrected per review findings 2-6.

Contract source: docs.perfectcorp.com AI Clothes integration guide. The documented response fields are
``data.task_status`` and ``data.results[].url``; those are what this module parses and what the fixtures
pin. Unit cost: the older guide table lists V2/V3 at 2 units and does not state V4; Perfect Corp's V4
announcement lists 2 units per image (owner-supplied, recorded in release/PHASE-D-RECONCILIATION.md).
The default is 2 with that source recorded, but the operator must confirm it: a published price is not
this account's billing truth.

Enforced here, each with a regression test:

* the API origin is pinned separately from media hosts, and the bearer token is sent only to an
  owner-approved origin;
* redirects are refused, so no credential can cross an origin hop;
* provider-supplied upload instructions are validated (method PUT, headers limited, host allow-listed)
  before any bytes leave;
* responses are size-bounded;
* media must be the consenting user's real JPEG/PNG with plausible dimensions, and each reference must
  carry recorded rights clearance — never a synthetic marker;
* every unit reservation is durable and atomic, taken BEFORE task creation, keyed by operation, session
  and authorization, and retained as UNCERTAIN when acknowledgement is lost.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import http.client
import socket
import sqlite3
import ssl

from PIL import Image, UnidentifiedImageError
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from adapters.youcam_stub import MAX_MEDIA_BYTES, AdapterError, FailureCode, MediaRef, PreviewTask
from domain.preview import PreviewPlan, PreviewResult

OFFICIAL_API_ORIGIN = "https://yce-api-01.makeupar.com"
FILE_PATH = "/s2s/v2.0/file"
TASK_PATH = "/s2s/v2.0/task/cloth-v4"
# The documented enum for garment_category on POST /s2s/v2.0/task/cloth-v4.
GARMENT_CATEGORIES = frozenset({"full_body", "upper_body", "lower_body", "outer",
                                "shoes", "auto"})
MAX_RESPONSE_BYTES = 256 * 1024
UPLOAD_ALLOWED_HEADERS = frozenset({"content-type", "content-length"})
# How long a delivered result stays displayable. Deliberately independent of the task deadline,
# which may have elapsed: a late-but-successful task can still be shown inside this window.
RESULT_VALIDITY = timedelta(minutes=10)
MIN_LONG_SIDE = 512
MAX_LONG_SIDE = 4096
MAX_IMAGE_BYTES = 10 * 1024 * 1024        # documented provider ceiling for an image body
# Ceiling for the *decoded* pixel buffer, so a small file cannot ask for a huge allocation.
# 4096 x 4096 RGBA plus one filter byte per row is ~64 MiB; 80 MiB leaves headroom without
# permitting an unbounded inflate.
MAX_DECODED_BYTES = 80 * 1024 * 1024
# A provider result is bounded in pixels as well as bytes: 40 M px is far above any try-on
# output and far below what a decompression bomb would need.
MAX_RESULT_PIXELS = 40_000_000
SUPPORTED_IMAGE_TYPE = "image/png"
_PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# Documented provider error codes -> normalised failures.
_PROVIDER_ERRORS: Mapping[str, FailureCode] = {
    "InvalidAccessToken": FailureCode.AUTH_FAILURE,
    "Unauthorized": FailureCode.AUTH_FAILURE,
    "error_below_min_image_size": FailureCode.INVALID_INPUT,
    "error_pose": FailureCode.INVALID_INPUT,
    "error_invalid_ref": FailureCode.INVALID_INPUT,
    "error_apply_region_mismatch": FailureCode.INVALID_INPUT,
    "error_invalid_src": FailureCode.INVALID_INPUT,
    "error_multi_person": FailureCode.INVALID_INPUT,
    "invalid_parameter": FailureCode.INVALID_INPUT,
    "error_download_image": FailureCode.INVALID_INPUT,
    "exceed_max_filesize": FailureCode.INVALID_INPUT,
    "error_nsfw_content_detected": FailureCode.INVALID_RESULT,
    "error_editing_failed": FailureCode.INVALID_RESULT,
    "unknown_internal_error": FailureCode.PROVIDER_FAILURE,
}
# Only refusals that arrive as a provider ANSWER to a transmitted request, and that prove no task
# was created. A redirect is deliberately excluded: it is a response after transmission.
_CERTAIN_REFUSALS = frozenset({FailureCode.AUTH_FAILURE, FailureCode.RATE_LIMIT,
                               FailureCode.INVALID_INPUT})
_TERMINAL_OK = frozenset({"success", "succeeded"})
_TERMINAL_FAIL = frozenset({"failed", "error", "cancelled", "rejected"})
_PENDING = frozenset({"pending", "processing", "queued", "running", "created", "in_progress"})


def _decode_png(data: bytes) -> tuple[int, int]:
    """Decode a complete PNG inside hard bounds, validating geometry before any inflation.

    Order matters: IHDR length and format are checked first, then the declared dimensions are
    bounded, then the expected decoded size is computed and capped, and only then is the zlib stream
    inflated incrementally with an explicit output ceiling. Overflow, incomplete streams and trailing
    compressed data are all refused without ever allocating past the cap.
    """
    import zlib

    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AdapterError(FailureCode.INVALID_INPUT, "payload is not a PNG")
    pos = 8
    width = height = None
    color_type = None
    idat = bytearray()
    saw_end = False
    first = True
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        if pos + 12 + length > len(data):
            raise AdapterError(FailureCode.INVALID_INPUT, "PNG is truncated")
        body = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        if zlib.crc32(tag + body) % (2 ** 32) != crc:
            raise AdapterError(FailureCode.INVALID_INPUT, "PNG chunk CRC mismatch")
        if first and tag != b"IHDR":
            raise AdapterError(FailureCode.INVALID_INPUT, "PNG does not start with IHDR")
        first = False
        if tag == b"IHDR":
            if length != 13:
                raise AdapterError(FailureCode.INVALID_INPUT,
                                   f"PNG IHDR is {length} bytes, expected 13")
            (width, height, bit_depth, color_type,
             compression, filter_method, interlace) = struct.unpack(">IIBBBBB", body)
            if compression or filter_method or interlace:
                raise AdapterError(FailureCode.INVALID_INPUT, "unsupported PNG encoding")
            if bit_depth != 8 or color_type not in _PNG_CHANNELS:
                raise AdapterError(FailureCode.INVALID_INPUT, "unsupported PNG pixel format")
            if not width or not height:
                raise AdapterError(FailureCode.INVALID_INPUT, "PNG declares a zero dimension")
            # Geometry is bounded before a single byte is inflated.
            if max(width, height) > MAX_LONG_SIDE:
                raise AdapterError(
                    FailureCode.INVALID_INPUT,
                    f"image is {width}x{height}; the maximum side is {MAX_LONG_SIDE} px")
            expected = height * (1 + width * _PNG_CHANNELS[color_type])
            if expected > MAX_DECODED_BYTES:
                raise AdapterError(
                    FailureCode.INVALID_INPUT,
                    f"decoded image would be {expected} bytes, above the "
                    f"{MAX_DECODED_BYTES}-byte bound")
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            saw_end = True
            pos += 12
            break
        pos += 12 + length
    if width is None or height is None:
        raise AdapterError(FailureCode.INVALID_INPUT, "PNG is missing its IHDR header")
    if not idat:
        raise AdapterError(FailureCode.INVALID_INPUT, "PNG carries no image data")
    if not saw_end:
        raise AdapterError(FailureCode.INVALID_INPUT, "PNG is truncated: no IEND chunk")
    if pos != len(data):
        raise AdapterError(FailureCode.INVALID_INPUT, "PNG has trailing bytes after IEND")

    expected = height * (1 + width * _PNG_CHANNELS[color_type])
    decoder = zlib.decompressobj()
    out = bytearray()
    pending = bytes(idat)
    while True:
        allowance = expected - len(out) + 1          # +1 so one extra byte is detectable
        piece = decoder.decompress(pending, allowance)
        out += piece
        if len(out) > expected:
            raise AdapterError(FailureCode.INVALID_INPUT,
                               "PNG image data inflates past its declared size")
        pending = decoder.unconsumed_tail
        if not pending:
            break
    tail = decoder.flush()
    out += tail
    if len(out) > expected:
        raise AdapterError(FailureCode.INVALID_INPUT, "PNG image data inflates past its declared size")
    if not decoder.eof:
        raise AdapterError(FailureCode.INVALID_INPUT, "PNG image data is an incomplete stream")
    if decoder.unused_data:
        raise AdapterError(FailureCode.INVALID_INPUT, "PNG image data has trailing compressed bytes")
    if len(out) != expected:
        raise AdapterError(FailureCode.INVALID_INPUT, "PNG image data is the wrong length")
    stride = 1 + width * _PNG_CHANNELS[color_type]
    for row in range(height):
        if out[row * stride] > 4:
            raise AdapterError(FailureCode.INVALID_INPUT, "PNG row filter is invalid")
    return width, height


def decode_result_image(payload: bytes) -> tuple[bytes, str, int, int]:
    """Decode a provider result with Pillow and return (bytes, mime, width, height).

    Format is decided by the decoder, never by the filename, the declared Content-Type or the magic
    bytes alone: a JPEG carrying a PNG header still fails here. The full image is decoded, so
    truncated entropy data is a refusal rather than a broken picture. PNG intake for the shopper's
    own photo and the garment reference is unchanged and still PNG-only.
    """
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise AdapterError(FailureCode.INVALID_RESULT, "result image is empty")
    if len(payload) > MAX_IMAGE_BYTES:
        raise AdapterError(FailureCode.INVALID_RESULT, "result image exceeded the size bound")
    data = bytes(payload)
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_RESULT_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = (probe.format or "").upper()
            width, height = probe.size
            if fmt not in ("PNG", "JPEG"):
                raise AdapterError(FailureCode.INVALID_RESULT,
                                   f"result image format {fmt or 'unknown'} is not supported")
            if width <= 0 or height <= 0:
                raise AdapterError(FailureCode.INVALID_RESULT, "result image has no usable size")
            if width * height > MAX_RESULT_PIXELS:
                raise AdapterError(FailureCode.INVALID_RESULT,
                                   f"result image is {width}x{height}, past the pixel bound")
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()                       # real decode: truncated data raises here
            fmt = (image.format or "").upper()
            width, height = image.size
    except AdapterError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError) as exc:
        raise AdapterError(FailureCode.INVALID_RESULT,
                           f"result image could not be decoded ({type(exc).__name__})") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    mime = "image/png" if fmt == "PNG" else "image/jpeg"
    return data, mime, width, height


def sniff_image(payload: bytes, content_type: str) -> tuple[int, int]:
    """Validate a complete, decodable PNG inside the documented limits.

    The qualification path accepts **PNG only**. JPEG is refused rather than accepted on the strength
    of marker framing: this build ships no bounded JPEG decoder, and marker framing does not establish
    that the entropy-coded data decodes. Both documented minimum dimensions are enforced — long side
    at least 512 px **and** short side at least 384 px — so 384x384 is refused.
    """
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise AdapterError(FailureCode.INVALID_INPUT, "media payload is empty")
    if len(payload) > MAX_IMAGE_BYTES:
        raise AdapterError(FailureCode.INVALID_INPUT, "media payload exceeds the 10 MB provider limit")
    data = bytes(payload)
    if data[:2] == b"\xff\xd8":
        # Intake (the shopper's own photo, the garment reference) stays PNG-only on purpose; only
        # provider *results* accept JPEG, and they are decoded before delivery.
        raise AdapterError(
            FailureCode.INVALID_INPUT,
            "JPEG is not accepted for uploads on this build; supply a PNG")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AdapterError(FailureCode.INVALID_INPUT, "payload is not a PNG")
    if content_type not in ("image/png",):
        raise AdapterError(FailureCode.INVALID_INPUT, "declared content type does not match PNG bytes")
    width, height = _decode_png(data)
    if max(width, height) < MIN_LONG_SIDE or min(width, height) < 384:
        raise AdapterError(
            FailureCode.INVALID_INPUT,
            f"image is {width}x{height}; the documented minimum is 512 px on the long side "
            f"and 384 px on the short side")
    return width, height


def _parse_stamp(value: object) -> datetime:
    """Read a stored stamp as UTC, so a naive record never skews a deadline."""
    if not isinstance(value, str) or not value:
        return datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    """Return a UTC-aware datetime; a naive value is read as UTC, never compared blindly."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ConsentGrant:
    session_id: str
    generation: int
    version: str
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def assert_live(self, now: datetime) -> None:
        if not self.version or self.generation < 0:
            raise AdapterError(FailureCode.CONSENT_REQUIRED, "consent grant is incomplete")
        if self.revoked_at is not None:
            raise AdapterError(FailureCode.CONSENT_REQUIRED, "consent was revoked")
        # A naive clock must never crash a gate: compare in one awareness.
        if _as_utc(now) >= _as_utc(self.expires_at):
            raise AdapterError(FailureCode.CONSENT_REQUIRED, "consent expired")


@dataclass(frozen=True, slots=True)
class LiveSettings:
    api_key: str
    authorization_ref: str
    allowed_media_hosts: frozenset[str]
    unit_cost: int = 2
    unit_cap: int = 0
    session_unit_cap: int = 6
    api_origin: str = OFFICIAL_API_ORIGIN
    allowed_api_origins: frozenset[str] = frozenset({OFFICIAL_API_ORIGIN})
    garment_category: str = "full_body"
    request_timeout: float = 15.0
    poll_interval_seconds: float = 1.0
    # How long the page keeps asking before it stops and offers reconciliation. The provider
    # documents no turnaround, so this is a bounded choice, not a claim about their latency.
    wait_seconds: float = 300.0
    # The one final status query made after the wait ends is bounded on its own clock.
    final_status_timeout: float = 10.0
    ledger_path: str | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise AdapterError(FailureCode.AUTH_FAILURE, "live mode requires an API key")
        if not self.authorization_ref:
            raise AdapterError(FailureCode.AUTH_FAILURE, "live mode requires an owner authorization reference")
        if not self.allowed_media_hosts:
            raise AdapterError(FailureCode.DISALLOWED_DESTINATION, "an owner-approved media host list is required")
        if self.api_origin.rstrip("/").lower() not in {o.rstrip("/").lower() for o in self.allowed_api_origins}:
            raise AdapterError(FailureCode.DISALLOWED_DESTINATION,
                               f"API origin {self.api_origin!r} is not owner-approved")
        if type(self.unit_cost) is not int or self.unit_cost <= 0:
            raise AdapterError(FailureCode.INVALID_INPUT, "unit_cost must be a positive integer")
        if type(self.unit_cap) is not int or self.unit_cap < 0:
            raise AdapterError(FailureCode.INVALID_INPUT, "unit_cap must be a non-negative integer")
        if type(self.session_unit_cap) is not int or self.session_unit_cap < 0:
            raise AdapterError(FailureCode.INVALID_INPUT, "session_unit_cap must be non-negative")
        if self.garment_category not in GARMENT_CATEGORIES:
            raise AdapterError(FailureCode.INVALID_INPUT, "unsupported garment category")
        if not (0 < self.request_timeout <= 120):
            raise AdapterError(FailureCode.INVALID_INPUT, "request_timeout out of range")

    def __repr__(self) -> str:
        return (f"LiveSettings(api_origin={self.api_origin!r}, unit_cost={self.unit_cost}, "
                f"unit_cap={self.unit_cap}, session_unit_cap={self.session_unit_cap}, api_key=<redacted>)")

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "LiveSettings":
        env = os.environ if env is None else env

        def need(name: str) -> str:
            value = (env.get(name) or "").strip()
            if not value:
                raise AdapterError(FailureCode.AUTH_FAILURE, f"live mode requires {name}")
            return value

        def num(name: str, default: int | None = None) -> int:
            raw = (env.get(name) or "").strip()
            if not raw and default is not None:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise AdapterError(FailureCode.INVALID_INPUT, f"{name} must be an integer") from exc

        hosts = frozenset(h.strip().lower() for h in need("SWAPSURE_LIVE_ALLOWED_HOSTS").split(",") if h.strip())
        origins = frozenset((env.get("SWAPSURE_LIVE_ALLOWED_API_ORIGINS") or OFFICIAL_API_ORIGIN)
                            .replace(",", " ").split())
        return cls(api_key=need("YOUCAM_API_KEY"),
                   authorization_ref=need("SWAPSURE_LIVE_AUTHORIZATION_REF"),
                   allowed_media_hosts=hosts,
                   unit_cost=num("SWAPSURE_LIVE_UNIT_COST", 2),
                   unit_cap=num("SWAPSURE_LIVE_UNIT_CAP"),
                   session_unit_cap=num("SWAPSURE_LIVE_SESSION_UNIT_CAP", 6),
                   api_origin=(env.get("SWAPSURE_LIVE_API_ORIGIN") or OFFICIAL_API_ORIGIN).rstrip("/"),
                   allowed_api_origins=origins,
                   garment_category=(env.get("SWAPSURE_LIVE_GARMENT_CATEGORY") or "full_body"),
                   ledger_path=env.get("SWAPSURE_LIVE_LEDGER") or None)


def live_configured(env: Mapping[str, str] | None = None) -> bool:
    try:
        LiveSettings.from_environment(env)
        return True
    except AdapterError:
        return False


class ReservationLedger:
    """Durable, atomic unit reservations taken before task creation.

    A crash, a duplicate request or a concurrent worker cannot spend past the cap; a lost
    acknowledgement stays UNCERTAIN and keeps counting until an operator reconciles it, because the
    provider may have created (and billed) the task.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = str(path or os.path.join(os.path.expanduser("~"), ".swapsure", "units.sqlite3"))
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        con = self._connect()
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS reservations (
                operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, authorization_ref TEXT NOT NULL,
                units INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.execute("PRAGMA busy_timeout=30000")
        return con

    @staticmethod
    def _sum(con, session_id: str | None) -> int:
        if session_id is None:
            row = con.execute("SELECT COALESCE(SUM(units),0) FROM reservations "
                              "WHERE state IN ('RESERVED','CONFIRMED','UNCERTAIN')").fetchone()
        else:
            row = con.execute("SELECT COALESCE(SUM(units),0) FROM reservations WHERE session_id=? "
                              "AND state IN ('RESERVED','CONFIRMED','UNCERTAIN')", (session_id,)).fetchone()
        return int(row[0])

    def reserve(self, *, operation_id: str, session_id: str, authorization_ref: str, units: int,
                deployment_cap: int, session_cap: int, now: datetime) -> None:
        if units <= 0:
            raise AdapterError(FailureCode.INVALID_INPUT, "reservation must be positive")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT state FROM reservations WHERE operation_id=?", (operation_id,)).fetchone():
                con.execute("COMMIT")
                raise AdapterError(FailureCode.BUDGET_EXHAUSTED,
                                   "this operation already holds a reservation; duplicate creation refused")
            if self._sum(con, None) + units > deployment_cap:
                con.execute("COMMIT")
                raise AdapterError(FailureCode.BUDGET_EXHAUSTED, "deployment unit cap would be exceeded")
            if self._sum(con, session_id) + units > session_cap:
                con.execute("COMMIT")
                raise AdapterError(FailureCode.BUDGET_EXHAUSTED, "session unit cap would be exceeded")
            stamp = _iso(now)
            con.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?)",
                        (operation_id, session_id, authorization_ref, units, "RESERVED", stamp, stamp))
            con.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise AdapterError(FailureCode.PROVIDER_FAILURE, f"unit ledger unavailable: {type(exc).__name__}") from exc
        finally:
            con.close()

    def confirm_with_task(self, *, operation_id: str, session_id: str, candidate_id: str,
                          provider_task_id: str, authorization_ref: str, units: int,
                          provider_version: str, plan_deadline: datetime, now: datetime) -> None:
        """Confirm the spend and store the provider's task id in one transaction.

        Either both are on disk or neither is: a reservation confirmed without its task id is a
        paid task nobody can query, which is exactly the failure this code exists to prevent.
        """
        stamp = _iso(now)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT state FROM reservations WHERE operation_id=?",
                              (operation_id,)).fetchone()
            if row is None:
                con.execute("ROLLBACK")
                raise AdapterError(FailureCode.RECOVERY_REQUIRED,
                                   "no reservation covers this operation; refusing to store a task id")
            con.execute("UPDATE reservations SET state='CONFIRMED', updated_at=? WHERE operation_id=?",
                        (stamp, operation_id))
            con.execute("INSERT OR REPLACE INTO provider_tasks (operation_id, session_id, candidate_id, "
                        "provider_task_id, authorization_ref, units, created_at, updated_at, "
                        "provider_version, plan_deadline, final_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                        (operation_id, session_id, candidate_id, provider_task_id, authorization_ref,
                         units, stamp, stamp, provider_version, _iso(plan_deadline)))
            con.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self._retain_uncertain(operation_id, now)
            raise AdapterError(
                FailureCode.RECOVERY_REQUIRED,
                "the provider task exists but could not be recorded; the reservation stays uncertain",
            ) from exc
        finally:
            con.close()

    def _retain_uncertain(self, operation_id: str, now: datetime) -> None:
        """After a failed write the money may well be spent: keep the hold, never release it."""
        try:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("UPDATE reservations SET state='UNCERTAIN', updated_at=? "
                            "WHERE operation_id=?", (_iso(now), operation_id))
                con.execute("COMMIT")
            finally:
                con.close()
        except sqlite3.Error:
            pass

    def mark(self, operation_id: str, state: str, now: datetime) -> None:
        if state not in ("CONFIRMED", "UNCERTAIN", "VOID"):
            raise AdapterError(FailureCode.INVALID_INPUT, "unknown reservation state")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE reservations SET state=?, updated_at=? WHERE operation_id=?",
                        (state, _iso(now), operation_id))
            con.execute("COMMIT")
        finally:
            con.close()

    def lookup_reservation(self, operation_id: str) -> dict[str, Any] | None:
        con = self._connect()
        try:
            row = con.execute("SELECT operation_id, session_id, authorization_ref, units, state, "
                              "created_at FROM reservations WHERE operation_id=?",
                              (operation_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return dict(zip(("operation_id", "session_id", "authorization_ref", "units", "state",
                         "created_at"), row))

    def outstanding(self) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            rows = con.execute("SELECT operation_id, session_id, units, state FROM reservations "
                               "WHERE state IN ('RESERVED','UNCERTAIN') ORDER BY created_at").fetchall()
        finally:
            con.close()
        return [{"operation_id": r[0], "session_id": r[1], "units": r[2], "state": r[3]} for r in rows]

    def committed_units(self, *, session_id: str | None = None) -> int:
        con = self._connect()
        try:
            return self._sum(con, session_id)
        finally:
            con.close()


class TaskRegistry:
    """Durable map from our operation to the provider's task id.

    Without this record a billed task can never be queried again, and the only remaining option
    would be a second charged generation. The record is written immediately after the provider
    acknowledges the task, inside the same database as the unit reservation, so the id, the
    session, the authority and the reserved units travel together.
    """

    def __init__(self, ledger: "ReservationLedger") -> None:
        self._ledger = ledger
        con = ledger._connect()
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS provider_tasks (
                operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                provider_task_id TEXT NOT NULL, authorization_ref TEXT NOT NULL, units INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                provider_version TEXT NOT NULL DEFAULT '', plan_deadline TEXT, final_at TEXT)""")
            # Older ledgers predate the immutable binding; add the columns without rewriting rows.
            have = {row[1] for row in con.execute("PRAGMA table_info(provider_tasks)")}
            for column, ddl in (("provider_version", "TEXT NOT NULL DEFAULT ''"),
                                ("plan_deadline", "TEXT"), ("final_at", "TEXT")):
                if column not in have:
                    con.execute(f"ALTER TABLE provider_tasks ADD COLUMN {column} {ddl}")
            con.execute("""CREATE TABLE IF NOT EXISTS poll_log (
                operation_id TEXT NOT NULL, at TEXT NOT NULL, http_status INTEGER,
                provider_status TEXT, outcome TEXT NOT NULL)""")
        finally:
            con.close()

    def record(self, *, operation_id: str, session_id: str, candidate_id: str,
               provider_task_id: str, authorization_ref: str, units: int, now: datetime) -> None:
        con = self._ledger._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            stamp = _iso(now)
            con.execute("INSERT OR REPLACE INTO provider_tasks VALUES (?,?,?,?,?,?,?,?)",
                        (operation_id, session_id, candidate_id, provider_task_id,
                         authorization_ref, units, stamp, stamp))
            con.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise AdapterError(
                FailureCode.RECOVERY_REQUIRED,
                "the provider task exists but its id could not be stored; do not retry",
            ) from exc
        finally:
            con.close()

    def lookup(self, operation_id: str) -> dict[str, Any] | None:
        con = self._ledger._connect()
        try:
            row = con.execute("SELECT operation_id, session_id, candidate_id, provider_task_id, "
                              "authorization_ref, units, created_at, provider_version, "
                              "plan_deadline, final_at FROM provider_tasks WHERE operation_id=?",
                              (operation_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        keys = ("operation_id", "session_id", "candidate_id", "provider_task_id",
                "authorization_ref", "units", "created_at", "provider_version",
                "plan_deadline", "final_at")
        return dict(zip(keys, row))

    def note_final(self, operation_id: str, now: datetime, outcome: str) -> None:
        """Record that the single post-deadline status query has been spent."""
        con = self._ledger._connect()
        try:
            con.execute("UPDATE provider_tasks SET final_at=?, updated_at=? WHERE operation_id=?",
                        (_iso(now), _iso(now), operation_id))
            con.execute("INSERT INTO poll_log VALUES (?,?,?,?,?)",
                        (operation_id, _iso(now), None, None, f"FINAL_STATUS:{outcome}"[:120]))
        except sqlite3.Error:
            pass
        finally:
            con.close()

    def final_status_at(self, operation_id: str) -> str | None:
        con = self._ledger._connect()
        try:
            row = con.execute("SELECT final_at FROM provider_tasks WHERE operation_id=?",
                              (operation_id,)).fetchone()
        finally:
            con.close()
        return None if row is None else row[0]

    def note_poll(self, *, operation_id: str, now: datetime, http_status: int | None,
                  provider_status: str | None, outcome: str) -> None:
        """Sanitized diagnostic trail: no keys, no bytes, no URLs."""
        con = self._ledger._connect()
        try:
            con.execute("INSERT INTO poll_log VALUES (?,?,?,?,?)",
                        (operation_id, _iso(now), http_status, provider_status, outcome[:120]))
            con.execute("UPDATE provider_tasks SET updated_at=? WHERE operation_id=?",
                        (_iso(now), operation_id))
        except sqlite3.Error:
            pass                      # a diagnostic write must never break a poll
        finally:
            con.close()

    def polls(self, operation_id: str) -> list[dict[str, Any]]:
        con = self._ledger._connect()
        try:
            rows = con.execute("SELECT at, http_status, provider_status, outcome FROM poll_log "
                               "WHERE operation_id=? ORDER BY at", (operation_id,)).fetchall()
        finally:
            con.close()
        return [{"at": r[0], "http_status": r[1], "provider_status": r[2], "outcome": r[3]}
                for r in rows]


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str] | None = None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(FailureCode.INVALID_RESULT, "provider response is not JSON") from exc


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Validates resolved addresses at connect time, then connects to the validated address.

    This closes the DNS-rebinding window a pre-check alone leaves open: the address that is
    validated is the address that is dialled, and TLS still verifies the hostname.
    """

    def connect(self) -> None:  # pragma: no cover - sockets
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        addresses = []
        for family, socktype, proto, _canon, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if not _public_ip(ip):
                raise AdapterError(FailureCode.DISALLOWED_DESTINATION,
                                   "destination resolves to a non-public address")
            addresses.append((family, socktype, proto, sockaddr))
        if not addresses:
            raise AdapterError(FailureCode.DISALLOWED_DESTINATION, "destination did not resolve")
        family, socktype, proto, sockaddr = addresses[0]
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(self.timeout)
            sock.connect(sockaddr)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _PinnedHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):  # pragma: no cover - sockets
        return self.do_open(_PinnedHTTPSConnection, req, context=getattr(self, "_context", None))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AdapterError(FailureCode.DISALLOWED_DESTINATION, f"provider redirect refused ({code})")


class Transport:
    """One HTTP request. Refuses redirects and oversized bodies; the default is stdlib."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect, _PinnedHandler())

    def request(self, method: str, url: str, *, headers: Mapping[str, str],
                body: bytes | None = None, timeout: float,
                max_bytes: int = MAX_RESPONSE_BYTES) -> Response:  # pragma: no cover - I/O
        req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(req, timeout=timeout) as response:
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise AdapterError(FailureCode.INVALID_RESULT, "provider response exceeded the size bound")
                return Response(response.status, data, dict(response.headers))
        except urllib.error.HTTPError as exc:
            data = exc.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise AdapterError(FailureCode.INVALID_RESULT, "provider error body exceeded the size bound")
            return Response(exc.code, data or b"", dict(exc.headers or {}))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AdapterError(FailureCode.TIMEOUT, f"provider request failed: {type(exc).__name__}") from exc


def _public_ip(ip: ipaddress._BaseAddress) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


def _safe_url(url: str) -> bool:
    """Reject anything whose parsing could smuggle a different destination.

    Malformed URLs and invalid ports are refusals, not crashes: urlsplit raises ValueError on a
    non-numeric port, so parsing is wrapped and reported as a typed destination failure.
    """
    if not isinstance(url, str) or not url or any(ord(c) < 0x21 or ord(c) > 0x7E for c in url):
        return False
    if "\\" in url or "#" in url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if "@" in parts.netloc:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and port in (None, 443)


def _permitted_origin(url: str, origins: frozenset[str]) -> bool:
    if not _safe_url(url):
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        return False
    return f"https://{(parts.hostname or '').lower()}" in {o.rstrip("/").lower() for o in origins}


def _permitted_media(url: str, hosts: frozenset[str]) -> bool:
    if not _safe_url(url):
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        return False
    host = (parts.hostname or "").lower()
    if not host or host not in hosts:
        return False
    try:
        return _public_ip(ipaddress.ip_address(host))
    except ValueError:
        return True


class YouCamLivePreviewAdapter:
    mode = "LIVE"

    def __init__(self, *, settings: LiveSettings, clock: Callable[[], datetime],
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                 ledger: ReservationLedger | None = None) -> None:
        self._settings = settings
        self._clock = clock
        self._transport = transport or Transport()
        self._sleep = sleep
        self._ledger = ledger or ReservationLedger(settings.ledger_path)
        self._media: dict[str, MediaRef] = {}
        self._payloads: dict[str, bytes] = {}
        self._references: dict[str, bytes] = {}
        self._reference_types: dict[str, str] = {}
        self._rights: dict[str, str] = {}
        self._consent: dict[str, ConsentGrant] = {}
        self._tasks: dict[str, PreviewTask] = {}
        self._provider_ids: dict[str, str] = {}
        self._registry = TaskRegistry(self._ledger)
        self._result_bytes: dict[str, tuple[bytes, str]] = {}
        self.tasks_created = 0

    @property
    def max_tasks_per_session(self) -> int:
        return 3

    @property
    def units_consumed(self) -> int:
        return self._ledger.committed_units()

    def outstanding_reservations(self) -> list[dict[str, Any]]:
        return self._ledger.outstanding()

    def _timeout(self, deadline: datetime) -> float:
        remaining = (deadline - self._clock()).total_seconds()
        if remaining <= 0:
            raise AdapterError(FailureCode.TIMEOUT, "overall preview deadline has passed")
        return min(self._settings.request_timeout, remaining)  # bounded, never padded upwards

    def _headers(self, url: str) -> dict[str, str]:
        if not _permitted_origin(url, self._settings.allowed_api_origins):
            raise AdapterError(FailureCode.DISALLOWED_DESTINATION,
                               "refusing to send credentials to an unapproved origin")
        return {"Authorization": f"Bearer {self._settings.api_key}", "content-type": "application/json"}

    def _check(self, response: Response) -> Any:
        if response.status in (401, 403):
            raise AdapterError(FailureCode.AUTH_FAILURE, f"provider refused the credential ({response.status})")
        if response.status in (301, 302, 303, 307, 308):
            raise AdapterError(FailureCode.DISALLOWED_DESTINATION, "provider redirect refused")
        if response.status == 429:
            raise AdapterError(FailureCode.RATE_LIMIT, "provider rate limit")
        if response.status >= 500:
            raise AdapterError(FailureCode.PROVIDER_FAILURE, f"provider error {response.status}")
        payload = response.json()
        error = payload.get("error_code") or payload.get("error") if isinstance(payload, dict) else None
        if error:
            raise AdapterError(_PROVIDER_ERRORS.get(str(error), FailureCode.PROVIDER_FAILURE),
                               f"provider error {error}")
        if response.status >= 400:
            raise AdapterError(FailureCode.PROVIDER_FAILURE, f"provider returned {response.status}")
        return payload

    def prepare_media(self, *, session_id: str, candidate_id: str, payload: bytes,
                      content_type: str, consent: ConsentGrant,
                      reference_rights: str | None = None,
                      reference_payload: bytes | None = None,
                      reference_content_type: str | None = "image/png",
                      garment_category: str | None = None) -> MediaRef:
        if not isinstance(consent, ConsentGrant) or consent.session_id != session_id:
            raise AdapterError(FailureCode.CONSENT_REQUIRED, "media preparation requires this session's consent")
        now = self._clock()
        consent.assert_live(now)
        if not reference_rights:
            raise AdapterError(FailureCode.INVALID_INPUT,
                               "the selected garment needs recorded rights clearance before any upload")
        if not isinstance(reference_payload, (bytes, bytearray)) or len(reference_payload) < 64:
            raise AdapterError(FailureCode.INVALID_INPUT,
                               "a real garment reference image is required; no substitute is sent")
        sniff_image(payload, content_type)
        ref_width, ref_height = sniff_image(reference_payload, self._reference_content_type(reference_payload, reference_content_type))
        # Each candidate carries the category its own reference was verified against; a task may
        # never fall back to a process-wide value, because one run may mix garment families.
        if garment_category is not None and garment_category not in GARMENT_CATEGORIES:
            raise AdapterError(
                FailureCode.INVALID_INPUT,
                f"unknown garment category {garment_category!r}; nothing was uploaded")
        handle = "media_" + hashlib.sha256(
            f"{session_id}\x1f{candidate_id}\x1f{now.isoformat()}".encode()).hexdigest()[:24]
        if not hasattr(self, "_categories"):
            self._categories = {}
        if garment_category is not None:
            self._categories[handle] = garment_category
        ref = MediaRef(handle=handle, session_id=session_id, candidate_id=candidate_id, created_at=now,
                       expires_at=consent.expires_at, content_type=content_type, byte_size=len(payload))
        self._media[handle] = ref
        self._payloads[handle] = bytes(payload)
        self._references[handle] = bytes(reference_payload)
        self._reference_types[handle] = reference_content_type or "image/png"
        self._rights[handle] = reference_rights
        self._consent[handle] = consent
        self._reference_size = (ref_width, ref_height)
        return ref

    @staticmethod
    def _reference_content_type(payload: bytes, declared: str | None) -> str:
        """Accept only the documented formats, and only when the declared type matches the bytes."""
        if payload[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if payload[:2] == b"\xff\xd8":
            return "image/jpeg" if declared in (None, "image/png") else declared
        raise AdapterError(FailureCode.INVALID_INPUT, "reference payload is not a JPEG or PNG")

    def _register_file(self, content_type: str, file_name: str, size: int,
                       deadline: datetime) -> tuple[str, dict[str, Any]]:
        body = json.dumps({"files": [{"content_type": content_type, "file_name": file_name,
                                      "file_size": size}]}).encode()
        url = self._settings.api_origin + FILE_PATH
        response = self._transport.request("POST", url, headers=self._headers(url), body=body,
                                           timeout=self._timeout(deadline))
        payload = self._check(response)
        try:
            entry = payload["data"]["files"][0]
            instruction = entry["requests"][0]
            file_id, upload_url = entry["file_id"], instruction["url"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError(FailureCode.INVALID_RESULT, "file registration response is malformed") from exc
        method = str(instruction.get("method", "")).upper()
        if method != "PUT":
            raise AdapterError(FailureCode.INVALID_RESULT, f"upload instruction must be PUT, got {method!r}")
        headers = {str(k).lower(): str(v) for k, v in (instruction.get("headers") or {}).items()}
        if set(headers) - UPLOAD_ALLOWED_HEADERS:
            raise AdapterError(FailureCode.INVALID_RESULT, "upload instruction carries unexpected headers")
        if not _permitted_media(upload_url, self._settings.allowed_media_hosts):
            raise AdapterError(FailureCode.DISALLOWED_DESTINATION, "presigned upload host is not allow-listed")
        return str(file_id), {"method": method, "url": upload_url, "headers": headers}

    def _upload(self, instruction: Mapping[str, Any], payload: bytes, content_type: str,
                deadline: datetime) -> None:
        headers = dict(instruction["headers"])
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(payload))
        response = self._transport.request(instruction["method"], instruction["url"], headers=headers,
                                           body=payload, timeout=self._timeout(deadline))
        if response.status not in (200, 201, 204):
            raise AdapterError(FailureCode.PROVIDER_FAILURE, f"upload rejected ({response.status})")

    def create_preview(self, plan: PreviewPlan) -> tuple[PreviewTask, ...]:
        if plan.mode != "LIVE":
            raise AdapterError(FailureCode.INVALID_INPUT, f"live adapter cannot serve a {plan.mode} plan")
        if not plan.authorization_ref:
            raise AdapterError(FailureCode.AUTH_FAILURE, "live plan carries no authorization reference")
        if plan.authorization_ref != self._settings.authorization_ref:
            raise AdapterError(FailureCode.AUTH_FAILURE,
                               "plan authorization does not match the configured authority")
        if len(plan.candidate_ids) > self.max_tasks_per_session:
            raise AdapterError(FailureCode.INVALID_INPUT, "plan exceeds the per-session task cap")

        created: list[PreviewTask] = []
        for index, candidate_id in enumerate(plan.candidate_ids):
            handle = next((h for h, m in self._media.items()
                           if m.session_id == plan.session_id and m.candidate_id == candidate_id), None)
            if handle is None:
                raise AdapterError(FailureCode.INVALID_INPUT, "no prepared media for this candidate")
            def gate() -> None:
                """Consent is re-read from current state before every outbound step."""
                self._consent[handle].assert_live(self._clock())

            gate()                                                 # before spending anything
            media = self._media[handle]
            # Resolve the category BEFORE the reservation or any upload: an unmapped garment must
            # cost nothing and transmit nothing.
            category = getattr(self, "_categories", {}).get(handle)
            if category not in GARMENT_CATEGORIES:
                raise AdapterError(
                    FailureCode.INVALID_INPUT,
                    f"no verified provider category for {candidate_id}; nothing was reserved "
                    "or uploaded")
            # Identity depends only on what the task IS: session, candidate, authority and provider
            # version. Reordering candidates or replaying a plan cannot mint a second billed task.
            # The photo digest is part of the identity: the same photo and garment can never
            # mint a second billed task, while a different photo is a genuinely different task.
            photo_digest = hashlib.sha256(self._payloads[handle]).hexdigest()[:32]
            operation_id = hashlib.sha256(
                f"{plan.session_id}|{candidate_id}|{plan.authorization_ref}|"
                f"{plan.provider_version}|{photo_digest}".encode()).hexdigest()
            if operation_id in self._tasks:
                # The same request again in this process: a duplicate, not a restart.
                raise AdapterError(FailureCode.BUDGET_EXHAUSTED,
                                   "this operation already holds a task; duplicate creation refused")
            adopted = self._registry.lookup(operation_id)
            if adopted is not None:
                # This operation already has a provider task on record. Query it, never re-create,
                # and only if the stored binding still matches the plan we are holding.
                task = self._adopt_from_record(adopted, plan, candidate_id, handle)
                self._tasks[operation_id] = task
                created.append(task)
                continue
            self._ledger.reserve(operation_id=operation_id, session_id=plan.session_id,
                                 authorization_ref=plan.authorization_ref, units=self._settings.unit_cost,
                                 deployment_cap=self._settings.unit_cap,
                                 session_cap=self._settings.session_unit_cap, now=self._clock())
            try:
                # Pre-flight: nothing has been asked of task creation yet, so a failure here is
                # provably free and the hold is released.
                gate()
                src_file_id, src_instruction = self._register_file(
                    media.content_type, f"src-{operation_id[:12]}.jpg", media.byte_size, plan.deadline)
                gate()
                self._upload(src_instruction, self._payloads[handle], media.content_type, plan.deadline)
                reference = self._references[handle]
                gate()
                ref_type = self._reference_types[handle]
                ref_file_id, ref_instruction = self._register_file(
                    ref_type, f"ref-{candidate_id}.png", len(reference), plan.deadline)
                gate()
                self._upload(ref_instruction, reference, ref_type, plan.deadline)
            except AdapterError:
                self._ledger.mark(operation_id, "VOID", self._clock())
                raise
            body = json.dumps({"src_file_id": src_file_id, "ref_file_id": ref_file_id,
                               "garment_category": category}).encode()
            url = self._settings.api_origin + TASK_PATH
            # Resolve the authority, the header set and the timeout BEFORE transmission, so a
            # destination refusal here is provably pre-dispatch and free.
            try:
                gate()
                task_headers = self._headers(url)
                task_timeout = self._timeout(plan.deadline)
            except AdapterError:
                self._ledger.mark(operation_id, "VOID", self._clock())
                raise
            try:
                response = self._transport.request("POST", url, headers=task_headers, body=body,
                                                   timeout=task_timeout)
                payload = self._check(response)
                provider_task_id = None
                if isinstance(payload, dict):
                    provider_task_id = payload.get("task_id") or (payload.get("data") or {}).get("task_id")
                if not provider_task_id:
                    raise AdapterError(FailureCode.INVALID_RESULT, "task creation returned no task id")
            except AdapterError as exc:
                # A definite refusal means no task was created; a timeout, 5xx or unreadable body means
                # the provider may have created and billed it, so the hold is retained as UNCERTAIN.
                state = "VOID" if exc.code in _CERTAIN_REFUSALS else "UNCERTAIN"
                self._ledger.mark(operation_id, state, self._clock())
                raise
            self.tasks_created += 1
            try:
                # The spend and the id are committed together; there is no window in which one
                # exists without the other.
                self._ledger.confirm_with_task(
                    operation_id=operation_id, session_id=plan.session_id, candidate_id=candidate_id,
                    provider_task_id=str(provider_task_id),
                    authorization_ref=plan.authorization_ref, units=self._settings.unit_cost,
                    provider_version=plan.provider_version, plan_deadline=plan.deadline,
                    now=self._clock())
            except AdapterError:
                # The task exists and is paid for, but it could not be recorded. The reservation is
                # left UNCERTAIN and the operation is surfaced as recovery-required, never retried.
                raise
            task = PreviewTask(task_id=operation_id, session_id=plan.session_id, candidate_id=candidate_id,
                               mode="LIVE", status="PENDING", created_at=self._clock(),
                               deadline=plan.deadline, media_handle=handle)
            self._tasks[operation_id] = task
            self._provider_ids[operation_id] = str(provider_task_id)
            created.append(task)
        return tuple(created)

    def _binding_mismatches(self, record: dict[str, Any], plan: PreviewPlan,
                            candidate_id: str) -> list[str]:
        """Every field that defines this purchase, compared exactly as stored."""
        return [
            name for name, stored, current in (
                ("session", record["session_id"], plan.session_id),
                ("candidate", record["candidate_id"], candidate_id),
                ("authorization", record["authorization_ref"], plan.authorization_ref),
                ("provider version", record["provider_version"], plan.provider_version),
                ("units", int(record["units"]), int(self._settings.unit_cost)),
                ("plan deadline", record["plan_deadline"], _iso(plan.deadline)),
            ) if str(stored) != str(current)
        ]

    def _adopt_from_record(self, record: dict[str, Any], plan: PreviewPlan,
                           candidate_id: str, handle: str) -> PreviewTask:
        """Resume a recorded task only when everything that defines it still matches.

        A conflicting mapping is never replaced: a mismatch means the durable record and the plan
        describe different purchases, and guessing which one is real could address someone else's
        task or claim a deadline that was never agreed.
        """
        reservation = self._ledger.lookup_reservation(str(record["operation_id"]))
        if reservation is None or int(reservation["units"]) != int(record["units"]):
            raise AdapterError(FailureCode.RECOVERY_REQUIRED,
                               "the recorded task has no matching counted reservation")
        if reservation["state"] not in ("RESERVED", "CONFIRMED", "UNCERTAIN"):
            raise AdapterError(FailureCode.RECOVERY_REQUIRED,
                               "the reservation behind this task is not countable")
        mismatches = self._binding_mismatches(record, plan, candidate_id)
        if mismatches:
            raise AdapterError(FailureCode.RECOVERY_REQUIRED,
                               "the recorded task binding does not match this plan ("
                               + ", ".join(mismatches) + "); refusing to adopt or overwrite it")
        self._provider_ids[str(record["operation_id"])] = str(record["provider_task_id"])
        return PreviewTask(task_id=str(record["operation_id"]), session_id=plan.session_id,
                           candidate_id=candidate_id, mode="LIVE", status="PENDING",
                           created_at=_parse_stamp(record["created_at"]),
                           deadline=_parse_stamp(record["plan_deadline"]) if record["plan_deadline"]
                           else plan.deadline, media_handle=handle)

    def adopt_preview(self, task_id: str, *, session_id: str,
                      plan: PreviewPlan | None = None) -> bool:
        """Take over a task an earlier process created, using only the durable record."""
        record = self._registry.lookup(task_id)
        if record is None or str(record["session_id"]) != session_id:
            return False
        reservation = self._ledger.lookup_reservation(task_id)
        if reservation is None or int(reservation["units"]) != int(record["units"]):
            return False                     # not counted: nothing to poll on this basis
        if plan is not None:
            mismatches = self._binding_mismatches(record, plan, str(record["candidate_id"]))
            if mismatches:
                raise AdapterError(
                    FailureCode.RECOVERY_REQUIRED,
                    "the recorded task binding does not match this plan ("
                    + ", ".join(mismatches) + "); refusing to adopt or overwrite it")
        self._provider_ids[task_id] = str(record["provider_task_id"])
        stored_deadline = _parse_stamp(record["plan_deadline"]) if record["plan_deadline"] else None
        self._tasks.setdefault(task_id, PreviewTask(
            task_id=task_id, session_id=session_id, candidate_id=str(record["candidate_id"]),
            mode="LIVE", status="PENDING", created_at=_parse_stamp(record["created_at"]),
            deadline=stored_deadline or _parse_stamp(record["created_at"])))
        return True

    def poll_preview(self, task_id: str, *, session_id: str) -> PreviewTask:
        task = self._tasks.get(task_id)
        if task is None or task.session_id != session_id:
            raise AdapterError(FailureCode.INVALID_INPUT, "unknown task for this session")
        consent = self._consent.get(task.media_handle or "") or self._consent_for_session(session_id)
        if consent is None:
            # No consent on record is a refusal in its own right — including for a cached terminal
            # answer, which must not be handed back after the grant has gone.
            raise AdapterError(FailureCode.CONSENT_REQUIRED, "no consent on record for this session")
        consent.assert_live(self._clock())              # revocation stops polling, even when terminal
        if task.status in ("SUCCEEDED", "FAILED"):
            return task
        provider_task_id = self._provider_ids.get(task_id)
        if provider_task_id is None:
            record = self._registry.lookup(task_id)
            if record is None:
                raise AdapterError(FailureCode.RECOVERY_REQUIRED,
                                   "the provider task id for this operation is not on record")
            provider_task_id = str(record["provider_task_id"])
            self._provider_ids[task_id] = provider_task_id
        # The wait deadline bounds how long the page keeps asking; it is not a verdict on the
        # provider and it never discards the task handle.
        past_wait = self._clock() >= task.deadline
        if past_wait and self._registry.final_status_at(task_id):
            # The one post-deadline query has already been spent: stop asking, keep the handle.
            self._tasks[task_id] = task = replace(task, status="UNRESOLVED",
                                                  failure=FailureCode.RECONCILIATION_PENDING)
            return task
        url = f"{self._settings.api_origin}{TASK_PATH}/{provider_task_id}"
        # A query made after the wait has expired cannot borrow the expired deadline, so it gets
        # its own small, bounded clock. It is a status read: nothing is created and nothing renews.
        timeout = (min(self._settings.final_status_timeout, self._settings.request_timeout)
                   if past_wait else self._timeout(task.deadline))
        response = None
        try:
            response = self._transport.request("GET", url, headers=self._headers(url),
                                               timeout=timeout)
            payload = self._check(response)
        except AdapterError as exc:
            # Authentication, destination and consent refusals stay terminal and distinct; an
            # HTTP 429/5xx is this provider asking us to try again later, not a final answer.
            transient = exc.code in (FailureCode.TIMEOUT, FailureCode.PROVIDER_FAILURE,
                                     FailureCode.RATE_LIMIT)
            self._registry.note_poll(operation_id=task_id, now=self._clock(),
                                     http_status=getattr(response, "status", None),
                                     provider_status=None, outcome=f"ERROR:{exc.code.value}")
            if past_wait:
                self._registry.note_final(task_id, self._clock(), f"ERROR:{exc.code.value}")
                self._tasks[task_id] = task = replace(
                    task, status="UNRESOLVED", failure=FailureCode.RECONCILIATION_PENDING)
                return task
            if transient:
                # Keep the task pending and let the next tick ask again.
                return task
            raise
        data = payload.get("data") if isinstance(payload, dict) else None
        status = str((data or {}).get("task_status") or "").lower()
        results = (data or {}).get("results")
        result_url = None
        if isinstance(results, dict):                  # official example: "results": {"url": "..."}
            result_url = results.get("url")
        elif results is not None:
            raise AdapterError(FailureCode.INVALID_RESULT,
                               "results must be an object with a url, per the documented contract")
        if status in _TERMINAL_OK:
            if not result_url or not _permitted_media(result_url, self._settings.allowed_media_hosts):
                self._tasks[task_id] = task = replace(task, status="FAILED", failure=FailureCode.INVALID_RESULT)
            else:
                self._tasks[task_id] = task = replace(task, status="SUCCEEDED", media_handle=result_url)
        elif status in _TERMINAL_FAIL:
            self._tasks[task_id] = task = replace(task, status="FAILED", failure=FailureCode.PROVIDER_FAILURE)
        elif status not in _PENDING:
            self._tasks[task_id] = task = replace(task, status="FAILED", failure=FailureCode.INVALID_RESULT)
        outcome = ("SUCCEEDED" if status in _TERMINAL_OK else
                   "FAILED" if status in _TERMINAL_FAIL else "PENDING")
        self._registry.note_poll(operation_id=task_id, now=self._clock(),
                                 http_status=getattr(response, "status", None),
                                 provider_status=status or None, outcome=outcome)
        if self._tasks[task_id].status == "PENDING" and past_wait:
            # Waiting stopped; the provider has not answered. Keep the handle, say so plainly.
            self._registry.note_final(task_id, self._clock(), outcome)
            self._tasks[task_id] = replace(self._tasks[task_id], status="UNRESOLVED",
                                           failure=FailureCode.RECONCILIATION_PENDING)
        return self._tasks[task_id]

    def adopt_consent(self, session_id: str, grant: ConsentGrant) -> None:
        """Restore the session's grant after a restart, from the application's stored record.

        The media handles a fresh process never saw are gone, so the grant is kept under a
        session key rather than a media handle. Revocation is still evaluated on every poll.
        """
        self._consent[f"session:{session_id}"] = grant

    def _consent_for_session(self, session_id: str) -> ConsentGrant | None:
        restored = self._consent.get(f"session:{session_id}")
        if restored is not None:
            return restored
        for handle, grant in self._consent.items():
            ref = self._media.get(handle)
            if ref is not None and ref.session_id == session_id:
                return grant
        return None

    def fetch_result_image(self, url: str, *, deadline: datetime) -> tuple[bytes, str]:
        """Fetch provider result bytes through the same validated, size-bounded transport.

        Validated bytes are kept, so the page's later request for the same image costs no second
        provider fetch.
        """
        cached = self._result_bytes.get(url)
        if cached is not None:
            return cached
        if not _permitted_media(url, self._settings.allowed_media_hosts):
            raise AdapterError(FailureCode.DISALLOWED_DESTINATION,
                               "result media host is not allow-listed")
        # Images get their own ceiling: 10 MB, not the 256 KB JSON response bound.
        response = self._transport.request("GET", url, headers={}, timeout=self._timeout(deadline),
                                           max_bytes=MAX_IMAGE_BYTES)
        if response.status != 200:
            raise AdapterError(FailureCode.PROVIDER_FAILURE, f"result fetch returned {response.status}")
        if len(response.body) > MAX_IMAGE_BYTES:
            raise AdapterError(FailureCode.INVALID_RESULT, "result image exceeded the size bound")
        data, content_type, _width, _height = decode_result_image(response.body)
        self._result_bytes[url] = (data, content_type)
        return data, content_type

    def build_result(self, *, session_id: str, plan: PreviewPlan,
                     tasks: tuple[PreviewTask, ...]) -> PreviewResult:
        # Delivering imagery is itself an outbound decision: current consent is required even when
        # the provider already returned a URL.
        grant = self._consent_for_session(session_id)
        if any(t.status == "SUCCEEDED" for t in tasks):
            if grant is None:
                raise AdapterError(FailureCode.CONSENT_REQUIRED, "no consent on record for this delivery")
            grant.assert_live(self._clock())
        # Provider completion is not delivery. Each candidate that the provider called done must
        # still yield bytes this product can actually show; otherwise it is an unusable result,
        # not a delivered preview, and the page must say so instead of rendering a broken image.
        validated: list[PreviewTask] = []
        for task in tasks:
            if task.status != "SUCCEEDED":
                validated.append(task)
                continue
            if not task.media_handle:
                validated.append(replace(task, status="FAILED", failure=FailureCode.INVALID_RESULT))
                continue
            try:
                self.fetch_result_image(
                    task.media_handle,
                    deadline=self._clock() + timedelta(
                        seconds=max(float(self._settings.final_status_timeout), 5.0)))
            except AdapterError as exc:
                self._registry.note_poll(
                    operation_id=task.task_id, now=self._clock(), http_status=None,
                    provider_status="success", outcome=f"RESULT_UNUSABLE:{exc.code.value}")
                validated.append(replace(task, status="FAILED", failure=FailureCode.INVALID_RESULT))
                continue
            validated.append(task)
        tasks = tuple(validated)

        succeeded = [t for t in tasks if t.status == "SUCCEEDED"]
        failed = [t for t in tasks if t.status == "FAILED"]
        unresolved = [t for t in tasks if t.status == "UNRESOLVED"]
        if unresolved and not failed and not succeeded:
            status = "UNRESOLVED"
        elif failed:
            status = "PARTIAL" if succeeded else "FAILED"
        else:
            status = "PARTIAL" if unresolved else "SUCCEEDED"
        return PreviewResult(session_id=session_id, mode="LIVE", status=status,
                             candidate_ids=tuple(t.candidate_id for t in tasks),
                             task_ids=tuple(t.task_id for t in tasks),
                             created_at=self._clock(),
                             expires_at=self._clock() + RESULT_VALIDITY,
                             media_handles=tuple(t.media_handle if t.status == "SUCCEEDED" else None
                                                 for t in tasks),
                             failure_code=(failed[0].failure.value if failed and failed[0].failure
                                           else (FailureCode.RECONCILIATION_PENDING.value
                                                 if unresolved else None)))

    def discard_local_media(self, handle: str, *, session_id: str) -> None:
        ref = self._media.get(handle)
        if ref is None or ref.session_id != session_id:
            return
        for store in (self._payloads, self._references, self._rights, self._consent, self._media):
            store.pop(handle, None)

    def discard_session_media(self, session_id: str) -> tuple[str, ...]:
        handles = tuple(h for h, m in self._media.items() if m.session_id == session_id)
        for handle in handles:
            self.discard_local_media(handle, session_id=session_id)
        return handles
