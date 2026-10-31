"""Outcome ledger and retained-GMV accounting — `docs/05-decision-contracts.md`.

Rules encoded here:

* only **committed** outcomes are recorded; pending, failed and refund outcomes
  contribute zero retained GMV;
* retained original GMV for a committed exchange is ``min(original, replacement)``;
* a $89 -> $84 exchange retains $84.00 and refunds $5.00;
* a $89 -> $99 exchange retains $89.00 and charges $10.00 — the $99 is never
  counted as "original value rescued";
* dedupe by committed transaction id, so a retried or duplicated event can never
  inflate retained value (T34);
* delivery is a transactional outbox: the record is persisted before delivery and
  marked delivered only after the sink acknowledges, with the sink keyed by
  transaction id so a crash between the two cannot double-apply;
* a zero-attempt denominator yields "N/A", never a fabricated percentage (T38).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

OUTCOME_EXCHANGE_COMMITTED = "EXCHANGE_COMMITTED"
OUTCOME_REFUND_HANDOFF = "REFUND_HANDOFF"
OUTCOME_PENDING = "PENDING"
OUTCOME_RECONCILING = "RECONCILING"
OUTCOMES: tuple[str, ...] = (
    OUTCOME_EXCHANGE_COMMITTED,
    OUTCOME_REFUND_HANDOFF,
    OUTCOME_PENDING,
    OUTCOME_RECONCILING,
)

Sink = Callable[["OutcomeRecord"], None]


class LedgerError(ValueError):
    """A ledger record violates the accounting contract."""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def retained_gmv_minor(original_value_minor: int, replacement_value_minor: int) -> int:
    """The only definition of retained original GMV in this codebase."""

    for name, value in (
        ("original_value_minor", original_value_minor),
        ("replacement_value_minor", replacement_value_minor),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LedgerError(f"{name} must be a non-negative integer")
    return min(original_value_minor, replacement_value_minor)


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One auditable outcome.  Carries no image bytes, handles or URLs."""

    transaction_id: str
    session_id: str
    order_id: str
    original_sku: str
    replacement_sku: str | None
    original_value_minor: int
    replacement_value_minor: int
    retained_gmv_minor: int
    partial_refund_minor: int
    additional_charge_minor: int
    currency: str
    outcome: str
    committed_at: datetime
    preview_mode: str
    policy_version: str
    eligibility_status: str

    def __post_init__(self) -> None:
        for name in ("transaction_id", "session_id", "order_id", "original_sku", "currency",
                     "outcome", "preview_mode", "policy_version", "eligibility_status"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise LedgerError(f"{name} must be a non-empty string")
        if self.outcome not in OUTCOMES:
            raise LedgerError(f"unknown outcome {self.outcome!r}")
        for name in ("original_value_minor", "replacement_value_minor", "retained_gmv_minor",
                     "partial_refund_minor", "additional_charge_minor"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LedgerError(f"{name} must be a non-negative integer")
        if self.committed_at.tzinfo is None:
            raise LedgerError("committed_at must be timezone-aware")

        if self.outcome == OUTCOME_EXCHANGE_COMMITTED:
            if self.replacement_sku is None:
                raise LedgerError("a committed exchange needs a replacement SKU")
            expected = retained_gmv_minor(self.original_value_minor, self.replacement_value_minor)
            if self.retained_gmv_minor != expected:
                raise LedgerError(
                    f"retained GMV {self.retained_gmv_minor} != min(original, replacement) {expected}"
                )
        else:
            if self.retained_gmv_minor != 0:
                raise LedgerError("non-committed outcomes retain zero GMV")

    def as_dict(self) -> dict[str, Any]:
        return {
            "transactionId": self.transaction_id,
            "sessionId": self.session_id,
            "orderId": self.order_id,
            "originalSku": self.original_sku,
            "replacementSku": self.replacement_sku,
            "originalValueMinor": self.original_value_minor,
            "replacementValueMinor": self.replacement_value_minor,
            "retainedGmvMinor": self.retained_gmv_minor,
            "partialRefundMinor": self.partial_refund_minor,
            "additionalChargeMinor": self.additional_charge_minor,
            "currency": self.currency,
            "outcome": self.outcome,
            "committedAt": _iso(self.committed_at),
            "previewMode": self.preview_mode,
            "policyVersion": self.policy_version,
            "eligibilityStatus": self.eligibility_status,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OutcomeRecord":
        return cls(
            transaction_id=str(raw["transactionId"]),
            session_id=str(raw["sessionId"]),
            order_id=str(raw["orderId"]),
            original_sku=str(raw["originalSku"]),
            replacement_sku=None if raw.get("replacementSku") is None else str(raw["replacementSku"]),
            original_value_minor=int(raw["originalValueMinor"]),
            replacement_value_minor=int(raw["replacementValueMinor"]),
            retained_gmv_minor=int(raw["retainedGmvMinor"]),
            partial_refund_minor=int(raw["partialRefundMinor"]),
            additional_charge_minor=int(raw["additionalChargeMinor"]),
            currency=str(raw["currency"]),
            outcome=str(raw["outcome"]),
            committed_at=_parse_iso(str(raw["committedAt"])),
            preview_mode=str(raw["previewMode"]),
            policy_version=str(raw["policyVersion"]),
            eligibility_status=str(raw["eligibilityStatus"]),
        )


def committed_exchange(
    *,
    transaction_id: str,
    session_id: str,
    order_id: str,
    original_sku: str,
    replacement_sku: str,
    original_value_minor: int,
    replacement_value_minor: int,
    currency: str,
    committed_at: datetime,
    preview_mode: str,
    policy_version: str,
    eligibility_status: str,
) -> OutcomeRecord:
    """Build the committed-exchange record with contract-derived accounting."""

    refund = max(original_value_minor - replacement_value_minor, 0)
    charge = max(replacement_value_minor - original_value_minor, 0)
    return OutcomeRecord(
        transaction_id=transaction_id,
        session_id=session_id,
        order_id=order_id,
        original_sku=original_sku,
        replacement_sku=replacement_sku,
        original_value_minor=original_value_minor,
        replacement_value_minor=replacement_value_minor,
        retained_gmv_minor=retained_gmv_minor(original_value_minor, replacement_value_minor),
        partial_refund_minor=refund,
        additional_charge_minor=charge,
        currency=currency,
        outcome=OUTCOME_EXCHANGE_COMMITTED,
        committed_at=committed_at,
        preview_mode=preview_mode,
        policy_version=policy_version,
        eligibility_status=eligibility_status,
    )


def non_committed_outcome(
    *,
    transaction_id: str,
    session_id: str,
    order_id: str,
    original_sku: str,
    replacement_sku: str | None,
    original_value_minor: int,
    replacement_value_minor: int,
    currency: str,
    outcome: str,
    committed_at: datetime,
    preview_mode: str,
    policy_version: str,
    eligibility_status: str,
) -> OutcomeRecord:
    """Refund/pending/reconciling outcome: explicitly zero retained GMV."""

    if outcome == OUTCOME_EXCHANGE_COMMITTED:
        raise LedgerError("use committed_exchange() for committed outcomes")
    return OutcomeRecord(
        transaction_id=transaction_id,
        session_id=session_id,
        order_id=order_id,
        original_sku=original_sku,
        replacement_sku=replacement_sku,
        original_value_minor=original_value_minor,
        replacement_value_minor=replacement_value_minor,
        retained_gmv_minor=0,
        partial_refund_minor=0,
        additional_charge_minor=0,
        currency=currency,
        outcome=outcome,
        committed_at=committed_at,
        preview_mode=preview_mode,
        policy_version=policy_version,
        eligibility_status=eligibility_status,
    )


@dataclass(frozen=True, slots=True)
class RecordResult:
    recorded: bool
    duplicate: bool
    transaction_id: str


@dataclass(frozen=True, slots=True)
class FlushResult:
    delivered: int
    failed: int
    remaining: int
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.remaining == 0


@dataclass(frozen=True, slots=True)
class Metrics:
    """Accounting view.  No percentages are invented from an empty denominator."""

    committed_exchanges: int
    retained_gmv_minor: int
    partial_refund_minor: int
    additional_charge_minor: int
    eligible_optins: int
    zero_retained: int
    currency: str = "USD"

    @property
    def has_denominator(self) -> bool:
        return self.eligible_optins > 0

    @property
    def rescue_conversion(self) -> float | None:
        """Committed exchanges / eligible sessions that opted in.

        ``None`` when nothing opted in — there is no honest percentage to report.
        """

        if not self.has_denominator:
            return None
        return self.committed_exchanges / self.eligible_optins

    def as_dict(self) -> dict[str, Any]:
        conversion = self.rescue_conversion
        return {
            "eligibleOptins": self.eligible_optins,
            "committedExchanges": self.committed_exchanges,
            "rescueConversion": "N/A" if conversion is None else round(conversion, 6),
            "retainedOriginalGmvMinor": self.retained_gmv_minor,
            "partialRefundMinor": self.partial_refund_minor,
            "additionalChargeMinor": self.additional_charge_minor,
            "currency": self.currency,
            "zeroRetainedOutcomes": self.zero_retained,
        }


class OutcomeLedger:
    """Committed-outcome ledger with a transactional outbox.

    The ledger is intentionally not thread-safe: the commerce store owns one lock
    that covers session state, inventory and ledger writes together, so that a
    refund and an exchange can never interleave.
    """

    def __init__(
        self,
        records: Iterable[OutcomeRecord] | None = None,
        pending: Iterable[OutcomeRecord] | None = None,
        delivered: Iterable[str] | None = None,
    ) -> None:
        self._records: dict[str, OutcomeRecord] = {}
        self._pending: list[OutcomeRecord] = []
        self._delivered: set[str] = set(delivered or ())
        for record in records or ():
            if record.transaction_id in self._records:
                raise LedgerError(f"duplicate transaction id in ledger state: {record.transaction_id}")
            self._records[record.transaction_id] = record
        for record in pending or ():
            if record.transaction_id not in self._records:
                raise LedgerError("pending outbox entry without a ledger record")
            if record.transaction_id not in self._delivered:
                self._pending.append(record)

    # -- reads ------------------------------------------------------------ #
    @property
    def records(self) -> tuple[OutcomeRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    @property
    def pending(self) -> tuple[OutcomeRecord, ...]:
        return tuple(self._pending)

    @property
    def delivered_ids(self) -> frozenset[str]:
        return frozenset(self._delivered)

    def get(self, transaction_id: str) -> OutcomeRecord | None:
        return self._records.get(transaction_id)

    def __len__(self) -> int:
        return len(self._records)

    # -- writes ----------------------------------------------------------- #
    def record(self, record: OutcomeRecord) -> RecordResult:
        """Record once.  A replay of the same transaction id is a no-op."""

        existing = self._records.get(record.transaction_id)
        if existing is not None:
            return RecordResult(recorded=False, duplicate=True, transaction_id=record.transaction_id)
        self._records[record.transaction_id] = record
        if record.transaction_id not in self._delivered:
            self._pending.append(record)
        return RecordResult(recorded=True, duplicate=False, transaction_id=record.transaction_id)

    def flush(self, sink: Sink) -> FlushResult:
        """Deliver pending records, at most once each, retrying on failure.

        The sink is called only for undelivered ids.  A sink that raises leaves its
        record pending for a later attempt; a sink that already applied the effect
        must key by transaction id, which keeps the retry idempotent.
        """

        delivered = 0
        failures: list[str] = []
        remaining: list[OutcomeRecord] = []
        for record in self._pending:
            if record.transaction_id in self._delivered:
                continue
            try:
                sink(record)
            except Exception as exc:  # noqa: BLE001 - the sink is a foreign boundary
                failures.append(f"{record.transaction_id}:{type(exc).__name__}")
                remaining.append(record)
                continue
            self._delivered.add(record.transaction_id)
            delivered += 1
        self._pending = remaining
        return FlushResult(
            delivered=delivered,
            failed=len(failures),
            remaining=len(self._pending),
            failures=tuple(failures),
        )

    # -- accounting ------------------------------------------------------- #
    def metrics(self, *, eligible_optins: int = 0, currency: str = "USD") -> Metrics:
        if isinstance(eligible_optins, bool) or not isinstance(eligible_optins, int) or eligible_optins < 0:
            raise LedgerError("eligible_optins must be a non-negative integer")
        committed = [record for record in self._records.values() if record.outcome == OUTCOME_EXCHANGE_COMMITTED]
        zero = len(self._records) - len(committed)
        return Metrics(
            committed_exchanges=len(committed),
            retained_gmv_minor=sum(record.retained_gmv_minor for record in committed),
            partial_refund_minor=sum(record.partial_refund_minor for record in committed),
            additional_charge_minor=sum(record.additional_charge_minor for record in committed),
            eligible_optins=eligible_optins,
            zero_retained=zero,
            currency=currency,
        )

    # -- persistence ------------------------------------------------------ #
    def to_state(self) -> dict[str, Any]:
        return {
            "records": [record.as_dict() for record in self.records],
            "pending": [record.as_dict() for record in self._pending],
            "delivered": sorted(self._delivered),
        }

    @classmethod
    def from_state(cls, raw: Mapping[str, Any]) -> "OutcomeLedger":
        return cls(
            records=[OutcomeRecord.from_dict(item) for item in raw.get("records", [])],
            pending=[OutcomeRecord.from_dict(item) for item in raw.get("pending", [])],
            delivered=list(raw.get("delivered", [])),
        )
