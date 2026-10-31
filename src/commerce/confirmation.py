"""The confirmation request value contract.

A confirmation is an *attempt*: it carries what the shopper's client claims, and it
is validated by the reducer and the simulator.  Keeping it a pure value (no store,
no lock, no I/O) lets the domain reducer, the simulator and the tests agree on one
shape without any of them depending on the others' machinery.

The payload digest deliberately excludes transport metadata (approval timestamp,
request token) so that a duplicate click carrying a fresh timestamp maps to the
same commerce intent, while a changed quote, candidate or action maps to a
conflict.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

ACTION_APPROVE_EXCHANGE = "APPROVE_EXCHANGE"
TRUSTED_ORIGIN = "shopper_ui"


@dataclass(frozen=True, slots=True)
class ConfirmRequest:
    """An attempt to commit an exchange, exactly as an untrusted client sends it."""

    session_id: str
    shopper_identity: str
    request_token: str
    quote_id: str
    quote_version: int
    candidate_sku: str
    action: str
    origin: str
    approved_at: datetime
    idempotency_key: str

    def commerce_payload(self) -> dict[str, Any]:
        """The fields that define *which* transaction this is."""

        return {
            "sessionId": self.session_id,
            "shopperIdentity": self.shopper_identity,
            "quoteId": self.quote_id,
            "quoteVersion": self.quote_version,
            "candidateSku": self.candidate_sku,
            "action": self.action,
            "origin": self.origin,
        }

    def payload_digest(self) -> str:
        canonical = json.dumps(self.commerce_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
