"""Bounded shopper-supplied scenario input (own order + catalog + reasons).

The normal path is the form: structured reason checkboxes over the synthetic
fixture.  This module is the optional second path — a file picker for a shopper
who already has their own order/catalog data.  It is deliberately narrow:

* UTF-8 JSON only, capped at 64 KiB, at most 20 catalog rows,
* a strict allow-list of keys and types (unknown keys are refused, not ignored),
* validated through the same value contracts the domain uses (``OrderView`` and
  ``Item.parse``), so a file cannot smuggle a price, a size or a rule override
  into the reducer,
* the exact captured bytes are hashed and stored outside any public directory;
  nothing is re-served to the browser.

Descriptions, notes and any script-looking text stay inert data: they are never
interpreted, only escaped when displayed.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from typing import Any, Mapping

from commerce.order import OrderView
from domain.catalog import Item
from ui.security import new_id
from ui.store import iso

MAX_UPLOAD_BYTES = 64 * 1024
MAX_ROWS = 20
MAX_TEXT = 64
MAX_REASON_CODES = 10

_CURRENCY = re.compile(r"^[A-Z]{3}$")
_TOP_LEVEL = frozenset({"order", "catalog", "reasonCodes", "preferences"})
_ORDER_KEYS = frozenset(
    {"orderId", "merchant", "category", "size", "currency", "priceMinor", "colorFamily", "sku"}
)
_ROW_KEYS = frozenset(
    {
        "sku",
        "merchant",
        "category",
        "size",
        "currency",
        "priceMinor",
        "stock",
        "colorFamily",
        "referenceRights",
        "style",
        "occasion",
        "exchangePolicy",
    }
)


class UploadError(Exception):
    def __init__(
        self, code: str, message: str, *, status: int = 400, details: tuple[str, ...] = ()
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class UploadScenario:
    order: OrderView
    rows: tuple[Mapping[str, Any], ...]
    reason_codes: tuple[str, ...]
    preferences: Mapping[str, Any]
    raw: bytes
    sha256: str
    byte_size: int
    filename: str


def _fail(code: str, message: str, details: tuple[str, ...] = ()) -> UploadError:
    return UploadError(code, message, status=400, details=details)


def _text(value: Any, name: str, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("INVALID_FIELD", f"{name} must be a non-empty string")
    if len(value) > limit:
        raise _fail("INVALID_FIELD", f"{name} exceeds {limit} characters")
    if any(ord(char) < 32 for char in value):
        raise _fail("INVALID_FIELD", f"{name} contains control characters")
    return value


def _integer(value: Any, name: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail("INVALID_FIELD", f"{name} must be an integer")
    if value < low or value > high:
        raise _fail("INVALID_FIELD", f"{name} must be between {low} and {high}")
    return value


def _money(value: Any, name: str) -> int:
    return _integer(value, name, low=0, high=10_000_000)


def _extract_document(body: bytes, *, content_type: str) -> tuple[bytes, str]:
    """Return the JSON bytes and a display filename from a multipart or JSON body."""

    declared = (content_type or "").split(";")[0].strip().lower()
    if declared == "application/json":
        return body, "scenario.json"
    if declared != "multipart/form-data":
        raise UploadError(
            "UNSUPPORTED_MEDIA_TYPE",
            "upload must be multipart/form-data with one JSON file",
            status=415,
        )
    header = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8")
    try:
        message = BytesParser(policy=email_policy).parsebytes(header + body)
    except Exception:
        raise _fail("MALFORMED_MULTIPART", "the upload could not be parsed") from None
    files: list[tuple[str, bytes]] = []
    for part in message.iter_parts():
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append((filename, payload))
    if not files:
        raise _fail("MISSING_FILE", "choose a .json file to upload")
    if len(files) > 1:
        raise _fail("TOO_MANY_FILES", "upload exactly one JSON file")
    filename, payload = files[0]
    safe = pathlib.PurePath(filename.replace("\\", "/")).name
    safe = "".join(char for char in safe if ord(char) >= 32)[:80] or "scenario.json"
    return payload, safe


def parse_upload(body: bytes, *, content_type: str) -> UploadScenario:
    if not body:
        raise _fail("EMPTY_UPLOAD", "the upload was empty")
    if len(body) > MAX_UPLOAD_BYTES:
        raise UploadError(
            "UPLOAD_TOO_LARGE",
            f"the upload exceeds {MAX_UPLOAD_BYTES} bytes",
            status=413,
        )
    raw, filename = _extract_document(body, content_type=content_type)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise UploadError(
            "UPLOAD_TOO_LARGE",
            f"the scenario document exceeds {MAX_UPLOAD_BYTES} bytes",
            status=413,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("BAD_ENCODING", "the file must be UTF-8 encoded JSON") from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail("BAD_JSON", f"invalid JSON at line {exc.lineno} column {exc.colno}") from None
    if not isinstance(document, dict):
        raise _fail("BAD_SHAPE", "the document must be a JSON object")

    unknown = set(document) - _TOP_LEVEL
    if unknown:
        raise _fail("UNEXPECTED_FIELD", "unexpected top-level fields", tuple(sorted(unknown)))
    missing = _TOP_LEVEL - set(document)
    if missing:
        raise _fail("MISSING_FIELD", "missing top-level fields", tuple(sorted(missing)))

    order_raw = document["order"]
    if not isinstance(order_raw, dict):
        raise _fail("INVALID_FIELD", "order must be an object")
    unknown = set(order_raw) - _ORDER_KEYS
    if unknown:
        raise _fail("UNEXPECTED_FIELD", "unexpected order fields", tuple(sorted(unknown)))
    for key in ("merchant", "category", "size", "currency", "colorFamily", "sku"):
        if key not in order_raw:
            raise _fail("MISSING_FIELD", f"order.{key} is required")
    currency = _text(order_raw["currency"], "order.currency", limit=3)
    if not _CURRENCY.match(currency):
        raise _fail("INVALID_FIELD", "order.currency must be a three-letter upper-case code")
    order = OrderView(
        order_id=_text(order_raw.get("orderId", "ORD-UPLOAD-1"), "order.orderId", limit=64),
        merchant=_text(order_raw["merchant"], "order.merchant"),
        category=_text(order_raw["category"], "order.category"),
        size=_text(order_raw["size"], "order.size", limit=12),
        color_family=_text(order_raw["colorFamily"], "order.colorFamily", limit=40),
        original_sku=_text(order_raw["sku"], "order.sku"),
        original_value_minor=_money(order_raw["priceMinor"], "order.priceMinor"),
        currency=currency,
    )
    if order.currency != "USD":
        raise _fail(
            "OUT_OF_FIXTURE_SCOPE",
            "the offline money contract is USD only; other currencies need authoritative totals",
        )

    catalog = document["catalog"]
    if not isinstance(catalog, list) or not catalog:
        raise _fail("INVALID_FIELD", "catalog must be a non-empty list")
    if len(catalog) > MAX_ROWS:
        raise _fail("TOO_MANY_ROWS", f"catalog is limited to {MAX_ROWS} rows")
    rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(catalog):
        if not isinstance(raw_row, dict):
            raise _fail("INVALID_FIELD", f"catalog[{index}] must be an object")
        unknown = set(raw_row) - _ROW_KEYS
        if unknown:
            raise _fail(
                "UNEXPECTED_FIELD",
                f"catalog[{index}] has unexpected fields",
                tuple(sorted(unknown)),
            )
        for key in ("sku", "merchant", "category", "size", "currency", "colorFamily"):
            _text(raw_row.get(key), f"catalog[{index}].{key}")
        _money(raw_row.get("priceMinor"), f"catalog[{index}].priceMinor")
        _integer(raw_row.get("stock"), f"catalog[{index}].stock", low=0, high=1000)
        row_currency = _text(raw_row["currency"], f"catalog[{index}].currency", limit=3)
        if not _CURRENCY.match(row_currency):
            raise _fail("INVALID_FIELD", f"catalog[{index}].currency is malformed")
        if row_currency != order.currency:
            raise _fail(
                "CURRENCY_MISMATCH",
                f"catalog[{index}].currency differs from the order currency",
            )
        for optional in ("referenceRights", "style", "occasion", "exchangePolicy"):
            if optional in raw_row and raw_row[optional] is not None:
                _text(raw_row[optional], f"catalog[{index}].{optional}", limit=40)
        if raw_row.get("exchangePolicy") not in (None, "allowed", "denied"):
            raise _fail("INVALID_FIELD", f"catalog[{index}].exchangePolicy is not allowed/denied")
        item, problems = Item.parse(raw_row)
        if item is None:
            raise _fail(
                "INVALID_ROW",
                f"catalog[{index}] is not a usable catalog row",
                problems,
            )
        rows.append(dict(raw_row))

    reason_codes = document["reasonCodes"]
    if not isinstance(reason_codes, list):
        raise _fail("INVALID_FIELD", "reasonCodes must be a list")
    if len(reason_codes) > MAX_REASON_CODES:
        raise _fail("TOO_MANY_REASONS", f"at most {MAX_REASON_CODES} reason codes")
    for index, code in enumerate(reason_codes):
        _text(code, f"reasonCodes[{index}]", limit=40)

    preferences = document["preferences"]
    if not isinstance(preferences, dict):
        raise _fail("INVALID_FIELD", "preferences must be an object")
    unknown = set(preferences) - {"preferred", "constraints"}
    if unknown:
        raise _fail("UNEXPECTED_FIELD", "unexpected preference fields", tuple(sorted(unknown)))
    preferred = preferences.get("preferred", {})
    constraints = preferences.get("constraints", {})
    if not isinstance(preferred, dict) or not isinstance(constraints, dict):
        raise _fail("INVALID_FIELD", "preferred and constraints must be objects")
    for mapping_name, mapping in (("preferred", preferred), ("constraints", constraints)):
        unknown = set(mapping) - {"colorFamily", "style", "occasion"}
        if unknown:
            raise _fail(
                "UNEXPECTED_FIELD",
                f"unexpected {mapping_name} fields",
                tuple(sorted(unknown)),
            )
    for name, values in preferred.items():
        if not isinstance(values, list) or not values:
            raise _fail("INVALID_FIELD", f"preferred.{name} must be a non-empty list")
        for value in values:
            _text(value, f"preferred.{name}", limit=40)
    for name, value in constraints.items():
        _text(value, f"constraints.{name}", limit=40)

    return UploadScenario(
        order=order,
        rows=tuple(rows),
        reason_codes=tuple(str(code) for code in reason_codes),
        preferences={"preferred": preferred, "constraints": constraints},
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        filename=filename,
    )


def store_upload(
    scenario: UploadScenario,
    *,
    data_dir: str | pathlib.Path,
    session_id: str | None,
    now: datetime,
) -> Mapping[str, Any]:
    root = pathlib.Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    handle = new_id("upl")
    path = root / f"{handle}.json"
    path.write_bytes(scenario.raw)
    record = {
        "handle": handle,
        "session_id": session_id,
        "filename": scenario.filename,
        "sha256": scenario.sha256,
        "byte_size": scenario.byte_size,
        "path": str(path),
        "created_at": iso(now),
    }
    return record
