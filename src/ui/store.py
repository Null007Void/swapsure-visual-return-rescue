"""SQLite application store (WAL, atomic transactions).

The commerce simulator owns the authoritative session reducer state and persists
it to an atomically replaced JSON file; this store keeps the *application*
records the web workflow needs and mirrors the authoritative state on every
mutation:

* return case inputs (structured reasons, notes, preferences, upload handle),
* full session snapshots (state, version, decision, selection, quote, receipt),
* an append-only consent ledger,
* media metadata plus a deletion ledger that survives restart,
* receipts and outcome-ledger rows,
* bounded preview-job state,
* the latest simulator payload (used for consistency checks only).

Mirroring is idempotent and re-run on open, so a process that dies between the
simulator write and the SQLite mirror self-heals on the next start.  The mirror
is never the authority for a reducer decision.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import weakref
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    session_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    shopper_identity TEXT NOT NULL,
    request_token TEXT NOT NULL,
    approval_nonce TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    immediate_refund INTEGER NOT NULL,
    size TEXT NOT NULL,
    preferences TEXT NOT NULL DEFAULT '{}',
    order_payload TEXT NOT NULL DEFAULT '{}',
    catalog_rows TEXT NOT NULL DEFAULT '[]',
    upload_handle TEXT,
    consent_generation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_snapshots (
    session_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    consent_version TEXT,
    recorded_at TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS media (
    handle TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    deleted_at TEXT,
    local_status TEXT NOT NULL DEFAULT 'PRESENT',
    provider_status TEXT NOT NULL DEFAULT 'NOT_APPLICABLE_MOCK',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS deletion_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT NOT NULL,
    session_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    local_status TEXT NOT NULL,
    provider_status TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS image_tombstones (
    session_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS uploads (
    handle TEXT PRIMARY KEY,
    session_id TEXT,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    commit_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    committed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_rows (
    transaction_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    retained_gmv_minor INTEGER NOT NULL,
    payload TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preview_jobs (
    session_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commerce_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("store timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class CaseRow:
    session_id: str
    order_id: str
    shopper_identity: str
    request_token: str
    approval_nonce: str
    reason_codes: tuple[str, ...]
    notes: str
    immediate_refund: bool
    size: str
    preferences: Mapping[str, Any]
    order_payload: Mapping[str, Any]
    catalog_rows: tuple[Mapping[str, Any], ...]
    upload_handle: str | None
    consent_generation: int
    created_at: datetime
    updated_at: datetime


def _row_to_case(row: sqlite3.Row) -> CaseRow:
    return CaseRow(
        session_id=row["session_id"],
        order_id=row["order_id"],
        shopper_identity=row["shopper_identity"],
        request_token=row["request_token"],
        approval_nonce=row["approval_nonce"],
        reason_codes=tuple(json.loads(row["reason_codes"])),
        notes=row["notes"],
        immediate_refund=bool(row["immediate_refund"]),
        size=row["size"],
        preferences=json.loads(row["preferences"]),
        order_payload=json.loads(row["order_payload"]),
        catalog_rows=tuple(json.loads(row["catalog_rows"])),
        upload_handle=row["upload_handle"],
        consent_generation=int(row["consent_generation"]),
        created_at=parse_iso(row["created_at"]),
        updated_at=parse_iso(row["updated_at"]),
    )


def _release_connection(con: sqlite3.Connection) -> None:
    """Close a connection that was never closed explicitly, before the interpreter warns about it."""
    try:
        con.close()
    except Exception:  # already closed, or closed from a dying thread
        pass


class Store:
    """One connection, one lock, transactions per operation."""

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schemaVersion', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
        # A caller that forgets close() must not leak the connection: finalise it deterministically
        # so Python 3.13's audit of unclosed databases does not fire at shutdown.
        self._finalizer = weakref.finalize(self, _release_connection, self._conn)

    # -- lifecycle ------------------------------------------------------- #
    def close(self) -> None:
        with self._lock:
            self._finalizer.detach()
            _release_connection(self._conn)

    def journal_mode(self) -> str:
        with self._lock:
            return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    def _write(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, tuple(params))

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    # -- cases ----------------------------------------------------------- #
    def add_case(self, case: CaseRow) -> None:
        self._write(
            "INSERT INTO cases(session_id, order_id, shopper_identity, request_token,"
            " approval_nonce, reason_codes, notes, immediate_refund, size, preferences,"
            " order_payload, catalog_rows, upload_handle, consent_generation, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case.session_id,
                case.order_id,
                case.shopper_identity,
                case.request_token,
                case.approval_nonce,
                json.dumps(list(case.reason_codes)),
                case.notes,
                1 if case.immediate_refund else 0,
                case.size,
                json.dumps(dict(case.preferences), sort_keys=True),
                json.dumps(dict(case.order_payload), sort_keys=True),
                json.dumps(list(case.catalog_rows), sort_keys=True),
                case.upload_handle,
                case.consent_generation,
                iso(case.created_at),
                iso(case.updated_at),
            ),
        )

    def get_case(self, session_id: str) -> CaseRow | None:
        rows = self._query("SELECT * FROM cases WHERE session_id = ?", (session_id,))
        return None if not rows else _row_to_case(rows[0])

    def update_case(self, session_id: str, *, now: datetime, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "size",
            "preferences",
            "consent_generation",
            "upload_handle",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown case fields: {sorted(unknown)}")
        assignments: list[str] = []
        params: list[Any] = []
        for name, value in fields.items():
            assignments.append(f"{name} = ?")
            if name == "preferences":
                params.append(json.dumps(dict(value), sort_keys=True))
            elif name == "consent_generation":
                params.append(int(value))
            else:
                params.append(value)
        assignments.append("updated_at = ?")
        params.append(iso(now))
        params.append(session_id)
        self._write(f"UPDATE cases SET {', '.join(assignments)} WHERE session_id = ?", params)

    # -- session snapshots ----------------------------------------------- #
    def upsert_snapshot(self, session_id: str, state: str, version: int, payload: Mapping[str, Any], now: datetime) -> None:
        self._write(
            "INSERT INTO session_snapshots(session_id, state, version, payload, updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
            " state=excluded.state, version=excluded.version,"
            " payload=excluded.payload, updated_at=excluded.updated_at",
            (session_id, state, int(version), json.dumps(dict(payload), sort_keys=True), iso(now)),
        )

    def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM session_snapshots WHERE session_id = ?", (session_id,))
        if not rows:
            return None
        return {
            "sessionId": rows[0]["session_id"],
            "state": rows[0]["state"],
            "version": rows[0]["version"],
            "payload": json.loads(rows[0]["payload"]),
            "updatedAt": rows[0]["updated_at"],
        }

    # -- consent --------------------------------------------------------- #
    def record_consent(self, session_id: str, action: str, consent_version: str | None, now: datetime, detail: str | None = None) -> None:
        self._write(
            "INSERT INTO consent_ledger(session_id, action, consent_version, recorded_at, detail)"
            " VALUES(?,?,?,?,?)",
            (session_id, action, consent_version, iso(now), detail),
        )

    def record_image_tombstone(self, session_id: str, reason: str, now: datetime,
                               detail: str | None = None) -> None:
        """Durably record that imagery was deleted for this session."""
        self._write(
            "INSERT OR REPLACE INTO image_tombstones(session_id, reason, recorded_at, detail)"
            " VALUES(?,?,?,?)",
            (session_id, reason, iso(now), detail),
        )

    def get_image_tombstone(self, session_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM image_tombstones WHERE session_id = ?", (session_id,)
        )
        return None if not rows else dict(rows[0])

    def consent_history(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM consent_ledger WHERE session_id = ? ORDER BY id", (session_id,)
        )
        return [dict(row) for row in rows]

    def latest_consent(self, session_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM consent_ledger WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        return None if not rows else dict(rows[0])

    # -- media + deletion ledger ---------------------------------------- #
    def add_media(self, row: Mapping[str, Any]) -> None:
        self._write(
            "INSERT OR REPLACE INTO media(handle, session_id, candidate_id, path,"
            " content_type, byte_size, created_at, expires_at, deleted_at, local_status,"
            " provider_status, attempts, last_error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["handle"],
                row["session_id"],
                row["candidate_id"],
                row["path"],
                row["content_type"],
                int(row["byte_size"]),
                row["created_at"],
                row["expires_at"],
                row.get("deleted_at"),
                row.get("local_status", "PRESENT"),
                row.get("provider_status", "NOT_APPLICABLE_MOCK"),
                int(row.get("attempts", 0)),
                row.get("last_error"),
            ),
        )

    def get_media(self, handle: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM media WHERE handle = ?", (handle,))
        return None if not rows else dict(rows[0])

    def list_media(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM media WHERE session_id = ? ORDER BY created_at, handle",
            (session_id,),
        )
        return [dict(row) for row in rows]

    def list_media_all(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM media ORDER BY created_at, handle")
        return [dict(row) for row in rows]

    def mark_media(self, handle: str, **fields: Any) -> None:
        allowed = {"deleted_at", "local_status", "provider_status", "attempts", "last_error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown media fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._write(f"UPDATE media SET {assignments} WHERE handle = ?", (*fields.values(), handle))

    def record_deletion(self, handle: str, session_id: str, now: datetime, local_status: str, provider_status: str, note: str | None = None) -> None:
        self._write(
            "INSERT INTO deletion_ledger(handle, session_id, recorded_at, local_status,"
            " provider_status, note) VALUES(?,?,?,?,?,?)",
            (handle, session_id, iso(now), local_status, provider_status, note),
        )

    def deletion_history(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id is None:
            rows = self._query("SELECT * FROM deletion_ledger ORDER BY id")
        else:
            rows = self._query(
                "SELECT * FROM deletion_ledger WHERE session_id = ? ORDER BY id", (session_id,)
            )
        return [dict(row) for row in rows]

    def pending_deletions(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM media WHERE local_status = 'PENDING_RETRY'")
        return [dict(row) for row in rows]

    # -- uploads --------------------------------------------------------- #
    def add_upload(self, row: Mapping[str, Any]) -> None:
        self._write(
            "INSERT OR REPLACE INTO uploads(handle, session_id, filename, sha256, byte_size,"
            " path, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                row["handle"],
                row.get("session_id"),
                row["filename"],
                row["sha256"],
                int(row["byte_size"]),
                row["path"],
                row["created_at"],
            ),
        )

    def get_upload(self, handle: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM uploads WHERE handle = ?", (handle,))
        return None if not rows else dict(rows[0])

    def attach_upload(self, handle: str, session_id: str) -> None:
        self._write("UPDATE uploads SET session_id = ? WHERE handle = ?", (session_id, handle))

    # -- receipts + ledger ---------------------------------------------- #
    def upsert_receipt(self, commit_id: str, session_id: str, payload: Mapping[str, Any], committed_at: datetime) -> None:
        self._write(
            "INSERT INTO receipts(commit_id, session_id, payload, committed_at)"
            " VALUES(?,?,?,?) ON CONFLICT(commit_id) DO UPDATE SET"
            " payload=excluded.payload, committed_at=excluded.committed_at",
            (commit_id, session_id, json.dumps(dict(payload), sort_keys=True), iso(committed_at)),
        )

    def get_receipt(self, session_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM receipts WHERE session_id = ? ORDER BY committed_at DESC LIMIT 1",
            (session_id,),
        )
        if not rows:
            return None
        payload = json.loads(rows[0]["payload"])
        payload["_commitId"] = rows[0]["commit_id"]
        return payload

    def upsert_ledger_row(self, transaction_id: str, session_id: str, outcome: str, retained_gmv_minor: int, payload: Mapping[str, Any], recorded_at: datetime) -> None:
        self._write(
            "INSERT INTO ledger_rows(transaction_id, session_id, outcome,"
            " retained_gmv_minor, payload, recorded_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(transaction_id) DO UPDATE SET outcome=excluded.outcome,"
            " retained_gmv_minor=excluded.retained_gmv_minor,"
            " payload=excluded.payload, recorded_at=excluded.recorded_at",
            (
                transaction_id,
                session_id,
                outcome,
                int(retained_gmv_minor),
                json.dumps(dict(payload), sort_keys=True),
                iso(recorded_at),
            ),
        )

    def ledger_rows(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM ledger_rows ORDER BY recorded_at, transaction_id")
        return [dict(row) for row in rows]

    def ledger_totals(self) -> dict[str, int]:
        rows = self._query(
            "SELECT COALESCE(SUM(retained_gmv_minor), 0) AS retained,"
            " COUNT(*) AS rows FROM ledger_rows"
        )
        row = rows[0]
        return {"retainedGmvMinor": int(row["retained"]), "rows": int(row["rows"])}

    # -- preview jobs ---------------------------------------------------- #
    def save_job(self, session_id: str, payload: Mapping[str, Any], now: datetime) -> None:
        self._write(
            "INSERT INTO preview_jobs(session_id, payload, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload,"
            " updated_at=excluded.updated_at",
            (session_id, json.dumps(dict(payload), sort_keys=True), iso(now)),
        )

    def load_job(self, session_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT payload FROM preview_jobs WHERE session_id = ?", (session_id,))
        return None if not rows else json.loads(rows[0]["payload"])

    def delete_job(self, session_id: str) -> None:
        self._write("DELETE FROM preview_jobs WHERE session_id = ?", (session_id,))

    # -- commerce mirror ------------------------------------------------- #
    def save_commerce(self, payload: Mapping[str, Any], now: datetime) -> None:
        self._write(
            "INSERT INTO commerce_state(id, payload, updated_at) VALUES(1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
            " updated_at=excluded.updated_at",
            (json.dumps(dict(payload), sort_keys=True), iso(now)),
        )

    def load_commerce(self) -> dict[str, Any] | None:
        rows = self._query("SELECT payload FROM commerce_state WHERE id = 1")
        return None if not rows else json.loads(rows[0]["payload"])

    # -- full mirror ----------------------------------------------------- #
    def sync(self, sim: Any, now: datetime) -> None:
        """Mirror authoritative simulator state into the application store."""

        payload = sim.to_state()
        with self._lock, self._conn:
            for session in payload["sessions"]:
                self._conn.execute(
                    "INSERT INTO session_snapshots(session_id, state, version, payload, updated_at)"
                    " VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
                    " state=excluded.state, version=excluded.version,"
                    " payload=excluded.payload, updated_at=excluded.updated_at",
                    (
                        session["sessionId"],
                        session["state"],
                        int(session["version"]),
                        json.dumps(session, sort_keys=True),
                        iso(now),
                    ),
                )
            for record in sim.ledger.records:
                self._conn.execute(
                    "INSERT INTO ledger_rows(transaction_id, session_id, outcome,"
                    " retained_gmv_minor, payload, recorded_at) VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(transaction_id) DO UPDATE SET outcome=excluded.outcome,"
                    " retained_gmv_minor=excluded.retained_gmv_minor,"
                    " payload=excluded.payload, recorded_at=excluded.recorded_at",
                    (
                        record.transaction_id,
                        record.session_id,
                        record.outcome,
                        int(record.retained_gmv_minor),
                        json.dumps(record.as_dict(), sort_keys=True),
                        iso(record.committed_at),
                    ),
                )
            for transaction in sim.transactions():
                if transaction.receipt is None:
                    continue
                self._conn.execute(
                    "INSERT INTO receipts(commit_id, session_id, payload, committed_at)"
                    " VALUES(?,?,?,?) ON CONFLICT(commit_id) DO UPDATE SET"
                    " payload=excluded.payload, committed_at=excluded.committed_at",
                    (
                        transaction.receipt.commit_id,
                        transaction.session_id,
                        json.dumps(transaction.receipt.as_dict(), sort_keys=True),
                        iso(transaction.receipt.committed_at),
                    ),
                )
            self._conn.execute(
                "INSERT INTO commerce_state(id, payload, updated_at) VALUES(1,?,?)"
                " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                " updated_at=excluded.updated_at",
                (json.dumps(payload, sort_keys=True), iso(now)),
            )

    def reset(self) -> None:
        tables = (
            "cases",
            "session_snapshots",
            "consent_ledger",
            "media",
            "deletion_ledger",
            "uploads",
            "receipts",
            "ledger_rows",
            "preview_jobs",
            "commerce_state",
        )
        with self._lock, self._conn:
            for table in tables:
                self._conn.execute(f"DELETE FROM {table}")
