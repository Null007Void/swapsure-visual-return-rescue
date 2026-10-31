"""Application service: one shopper return case over the frozen Phase B contract.

The service is the only place that decides *which* domain call a page action maps
to.  It never computes eligibility, prices or candidate order itself: those come
from ``domain.eligibility``, ``domain.catalog``, ``commerce.quote`` and the atomic
simulator.  What it adds is the web workflow around them:

* server-authoritative session identity, request token and approval nonce,
* structured form input → validated domain input (free text never reaches a rule),
* consent and image lifecycle (grant, withdraw, revoke, purge, retry),
* bounded mock preview dispatch and polling,
* persistence through the SQLite application store plus the simulator journal.

Every refusal is a typed :class:`ServiceError` so the web layer can render an
honest status page instead of a stack trace.
"""

from __future__ import annotations

import os
import hashlib
import pathlib
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from datetime import timedelta

from adapters.youcam_live import ConsentGrant
from adapters.youcam_stub import AdapterError, FailureCode, adapter_for_mode
from commerce.inventory import StockRow
from commerce.order import OrderView
from commerce.quote import Quote
from commerce.simulator import CommerceSimulator, ConfirmResult, RefundResult, StoreError
from domain.catalog import (
    CandidateQuery,
    CandidateSelection,
    ExchangePolicy,
    InventorySnapshot,
    Item,
    ShopperPreferences,
    select_candidates,
)
from domain.eligibility import EligibilityDecision, decide
from domain.preview import PreviewResult
from domain.state_machine import (
    SessionState,
    is_precommit,
)
from specs import policy as load_policy
from ui.media import MediaManager, MediaView
from ui.previews import PreviewCoordinator
from ui.security import new_id, new_token, utc_now
from ui.store import CaseRow, Store

POLICY_VERSION = "1.0"
CONSENT_VERSION = "image-consent-v1"
MEDIA_REASON_ORDER = ("CONSENT_REVOKED", "SHOPPER_DELETED", "TTL_EXPIRED")


class ServiceError(Exception):
    """A typed, displayable failure.  ``status`` is the HTTP status class."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: Sequence[str] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.details = tuple(details)
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ScenarioInput:
    """A validated shopper-supplied order + catalog, or ``None`` for the fixture."""

    order: OrderView
    rows: tuple[Mapping[str, Any], ...]
    reason_codes: tuple[str, ...] = ()
    upload_handle: str | None = None
    sha256: str | None = None
    preferences: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CaseView:
    session: SessionState
    case: CaseRow
    decision: EligibilityDecision | None
    selection: CandidateSelection | None
    media: tuple[MediaView, ...]
    job: Mapping[str, Any] | None
    receipt: Mapping[str, Any] | None
    consent: Mapping[str, Any] | None
    consent_history: tuple[Mapping[str, Any], ...]
    deletions: tuple[Mapping[str, Any], ...]
    quote_remaining: float | None
    photo_present: bool = False

    @property
    def state(self) -> str:
        return self.session.state

    def media_for(self, candidate_id: str) -> MediaView | None:
        for item in self.media:
            if item.candidate_id == candidate_id and item.local_status == "PRESENT":
                return item
        return None


# How long a recorded image consent authorises outbound work. Recorded with the grant, so a
# restored session cannot silently extend it.
CONSENT_WINDOW = __import__("datetime").timedelta(minutes=30)


PNG_SIGNATURE = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


class ReturnService:
    """One data directory, one shopper store, one offline mode."""

    def __init__(
        self,
        data_dir: str | pathlib.Path,
        *,
        clock: Callable[[], datetime] | None = None,
        latency_seconds: float = 0.0,
        media_unlink: Callable[[str], None] | None = None,
        preview_mode: str = "MOCK",
        adapter: object | None = None,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock or utc_now
        self._lock = threading.RLock()

        from ui import views as _views  # imported late: views imports CaseView from here

        _views.configure_judging_notice(
            os.environ.get("SWAPSURE_JUDGING_NOTICE") == "1"
        )
        self.store = Store(self.data_dir / "app.db")
        self.preview_mode = str(preview_mode)
        # One adapter instance, shared by the service and its coordinator. Injecting it keeps the
        # application exactly as configured instead of swapping one reference out from under the other.
        self.adapter = adapter if adapter is not None else adapter_for_mode(preview_mode, clock=self._clock)
        self.media = MediaManager(
            self.data_dir / "media", self.store, clock=self._clock, unlink=media_unlink
        )
        self.coordinator = PreviewCoordinator(
            self.adapter, clock=self._clock, latency_seconds=latency_seconds
        )
        from ui.photo import PhotoStore
        from ui.references import ReferenceCatalog

        self.photo = PhotoStore(self.data_dir / "photos")
        self._result_cache: dict[tuple[str, str], tuple[bytes, str]] = {}
        # Tombstones are records, not bytes: they say imagery was deleted for this session.
        self._result_tombstones: dict[str, dict[str, Any]] = {}
        bundled = pathlib.Path(__file__).resolve().parents[2] / "assets" / "catalog" / "references"
        self.references = ReferenceCatalog(self.data_dir / "references", bundled)
        self.sim = CommerceSimulator.from_fixture_rows(
            _fixture_rows(), clock=self._clock, store_path=self.data_dir / "commerce.json"
        )
        # Retry any deletion the previous process could not finish, and drop
        # media that outlived its TTL while the process was down.
        self.media.sweep(self._clock())
        self._sync()

    # -- helpers --------------------------------------------------------- #
    @property
    def clock(self) -> Callable[[], datetime]:
        return self._clock

    def sample_photo(self, name: str) -> bytes | None:
        """A bundled synthetic sample image, served read-only. It touches no case and no ledger."""
        if name not in {"female-sample.png", "male-sample.png"}:
            return None
        path = pathlib.Path(__file__).resolve().parents[2] / "assets" / "samples" / name
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data if data[:8] == PNG_SIGNATURE else None

    def close(self) -> None:
        with self._lock:
            self.store.close()

    def _sync(self) -> None:
        self.store.sync(self.sim, self._clock())

    def _case(self, session_id: str) -> CaseRow:
        case = self.store.get_case(session_id)
        if case is None:
            raise ServiceError("UNKNOWN_SESSION", "no such return case", status=404)
        return case

    def _session(self, session_id: str) -> SessionState:
        try:
            return self.sim.session(session_id)
        except StoreError:
            raise ServiceError("UNKNOWN_SESSION", "no such return case", status=404) from None

    def _order(self, case: CaseRow) -> OrderView:
        return OrderView.from_dict(case.order_payload)

    def _rows(self, case: CaseRow) -> tuple[Mapping[str, Any], ...]:
        return case.catalog_rows or _fixture_rows()

    # -- reads ----------------------------------------------------------- #
    def get_case(self, session_id: str) -> CaseView:
        with self._lock:
            case = self._case(session_id)
            session = self._session(session_id)
            selection = session.selection
            if selection is None and session.state in ("CANDIDATES_READY", "PREVIEW_PENDING"):
                selection = self.selection_for(case, session)
            remaining = None
            if session.quote is not None:
                remaining = session.quote.seconds_remaining(self._clock())
            return CaseView(
                session=session,
                case=case,
                decision=session.decision,
                selection=selection,
                media=tuple(self.media.list_session(session_id)),
                job=self.store.load_job(session_id),
                receipt=session.receipt,  # type: ignore[arg-type]
                consent=self.store.latest_consent(session_id),
                consent_history=tuple(self.store.consent_history(session_id)),
                deletions=tuple(self.store.deletion_history(session_id)),
                quote_remaining=remaining,
                photo_present=self.photo.has(session_id),
            )

    def selection_for(self, case: CaseRow, session: SessionState | None = None) -> CandidateSelection:
        """Recompute candidate selection from the authoritative inventory now."""

        session = session or self._session(case.session_id)
        order = self._order(case)
        # The candidate universe is the shopper's own catalog (fixture by default),
        # while price and stock come from the authoritative inventory so that a
        # selection and its quote can never disagree.
        rows: list[dict[str, Any]] = []
        for raw in case.catalog_rows:
            sku = raw.get("sku") if isinstance(raw, Mapping) else None
            current = self.sim.inventory.get(str(sku)) if sku else None
            rows.append(current.as_row() if current is not None else dict(raw))
        order_item = Item(
            sku=order.original_sku,
            merchant=order.merchant,
            category=order.category,
            size=order.size,
            currency=order.currency,
            price_minor=order.original_value_minor,
            stock=1,
            color_family=order.color_family,
            reference_rights="synthetic-placeholder-only",
        )
        preferences = ShopperPreferences(
            constraints=dict(case.preferences.get("constraints", {})),
            preferred={
                name: tuple(values)
                for name, values in dict(case.preferences.get("preferred", {})).items()
                if values
            },
        )
        now = self._clock()
        query = CandidateQuery(
            session_id=case.session_id,
            order=order_item,
            shopper_size=case.size,
            reason_codes=case.reason_codes,
            snapshot=InventorySnapshot(
                version=self.sim.inventory.version,
                captured_at=now,
                rows=rows,
            ),
            preferences=preferences,
            exchange_policy=ExchangePolicy(
                max_price_delta_minor=int(load_policy()["fixturePriceDeltaCapMinor"])
            ),
        )
        return select_candidates(query, now=now)

    # -- case creation --------------------------------------------------- #
    def create_return(
        self,
        *,
        reason_codes: Sequence[str],
        immediate_refund: bool,
        notes: str = "",
        scenario: ScenarioInput | None = None,
        shopper_size: str | None = None,
    ) -> str:
        with self._lock:
            now = self._clock()
            order = scenario.order if scenario is not None else _fixture_order()
            rows = tuple(scenario.rows) if scenario is not None else _fixture_rows()
            self._admit_inventory(order, rows)

            session_id = new_id("ses")
            shopper_identity = "shopper_" + hashlib.sha256(session_id.encode()).hexdigest()[:16]
            request_token = new_token()
            approval_nonce = new_token()
            codes = tuple(str(code) for code in reason_codes)

            self.sim.open_session(
                order,
                shopper_identity=shopper_identity,
                request_token=request_token,
                session_id=session_id,
            )
            decision = decide(session_id, POLICY_VERSION, list(codes), bool(immediate_refund))
            result = self.sim.evaluate(session_id, decision)
            if not result.accepted:  # pragma: no cover - reducer guard
                raise ServiceError("EVALUATION_REFUSED", result.refusal or "refused", status=409)

            self.store.add_case(
                CaseRow(
                    session_id=session_id,
                    order_id=order.order_id,
                    shopper_identity=shopper_identity,
                    request_token=request_token,
                    approval_nonce=approval_nonce,
                    reason_codes=codes,
                    notes=str(notes)[:2000],
                    immediate_refund=bool(immediate_refund),
                    size=(shopper_size or order.size),
                    preferences=dict(scenario.preferences) if scenario is not None else {},
                    order_payload=order.as_dict(),
                    catalog_rows=rows,
                    upload_handle=None if scenario is None else scenario.upload_handle,
                    consent_generation=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            if scenario is not None and scenario.upload_handle:
                self.store.attach_upload(scenario.upload_handle, session_id)
            self._sync()
            return session_id

    def _admit_inventory(self, order: OrderView, rows: Sequence[Mapping[str, Any]]) -> None:
        """Admit a shopper-supplied catalog without silently rewriting the store."""

        existing_orders = {
            str(item["orderId"]): item for item in self.sim.to_state().get("orders", [])
        }
        prior = existing_orders.get(order.order_id)
        if prior is not None:
            if OrderView.from_dict(prior) != order:
                raise ServiceError(
                    "ORDER_CONFLICT",
                    "that order id already exists with different terms in this offline store",
                    status=409,
                )
            return
        for raw in rows:
            item, problems = Item.parse(raw)
            if item is None:
                raise ServiceError(
                    "CATALOG_ROW_REJECTED",
                    "a catalog row is not usable",
                    details=problems,
                )
            current = self.sim.inventory.get(item.sku)
            if current is None:
                self.sim.inventory.add(
                    StockRow(
                        sku=item.sku,
                        merchant=item.merchant,
                        category=item.category,
                        size=item.size,
                        currency=item.currency,
                        price_minor=item.price_minor,
                        stock=item.stock,
                        color_family=item.color_family,
                        reference_rights=item.reference_rights,
                        style=item.style,
                        occasion=item.occasion,
                    )
                )
            elif current.as_row() != _comparable_row(item):
                raise ServiceError(
                    "SKU_CONFLICT",
                    f"SKU {item.sku} already exists with different terms in this offline store",
                    status=409,
                )

    # -- eligibility-adjacent actions ------------------------------------ #
    def opt_in(self, session_id: str) -> None:
        with self._lock:
            result = self.sim.opt_in(session_id)
            if not result.accepted:
                raise ServiceError(result.refusal or "REFUSED", "rescue opt-in refused", status=409)
            self._sync()

    def decline(self, session_id: str, reason: str = "SHOPPER_CHOSE_REFUND") -> None:
        with self._lock:
            from domain.state_machine import DeclineRescue

            result = self.sim.apply_event(session_id, DeclineRescue(reason=reason))
            if not result.accepted:
                raise ServiceError(result.refusal or "REFUSED", "rescue decline refused", status=409)
            self._sync()

    def record_consent(
        self,
        session_id: str,
        *,
        image_consent: bool,
        mock_acknowledged: bool,
    ) -> None:
        with self._lock:
            case = self._case(session_id)
            if not image_consent:
                raise ServiceError(
                    "CONSENT_NOT_GIVEN",
                    "image-processing consent must be given explicitly",
                    status=400,
                )
            if not mock_acknowledged:
                raise ServiceError(
                    "MOCK_NOT_ACKNOWLEDGED",
                    "the mock-preview acknowledgement must be checked",
                    status=400,
                )
            result = self.sim.record_consent(session_id, CONSENT_VERSION)
            if not result.accepted:
                raise ServiceError(result.refusal or "REFUSED", "consent refused", status=409)
            now = self._clock()
            self.store.record_consent(
                session_id, "GRANTED", CONSENT_VERSION, now, detail="image processing + mock acknowledged"
            )
            self.store.update_case(
                session_id, now=now, consent_generation=case.consent_generation + 1
            )
            self._sync()

    def withdraw_consent(self, session_id: str) -> None:
        with self._lock:
            from domain.state_machine import WithdrawConsent

            result = self.sim.apply_event(
                session_id, WithdrawConsent(reason="SHOPPER_WITHDREW_CONSENT")
            )
            if not result.accepted:
                raise ServiceError(
                    result.refusal or "REFUSED", "consent withdrawal refused", status=409
                )
            now = self._clock()
            self.store.record_consent(session_id, "WITHDRAWN", None, now)
            self.store.delete_job(session_id)
            self.media.purge_session(session_id, reason="CONSENT_WITHDRAWN")
            self._sync()

    def revoke_image_consent(self, session_id: str) -> dict[str, Any]:
        """Withdraw image consent after it was granted.

        Future dispatch stops immediately, in-flight results can no longer be
        applied, local media is purged, and the rescue exits to the ordinary
        return route because no further image processing is authorised.
        """

        with self._lock:
            case = self._case(session_id)
            session = self._session(session_id)
            now = self._clock()
            self.store.record_consent(session_id, "REVOKED", None, now, detail="shopper revoked")
            self.store.update_case(
                session_id, now=now, consent_generation=case.consent_generation + 1
            )
            self.store.delete_job(session_id)
            purged = self.media.purge_session(session_id, reason="CONSENT_REVOKED")
            self._discard_local_imagery(session_id, reason="CONSENT_REVOKED")
            try:
                self.adapter.discard_session_media(session_id)
            except Exception:  # adapter memory is best-effort; the ledger is truth
                pass
            refunded = None
            if is_precommit(session.state):
                refunded = self.sim.request_refund(
                    session_id,
                    shopper_identity=case.shopper_identity,
                    request_token=case.request_token,
                    reason="CONSENT_REVOKED",
                )
            self._sync()
            return {
                "purged": len(purged),
                "localDeleted": sum(1 for item in purged if item.get("localStatus") == "DELETED"),
                "pendingRetry": sum(
                    1 for item in purged if item.get("localStatus") == "PENDING_RETRY"
                ),
                "providerStatus": "NOT_APPLICABLE_MOCK",
                "refund": refunded,
            }

    def delete_previews(self, session_id: str) -> dict[str, Any]:
        """Delete local preview media without touching the commerce record."""

        with self._lock:
            purged = self.media.purge_session(session_id, reason="SHOPPER_DELETED")
            discarded = self._discard_local_imagery(session_id, reason="SHOPPER_DELETED")
            self._sync()
            return {
                "purged": len(purged),
                "localDeleted": sum(1 for item in purged if item.get("localStatus") == "DELETED"),
                "pendingRetry": sum(
                    1 for item in purged if item.get("localStatus") == "PENDING_RETRY"
                ),
                **discarded,
            }

    def _discard_local_imagery(self, session_id: str, *, reason: str) -> dict[str, Any]:
        """Delete the source photo and cached result bytes, and leave a tombstone.

        Centralised here so an HTTP action and a direct caller behave identically. The tombstone is
        kept apart from the media bytes: it records that imagery was deleted for this session, so a
        later request cannot silently refetch provider results from a stored URL.
        """
        cleared = self.forget_result_images(session_id)
        photo_deleted = False
        try:
            photo_deleted = self.photo.has(session_id)
            self.photo.delete(session_id)
        except OSError:
            photo_deleted = False
        tombstone = {
            "reason": reason,
            "at": self._clock().isoformat(),
            "cachedCleared": cleared,
            "photoDeleted": photo_deleted,
        }
        self._result_tombstones[session_id] = tombstone
        # The store is authoritative: an in-memory dictionary would forget this across a restart.
        self.store.record_image_tombstone(session_id, reason, self._clock(),
                                          detail=f"cachedCleared={cleared};photoDeleted={photo_deleted}")
        return {"photoDeleted": photo_deleted, "cachedCleared": cleared}

    # -- candidates and previews ----------------------------------------- #
    def set_preferences(
        self,
        session_id: str,
        *,
        size: str,
        preference_field: str | None = None,
        preference_value: str | None = None,
    ) -> None:
        with self._lock:
            case = self._case(session_id)
            session = self._session(session_id)
            if session.state != "CANDIDATES_READY":
                raise ServiceError(
                    "PREFERENCES_LOCKED",
                    "size and preferences can only change before previews start",
                    status=409,
                )
            size_value = (size or "").strip()[:12]
            if not size_value:
                raise ServiceError("INVALID_SIZE", "a size is required", status=400)
            preferences = dict(case.preferences)
            if preference_field:
                if preference_field not in ("colorFamily", "style", "occasion"):
                    raise ServiceError("INVALID_PREFERENCE", "unknown preference field", status=400)
                preferred = dict(preferences.get("preferred", {}))
                if preference_value:
                    preferred[preference_field] = [preference_value[:40]]
                else:
                    preferred.pop(preference_field, None)
                preferences["preferred"] = preferred
            self.store.update_case(
                session_id, now=self._clock(), size=size_value, preferences=preferences
            )

    def start_sample_return(self, collection: str, sku: str) -> str:
        """Create the sample return for one catalogue garment, server-side.

        The payload is built from the bundled table, so a browser can choose a collection and a
        SKU and nothing else: no path, no URL and no category travels from the client.
        """
        import json as _json

        from domain.samples import scenario_payload
        from ui.uploads import parse_upload

        if collection not in ("women", "men"):
            raise ServiceError("UNKNOWN_COLLECTION", "that collection is not bundled", status=404)
        try:
            payload = scenario_payload(collection, sku)
        except KeyError:
            raise ServiceError("UNKNOWN_GARMENT", "that garment is not in this collection", status=404)
        parsed = parse_upload(_json.dumps(payload).encode(), content_type="application/json")
        session_id = self.create_return(
            reason_codes=list(parsed.reason_codes), immediate_refund=False, notes="",
            scenario=ScenarioInput(order=parsed.order, rows=parsed.rows,
                                   reason_codes=parsed.reason_codes, upload_handle=None,
                                   sha256=None, preferences=parsed.preferences))
        # The catalogue click is this session's garment. Nothing later asks again, and nothing may
        # quietly substitute a different garment for it.
        self._chosen()[session_id] = sku
        return session_id

    def _chosen(self) -> dict:
        if not hasattr(self, "_chosen_garments"):
            self._chosen_garments = {}
        return self._chosen_garments

    def selected_garment(self, session_id: str) -> str | None:
        """The catalogue garment bound to this session, if the shopper has chosen one."""
        return self._chosen().get(session_id)

    def choose_garment(self, session_id: str, sku: str) -> None:
        """Bind a garment to this session after re-checking it is genuinely on offer."""
        with self._lock:
            session = self._session(session_id)
            if session.state != "CANDIDATES_READY":
                raise ServiceError("WRONG_STATE",
                                   "a garment can be chosen once consent is given", status=409)
            eligible = tuple(self.selection_for(self._case(session_id), session).candidate_skus)
            if sku not in eligible:
                raise ServiceError(
                    "CANDIDATE_NOT_AVAILABLE",
                    "that garment is not in stock in this size, or is not eligible for this "
                    "return; choose another", status=409)
            if session.photo_version and f"{session.photo_version}:{sku}" in session.generated:
                raise ServiceError("ALREADY_GENERATED",
                                   "this photo already produced a preview for that garment; "
                                   "choose another", status=409)
            self._chosen()[session_id] = sku
            self._sync()

    def clear_garment(self, session_id: str) -> None:
        """Re-open garment selection on purpose (Change garment / Try something else on)."""
        with self._lock:
            self._chosen().pop(session_id, None)
            self._sync()

    def start_previews(self, session_id: str, *, sku: str | None = None) -> CaseView:
        with self._lock:
            case = self._case(session_id)
            session = self._session(session_id)
            if session.state != "CANDIDATES_READY":
                raise ServiceError("WRONG_STATE", f"cannot start previews from {session.state}", status=409)
            selection = self.selection_for(case, session)
            if sku is None:
                sku = self.selected_garment(session_id)
            if sku is None and len(selection.candidate_skus) == 1:
                # One eligible garment needs no explicit choice; several keep the previous
                # behaviour of planning the whole eligible set (the catalogue click binds one).
                sku = selection.candidate_skus[0]
            if sku is not None:
                if sku not in selection.candidate_skus:
                    raise ServiceError("CANDIDATE_NOT_AVAILABLE",
                                       "that alternative is not on offer for this return", status=409)
                # The shopper's explicit choice governs what is sent: one candidate, one task.
                selection = replace(selection,
                                    candidates=tuple(c for c in selection.candidates if c.sku == sku))
            session_now = self._session(session_id)
            if not selection.has_candidates:
                result = self.sim.report_no_candidates(session_id, selection)
                if not result.accepted:
                    raise ServiceError(result.refusal or "REFUSED", "no-candidate report refused", status=409)
                self._sync()
                return self.get_case(session_id)
            # Phase D: the plan follows the configured mode. A LIVE plan is priced against the
            # owner's unit cap and carries the authorization reference; anything missing means the
            # adapter was never configured for LIVE, and this stays a MOCK plan.
            live = self.preview_mode == "LIVE"
            settings = getattr(self.adapter, "_settings", None) if live else None
            if live and settings is None:
                raise ServiceError("LIVE_NOT_CONFIGURED",
                                   "live previews require configured credentials and caps", status=503)
            if live:
                # A live preview is bound to the exact photo: no photo, no plan, and a pair that
                # already produced a preview is never charged a second time.
                if not session_now.photo_version:
                    raise ServiceError("PHOTO_REQUIRED",
                                       "send a photo before generating previews", status=409)
                generated = session_now.generated
                tried = [c for c in selection.candidates
                         if f"{session_now.photo_version}:{c.sku}" in generated]
                if tried:
                    if sku is not None:
                        raise ServiceError(
                            "ALREADY_GENERATED",
                            "this photo and garment already produced a preview; choose another garment",
                            status=409,
                        )
                    # A multi-candidate plan never re-offers a garment this photo already produced.
                    selection = replace(selection,
                                        candidates=tuple(c for c in selection.candidates
                                                         if c not in tried))
                    if not selection.has_candidates:
                        raise ServiceError(
                            "ALREADY_GENERATED",
                            "every eligible garment has already been tried with this photo; "
                            "change the photo or continue the return",
                            status=409,
                        )
                if not self.photo.has(session_id):
                    raise ServiceError("PHOTO_REQUIRED",
                                       "send a photo before generating previews", status=409)
                # The remaining authorised allowance is checked before a plan exists, so an
                # exhausted session is refused rather than sent into a doomed paid attempt.
                ledger = getattr(self.adapter, "_ledger", None)
                if ledger is not None:
                    unit_cost = int(getattr(settings, "unit_cost", 0))
                    session_cap = int(getattr(settings, "session_unit_cap", 0)) or int(
                        getattr(settings, "unit_cap", 0))
                    # Both caps bind: the session's own allowance and the authorization's total.
                    remaining = min(
                        session_cap - ledger.committed_units(session_id=session_id),
                        int(getattr(settings, "unit_cap", 0) or session_cap)
                        - ledger.committed_units(),
                    )
                    needed = unit_cost * len(selection.candidates)
                    if needed > remaining:
                        raise ServiceError(
                            "ALLOWANCE_EXHAUSTED",
                            f"this authorization has {remaining} units left of {session_cap}; "
                            f"{len(selection.candidates)} garment(s) would need {needed}",
                            status=409,
                        )
                try:
                    self._prepare_live_media(session_id, case, selection)
                except AdapterError as exc:
                    # A missing or unusable reference is a refusal the shopper can act on, not a
                    # crash: surface it as a typed service error before anything is spent.
                    raise ServiceError(exc.code.value, str(exc), status=409) from exc
            result = self.sim.prepare_candidates(
                session_id, selection,
                budget_units=int(getattr(settings, "unit_cap", 0)) if live else 0,
                mode="LIVE" if live else "MOCK",
                authorization_ref=getattr(settings, "authorization_ref", None) if live else None,
                unit_cost=int(getattr(settings, "unit_cost", 0)) if live else 0,
            )
            if not result.accepted:
                raise ServiceError(result.refusal or "REFUSED", "preview plan refused", status=409)
            plan = self.sim.session(session_id).preview_plan
            if plan is None:  # pragma: no cover - reducer invariant
                raise ServiceError("NO_PLAN", "preview plan is missing", status=500)
            job = self.coordinator.start(plan, generation=case.consent_generation,
                                        photo_version=session_now.photo_version)
            self.store.save_job(session_id, job, self._clock())
            self._sync()
            return self.get_case(session_id)

    def now(self):
        """The service clock. Views compare result validity against it, never against wall time."""
        return self._clock()

    def submit_photo(self, session_id: str, payload: bytes, content_type: str) -> dict:
        """Accept the consenting shopper's own photo. Nothing is sent anywhere by this call."""
        with self._lock:
            case = self._case(session_id)
            latest = self.store.latest_consent(session_id)
            if not latest or latest["action"] != "GRANTED":
                raise ServiceError("CONSENT_REQUIRED", "photo intake requires granted consent", status=409)
            record = self.photo.submit(session_id, payload, content_type, now=self._clock())
            from hashlib import sha256

            applied = self.sim.record_photo(session_id, sha256(payload).hexdigest())
            if not applied.accepted:
                raise ServiceError(applied.refusal or "REFUSED",
                                   "the photo could not be bound to this session", status=409)
            self._sync()
            return record

    def try_another(self, session_id: str) -> CaseView:
        """Re-open the garment choice with the same photo and consent. Spends nothing here."""
        with self._lock:
            applied = self.sim.try_another(session_id)
            if not applied.accepted:
                raise ServiceError(applied.refusal or "REFUSED",
                                   "another garment cannot be chosen from this state", status=409)
            self.store.delete_job(session_id)
            self._sync()
            return self.get_case(session_id)

    def change_photo(self, session_id: str) -> CaseView:
        """Discard the photo, its grant and anything produced from it, then ask for a new photo."""
        with self._lock:
            applied = self.sim.change_photo(session_id)
            if not applied.accepted:
                raise ServiceError(applied.refusal or "REFUSED",
                                   "the photo cannot be changed from this state", status=409)
            self.store.delete_job(session_id)
            self.photo.delete(session_id)
            self.store.record_consent(session_id, "WITHDRAWN", None, self._clock(),
                                      detail="shopper changed photo")
            # Clear what was produced from the previous photo, but leave no session tombstone:
            # a tombstone would also refuse the imagery of the *next* photo in this same session.
            try:
                self.forget_result_images(session_id)
            except Exception:  # local imagery is best-effort; the binding is the authority
                pass
            try:
                self.adapter.discard_session_media(session_id)
            except Exception:
                pass
            self._sync()
            return self.get_case(session_id)

    def _live_consent(self, session_id: str, case) -> "ConsentGrant":
        """Build the grant from what was actually recorded, not from convenient defaults.

        The version and grant time come from the consent ledger; expiry is that grant time plus the
        documented consent window; revocation is the recorded revocation, if any.
        """
        from datetime import timedelta

        from adapters.youcam_live import ConsentGrant

        latest = self.store.latest_consent(session_id) or {}
        action = str(latest.get("action") or "")
        granted_at = self._parse_stamp(latest.get("recorded_at")) or self._clock()
        now = self._clock()
        revoked = None
        if action == "REVOKED":
            revoked = self._parse_stamp(latest.get("recorded_at")) or now
        elif action != "GRANTED":
            raise AdapterError(FailureCode.CONSENT_REQUIRED, "no granted consent is on record")
        return ConsentGrant(session_id=session_id, generation=case.consent_generation,
                            version=str(latest.get("consent_version") or ""),
                            granted_at=granted_at,
                            expires_at=granted_at + CONSENT_WINDOW, revoked_at=revoked)

    @staticmethod
    def _parse_stamp(value: object):
        from datetime import datetime

        if not isinstance(value, str) or not value:
            return None
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp.tzinfo is None:
            from datetime import timezone

            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp

    def _provider_category(self, case, sku: str) -> str:
        """The verified provider category for this garment, or a refusal.

        The browser never supplies this. A bundled catalog SKU uses its own recorded mapping; any
        other SKU uses the recorded catalogue row's family; anything else is refused before a unit
        is reserved or an image is transmitted.
        """
        from domain.samples import FAMILY_PROVIDER_CATEGORY, provider_category_for

        verified = provider_category_for(sku)
        if verified:
            return verified
        for row in self._rows(case):
            if str(row.get("sku") or "") == sku:
                family = str(row.get("category") or "").strip().lower()
                mapped = FAMILY_PROVIDER_CATEGORY.get(family)
                if mapped:
                    return mapped
                break
        raise ServiceError(
            "CATEGORY_UNMAPPED",
            f"no verified provider category for {sku}; nothing was uploaded or reserved",
            status=409,
        )

    def _prepare_live_media(self, session_id: str, case, selection) -> None:
        """Photo + per-candidate rights-cleared reference, validated, then handed to the adapter."""
        payload, content_type = self.photo.load(session_id)
        grant = self._live_consent(session_id, case)
        skus = (tuple(getattr(selection, "candidate_skus", ()) or ())
                or tuple(c.sku for c in getattr(selection, "candidates", ())))
        # Resolve every category first: one unmapped garment refuses the whole plan before any
        # upload, so a mixed run can never partly transmit.
        categories = {sku: self._provider_category(case, sku) for sku in skus}
        for sku in skus:
            reference = self.references.load(sku)
            self.adapter.prepare_media(session_id=session_id, candidate_id=sku, payload=payload,
                                       content_type=content_type, consent=grant,
                                       reference_rights=reference["rights_ref"],
                                       reference_payload=reference["bytes"],
                                       reference_content_type=reference["content_type"],
                                       garment_category=categories[sku])

    def advance_previews(self, session_id: str) -> CaseView:
        """Poll the bounded mock tasks.  Safe to call on every page GET."""

        with self._lock:
            session = self._session(session_id)
            if session.state != "PREVIEW_PENDING":
                return self.get_case(session_id)
            case = self._case(session_id)
            job = self.store.load_job(session_id)
            if job is None:
                # The process restarted mid-preview: keep the shopper moving with
                # an honest failed preview rather than an invented image.
                now = self._clock()
                from datetime import timedelta

                result = PreviewResult(
                    session_id=session_id,
                    mode=self.preview_mode,
                    status="FAILED",
                    candidate_ids=session.candidate_skus(),
                    task_ids=tuple(f"interrupted-{index}" for index in range(len(session.candidate_skus()))),
                    created_at=now,
                    expires_at=now + timedelta(seconds=60),
                    failure_code="INTERRUPTED",
                )
                self.sim.receive_preview_result(session_id, result)
                self._sync()
                return self.get_case(session_id)

            latest = self.store.latest_consent(session_id)
            consent_live = bool(latest and latest["action"] == "GRANTED")
            generation_valid = (
                consent_live and int(job.get("generation", -1)) == case.consent_generation
            )
            # A restarted process has no media handles; hand it the stored grant so a task it
            # adopts can still be polled and delivered. A missing or revoked grant is a refusal.
            adopter = getattr(self.coordinator.adapter, "adopt_consent", None)
            if callable(adopter) and generation_valid:
                try:
                    adopter(session_id, self._live_consent(session_id, case))
                except AdapterError:
                    pass
            outcome = self.coordinator.tick(job, generation_valid=generation_valid)
            if outcome.status == "DISCARDED":
                self.store.delete_job(session_id)
                self._sync()
                return self.get_case(session_id)
            if outcome.status == "PENDING":
                self.store.save_job(session_id, job, self._clock())
                return self.get_case(session_id)

            result = outcome.result
            assert result is not None
            bound = getattr(result, "photo_version", None) or job.get("photoVersion")
            current = self._session(session_id).photo_version
            if bound and current and bound != current:
                # The shopper changed photo while this preview was in flight. The result belongs
                # to the previous photo and must never be attached to the new one.
                self.store.delete_job(session_id)
                self._sync()
                return self.get_case(session_id)
            if bound and getattr(result, "photo_version", None) is None:
                from dataclasses import replace as _replace

                result = _replace(result, photo_version=bound)
            applied = self.sim.receive_preview_result(session_id, result)
            if applied.accepted and result.mode != "LIVE":
                # Offline placeholders only. In LIVE the result carries provider-returned media
                # URLs and nothing is drawn locally, so a failed live preview can never look real.
                for candidate_id, handle in zip(result.candidate_ids, result.media_handles):
                    if handle is None:
                        continue
                    self.media.create_placeholder(
                        session_id=session_id,
                        candidate_id=candidate_id,
                        sku=candidate_id,
                    )
            if applied.accepted:
                self.store.delete_job(session_id)
            # A refused result keeps its job: the work is not silently thrown away, and the next
            # page load can retry delivery instead of pretending nothing happened.
            self._sync()
            return self.get_case(session_id)

    def result_image(self, session_id: str, candidate_id: str) -> tuple[bytes, str]:
        """Bytes for one live result, fetched through the validated provider transport.

        Serving them from this origin keeps ``img-src 'self'`` intact: the browser never talks to
        the provider host, and a stored result URL is never rendered as an external image.
        """
        from datetime import timedelta

        with self._lock:
            session = self._session(session_id)
            result = session.preview_result
            if result is None or result.mode != "LIVE":
                raise ServiceError("NO_LIVE_RESULT", "no live preview is available", status=404)
            tombstone = self.store.get_image_tombstone(session_id) or self._result_tombstones.get(session_id)
            if tombstone is not None:
                raise ServiceError(
                    "IMAGERY_DELETED",
                    f"local imagery for this session was deleted ({tombstone['reason']})",
                    status=410,
                )
            # Consent must be current *now* — granted, unexpired, unrevoked — before any bytes are
            # returned from cache or fetched again.
            grant = self._live_consent(session_id, self._case(session_id))
            try:
                grant.assert_live(self._clock())
            except AdapterError as exc:
                raise ServiceError("CONSENT_REQUIRED", str(exc), status=409) from exc
            if self._clock() >= result.expires_at:
                raise ServiceError("RESULT_EXPIRED", "this preview result is past its validity", status=410)
            key = (session_id, candidate_id)
            cached = self._result_cache.get(key)
            if cached is not None:
                return cached
            try:
                index = result.candidate_ids.index(candidate_id)
            except ValueError as exc:
                raise ServiceError("CANDIDATE_NOT_AVAILABLE", "unknown candidate", status=404) from exc
            url = result.media_handles[index] if index < len(result.media_handles) else None
            if not url:
                raise ServiceError("NO_IMAGE", "this candidate has no image", status=404)
            try:
                payload = self.adapter.fetch_result_image(
                    url, deadline=self._clock() + timedelta(minutes=2))
            except AdapterError as exc:
                raise ServiceError("RESULT_UNAVAILABLE", str(exc), status=502) from exc
            self._result_cache[key] = payload
            return payload

    def forget_result_images(self, session_id: str) -> int:
        keys = [key for key in self._result_cache if key[0] == session_id]
        for key in keys:
            self._result_cache.pop(key, None)
        return len(keys)

    # -- quote / approval / receipt -------------------------------------- #
    def choose(self, session_id: str, candidate_sku: str) -> Quote:
        with self._lock:
            session = self._session(session_id)
            if session.state != "DECISION_READY":
                raise ServiceError("WRONG_STATE", f"cannot choose from {session.state}", status=409)
            if candidate_sku not in session.candidate_skus():
                raise ServiceError(
                    "CANDIDATE_NOT_AVAILABLE",
                    "that alternative is not one of this session's candidates",
                    status=409,
                )
            result = self.sim.choose(session_id, candidate_sku)
            if not result.accepted:
                raise ServiceError(result.refusal or "REFUSED", "candidate selection refused", status=409)
            quote = self.sim.session(session_id).quote
            self._sync()
            if quote is None:  # pragma: no cover - reducer invariant
                raise ServiceError("NO_QUOTE", "quote was not created", status=500)
            return quote

    def requote(self, session_id: str) -> Quote:
        with self._lock:
            try:
                quote = self.sim.reissue_quote(session_id)
            except StoreError as exc:
                raise ServiceError("REQUOTE_REFUSED", str(exc), status=409) from None
            self._sync()
            return quote

    def approve(self, session_id: str, *, nonce: str) -> ConfirmResult:
        from commerce.confirmation import ACTION_APPROVE_EXCHANGE, TRUSTED_ORIGIN, ConfirmRequest

        with self._lock:
            case = self._case(session_id)
            session = self._session(session_id)
            from ui.security import constant_time_eq

            if not constant_time_eq(nonce, case.approval_nonce):
                raise ServiceError(
                    "APPROVAL_NONCE_INVALID",
                    "this approval form is not the one issued for the session",
                    status=403,
                )
            if session.state != "QUOTE_READY" or session.quote is None:
                raise ServiceError("WRONG_STATE", f"cannot approve from {session.state}", status=409)
            quote = session.quote
            key_payload = f"{case.approval_nonce}\x1f{quote.quote_id}\x1f{quote.quote_version}"
            idempotency_key = "idem_" + hashlib.sha256(key_payload.encode()).hexdigest()[:24]
            request = ConfirmRequest(
                session_id=session_id,
                shopper_identity=case.shopper_identity,
                request_token=case.request_token,
                quote_id=quote.quote_id,
                quote_version=quote.quote_version,
                candidate_sku=quote.candidate_variant_id,
                action=ACTION_APPROVE_EXCHANGE,
                origin=TRUSTED_ORIGIN,
                approved_at=self._clock(),
                idempotency_key=idempotency_key,
            )
            result = self.sim.confirm_exchange(request)
            self._sync()
            return result

    def refund(self, session_id: str, *, reason: str = "CONTINUE_REFUND") -> RefundResult:
        with self._lock:
            case = self._case(session_id)
            result = self.sim.request_refund(
                session_id,
                shopper_identity=case.shopper_identity,
                request_token=case.request_token,
                reason=reason,
            )
            self._sync()
            return result

    def require_refund_route(self, session_id: str | None) -> None:
        """The refund route never depends on a session existing."""

        return None

    # -- diagnostics ----------------------------------------------------- #
    def ledger_totals(self) -> dict[str, Any]:
        totals = self.store.ledger_totals()
        totals["metrics"] = self.sim.metrics()
        return totals


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #
def _fixture_rows() -> tuple[Mapping[str, Any], ...]:
    from adapters.mock.fixtures import load_catalog_fixture

    # The spec loader returns read-only mapping views; the case record stores a
    # plain JSON document, so thaw one level (every row value is scalar).
    return tuple(dict(row) for row in load_catalog_fixture().rows)


def _fixture_order() -> OrderView:
    from adapters.mock.fixtures import load_catalog_fixture

    return load_catalog_fixture().order


def _comparable_row(item: Item) -> dict[str, Any]:
    row = item.as_dict()
    return {
        key: row[key]
        for key in (
            "sku",
            "merchant",
            "category",
            "size",
            "currency",
            "priceMinor",
            "stock",
            "colorFamily",
            "referenceRights",
        )
    } | ({"style": item.style} if item.style is not None else {}) | (
        {"occasion": item.occasion} if item.occasion is not None else {}
    )


__all__ = [
    "CONSENT_VERSION",
    "CaseView",
    "ReturnService",
    "ScenarioInput",
    "ServiceError",
]
