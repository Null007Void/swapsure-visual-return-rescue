"""Bounded intake for the consenting shopper's own photo.

Only a real, decodable JPEG/PNG inside the documented provider limits is accepted. The bytes are
stored privately under the data directory and are never served back to a browser; they leave the
process only through the provider adapter, and only while consent is live.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Any

from adapters.youcam_live import sniff_image
from adapters.youcam_stub import AdapterError, FailureCode

ALLOWED_TYPES = ("image/png",)


class PhotoStore:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, session_id: str) -> tuple[pathlib.Path, pathlib.Path]:
        safe = session_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.bin", self.root / f"{safe}.json"

    def submit(self, session_id: str, payload: bytes, content_type: str, *, now: datetime) -> dict[str, Any]:
        if content_type not in ALLOWED_TYPES:
            raise AdapterError(FailureCode.INVALID_INPUT, "the shopper photo must be a PNG")
        width, height = sniff_image(payload, content_type)
        blob, meta = self._paths(session_id)
        blob.write_bytes(bytes(payload))
        meta.write_text(json.dumps({"content_type": content_type, "width": width, "height": height,
                                    "bytes": len(payload),
                                    "received_at": now.astimezone(timezone.utc).isoformat()}), encoding="utf-8")
        return {"content_type": content_type, "width": width, "height": height, "bytes": len(payload)}

    def load(self, session_id: str) -> tuple[bytes, str]:
        blob, meta = self._paths(session_id)
        if not blob.exists() or not meta.exists():
            raise AdapterError(FailureCode.INVALID_INPUT, "no shopper photo has been provided")
        return blob.read_bytes(), json.loads(meta.read_text(encoding="utf-8"))["content_type"]

    def has(self, session_id: str) -> bool:
        return self._paths(session_id)[0].exists()

    def delete(self, session_id: str) -> None:
        for path in self._paths(session_id):
            path.unlink(missing_ok=True)
