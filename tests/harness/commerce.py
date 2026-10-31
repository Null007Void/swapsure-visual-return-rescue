"""Commerce scenario helpers: drive the simulator to a confirmable quote."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from adapters.mock.fixtures import CatalogFixture
from commerce.inventory import Inventory, StockRow
from commerce.quote import Quote
from commerce.simulator import CommerceSimulator, ConfirmRequest, ConfirmResult
from domain.catalog import CandidateSelection, select_candidates
from domain.eligibility import decide
from domain.preview import PreviewPlan, PreviewResult
from harness.clock import ManualClock
from harness.scenarios import CONSENT, SHOPPER, TOKEN, confirm_request


def simulator_for(
    catalog: CatalogFixture,
    clock: ManualClock,
    *,
    store_path=None,
    ledger_sink=None,
) -> CommerceSimulator:
    """A commerce store seeded from the synthetic catalog fixture."""

    inventory = Inventory(
        StockRow(
            sku=str(row["sku"]),
            merchant=str(row["merchant"]),
            category=str(row["category"]),
            size=str(row["size"]),
            currency=str(row["currency"]),
            price_minor=int(row["priceMinor"]),
            stock=int(row["stock"]),
            color_family=str(row["colorFamily"]),
            reference_rights=str(row.get("referenceRights", "")),
            style=row.get("style"),
            occasion=row.get("occasion"),
        )
        for row in catalog.rows
    )
    return CommerceSimulator(
        inventory,
        clock=clock,
        store_path=store_path,
        ledger_sink=ledger_sink,
    )


@dataclass(frozen=True, slots=True)
class RescueFlow:
    sim: CommerceSimulator
    session_id: str
    selection: CandidateSelection
    sku: str
    preview: PreviewResult
    clock: ManualClock
    quote_ref: Quote

    @property
    def session(self):
        return self.sim.session(self.session_id)

    @property
    def quote(self) -> Quote:
        quote = self.session.quote
        assert quote is not None, f"session is in {self.session.state} without a quote"
        return quote

    def request(self, *, quote: Quote | None = None, **overrides) -> ConfirmRequest:
        """A client-side confirmation of a quote it was shown.

        By default this is the quote captured when the offer was made, exactly as a
        browser would hold it; after a reselection the caller passes the new quote.
        """

        return confirm_request(self.session, quote or self.quote_ref, **overrides)


@dataclass(frozen=True, slots=True)
class PartialFlow:
    """A session stopped before a quote exists (DECISION_READY or earlier)."""

    sim: CommerceSimulator
    session_id: str
    selection: CandidateSelection
    preview: PreviewResult | None
    clock: ManualClock

    @property
    def session(self):
        return self.sim.session(self.session_id)


def walk(
    catalog: CatalogFixture,
    clock: ManualClock,
    *,
    stop: str = "QUOTE_READY",
    reason_codes: tuple[str, ...] = ("COLOR",),
    candidate_sku: str | None = None,
    store_path=None,
    ledger_sink=None,
) -> RescueFlow | PartialFlow:
    """Drive a real session through the simulator's public API."""

    sim = simulator_for(catalog, clock, store_path=store_path, ledger_sink=ledger_sink)
    session = sim.open_session(catalog.order, shopper_identity=SHOPPER, request_token=TOKEN)
    decision = decide(session.session_id, "1.0", list(reason_codes), False)
    assert sim.evaluate(session.session_id, decision).accepted
    assert sim.opt_in(session.session_id).accepted
    assert sim.record_consent(session.session_id, CONSENT).accepted

    selection = select_candidates(
        catalog.query(session_id=session.session_id, reason_codes=reason_codes), now=clock()
    )
    assert selection.has_candidates, "fixture flow needs at least one candidate"
    assert sim.prepare_candidates(session.session_id, selection).accepted

    # A successful MOCK preview outcome, built directly: this harness is about the
    # commerce contract, so it does not need the provider adapter to be importable.
    plan = PreviewPlan(
        session_id=session.session_id,
        consent_version=CONSENT,
        candidate_ids=selection.candidate_skus,
        provider_version="mock-v1",
        estimated_units=0,
        authorization_ref=None,
        deadline=clock() + timedelta(seconds=60),
    )
    preview = PreviewResult(
        session_id=session.session_id,
        mode=plan.mode,
        status="SUCCEEDED",
        candidate_ids=selection.candidate_skus,
        task_ids=tuple(f"task-{index}" for index in range(len(selection.candidate_skus))),
        created_at=clock(),
        expires_at=clock() + timedelta(seconds=60),
        media_handles=tuple(
            f"media-{index}" for index in range(len(selection.candidate_skus))
        ),
    )
    assert sim.receive_preview_result(session.session_id, preview).accepted

    if stop == "DECISION_READY":
        return PartialFlow(
            sim=sim,
            session_id=session.session_id,
            selection=selection,
            preview=preview,
            clock=clock,
        )

    sku = candidate_sku or selection.candidate_skus[0]
    assert sim.choose(session.session_id, sku).accepted
    offered = sim.session(session.session_id).quote
    assert offered is not None
    return RescueFlow(
        sim=sim,
        session_id=session.session_id,
        selection=selection,
        sku=sku,
        preview=preview,
        clock=clock,
        quote_ref=offered,
    )


def start_flow(catalog: CatalogFixture, clock: ManualClock, **kwargs) -> RescueFlow:
    flow = walk(catalog, clock, stop="QUOTE_READY", **kwargs)
    assert isinstance(flow, RescueFlow)
    return flow


def decision_ready_flow(catalog: CatalogFixture, clock: ManualClock, **kwargs) -> PartialFlow:
    flow = walk(catalog, clock, stop="DECISION_READY", **kwargs)
    assert isinstance(flow, PartialFlow)
    return flow


def confirm(flow: RescueFlow, **overrides) -> ConfirmResult:
    return flow.sim.confirm_exchange(flow.request(**overrides))


def committed_flow(catalog, clock, **kwargs) -> tuple[RescueFlow, ConfirmResult]:
    flow = start_flow(catalog, clock, **kwargs)
    result = confirm(flow)
    assert result.outcome == "COMMITTED", result
    return flow, result
