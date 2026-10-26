"""Native orchestration tests use injected transports, never real accounts."""
import asyncio
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest

from evplatform.core.bus import EventBus, SchemaRegistry
from evplatform.core.config import RiskConfig
from evplatform.core.conflicts import reviewed_correlations, risk_family
from evplatform.core.metrics import Metrics
from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.native_accounting import NativeCumulativeSnapshot, NativeFill, NativeInstrument, NativeOrderIntent, NativePriceBand
from evplatform.core.native_oms import NativeOrder, NativeOrderManager, NativeSettlement
from evplatform.core.pricing import fair_value, opportunity
from evplatform.core.risk import RiskEngine, RiskRejected
from evplatform.core.storage import Storage
from evplatform.venues.reconciliation import NativeCancelReport, NativeOrderReport


def order(market, *, venue="novig", outcome="home", identifier=None):
    identifier = identifier or str(uuid4())
    instrument = NativeInstrument(venue=venue, instrument_id="native-market", canonical_market_id=market.id,
        outcome=outcome, native_outcome="native-"+outcome, quantity_unit="whole_cent_contracts",
        price_format="probability", payout_per_unit_usd=".01", quantity_step="1", minimum_quantity="1",
        cash_quantum_usd=".00001", price_bands=(NativePriceBand(minimum=".005", maximum=".995", step=".005"),),
        settlement_rules_fingerprint=market.identity(), verified=True, source="OFFLINE reviewed fixture")
    intent = NativeOrderIntent(id=identifier, client_order_id="ev-"+identifier, instrument=instrument,
        quantity="100", limit_price=".45", max_fee_usd=".02", fee_source="OFFLINE fee fixture",
        fees_verified=True, mode="sandbox")
    return NativeOrder(intent=intent, candidate_id="test-candidate", strategy="native_positive_ev",
        account_fingerprint="a"*64, market_snapshot=market, market_fingerprint=market.identity(),
        risk_family_id=risk_family(market), reviewed_correlation_groups=reviewed_correlations(market))


async def test_kalshi_shard_three_persists_with_unknown_intent_across_restart(system, market):
    manager, _, storage, _ = system
    base = order(market)
    instrument = base.intent.instrument.model_copy(update={
        "venue": "kalshi", "instrument_id": "OFFLINE-NBA-TICKER", "native_outcome": "yes",
        "exchange_index": 3, "quantity_unit": "contracts", "payout_per_unit_usd": D(1),
        "quantity_step": D(".01"), "minimum_quantity": D(".01"), "cash_quantum_usd": D(".0001"),
        "price_bands": (NativePriceBand(minimum=".0001", maximum=".9999", step=".0001"),)})
    intent = base.intent.model_copy(update={"instrument": instrument, "quantity": D(1),
                                     "mode": "production"})
    uncertain = NativeOrder.model_validate({**base.model_dump(mode="json"),
        "intent": intent.model_dump(mode="json"), "status": "submitting"})
    await manager._save(uncertain)
    recovered_risk = RiskEngine(RiskConfig(live_max_order_stake=D("1")))
    recovered = NativeOrderManager(recovered_risk, storage, manager.bus, Metrics(), lock=asyncio.Lock(),
        revalidate=lambda o: None, preflight=lambda o: None)
    await recovered.start()
    saved = recovered_risk.native_orders[uncertain.id]
    assert saved.intent.instrument.exchange_index == 3
    assert saved.intent.instrument.instrument_id == "OFFLINE-NBA-TICKER"
    assert saved.account_fingerprint == uncertain.account_fingerprint
    assert saved.status == OrderStatus.UNKNOWN and recovered_risk.kill_switch
    assert not recovered_risk.auto_trade


class Adapter:
    def __init__(self, risk):
        self.risk, self.submissions, self.cancellations = risk, [], []
        self.bound_account_fingerprint = "a" * 64
        self.report = None
        self.fail = False
        self.bound_account_fingerprint = "a"*64

    async def submit_intent(self, intent, authorization):
        assert self.risk.authority.consume(authorization, intent.id)
        self.submissions.append(intent)
        if self.fail: raise TimeoutError("OFFLINE unknown response")
        return {"native_order_id": "native-"+intent.id}

    async def cancel_native(self, native_id, intent):
        self.cancellations.append(native_id)
        return NativeCancelReport(native_order_id=native_id, client_order_id=intent.client_order_id,
                                  matching_stopped=True)

    async def reconcile_native(self, native_id, intent):
        return self.report


@pytest.fixture
async def system(tmp_path):
    storage = Storage(str(tmp_path / "native.db"))
    await storage.open()
    risk = RiskEngine(RiskConfig(live_max_order_stake=D("1")))
    risk.auto_trade = True
    risk.native_authorization_expires_at = utcnow() + timedelta(minutes=5)
    schemas = SchemaRegistry()
    schemas.register("native.order", 1, NativeOrder)
    schemas.register("native.settlement", 1, NativeSettlement)
    bus = EventBus(schemas, risk.halt)
    manager = NativeOrderManager(risk, storage, bus, Metrics(), lock=asyncio.Lock(),
        revalidate=lambda o: None, preflight=lambda o: None)
    await manager.start()
    adapter = Adapter(risk)
    manager.register("novig", adapter)
    yield manager, risk, storage, adapter
    await storage.close()


def report(order, *, quantity="40", cost=".18", fees=".001", status="partial", terminal=False, observed_at=None):
    qty = D(quantity)
    snapshot = NativeCumulativeSnapshot(order_id=order.venue_order_id, client_order_id=order.client_order_id,
        cumulative_quantity=qty, cumulative_cost_usd=cost, cumulative_gross_payout_usd=qty*D(".01"),
        cumulative_fee_usd=fees, observed_at=observed_at or utcnow(), status=status, authoritative=True,
        source="OFFLINE authoritative fixture")
    return NativeOrderReport(native_order_id=order.venue_order_id, client_order_id=order.client_order_id,
        status=status, remaining_quantity=0 if terminal else 100-qty, terminal=terminal,
        financial_complete=True, snapshot=snapshot)


def test_native_fill_retains_bounded_exchange_clock_skew_but_rejects_large_future(market):
    placed = order(market).model_copy(update={"venue_order_id": "native-order"})
    observed = utcnow() - timedelta(milliseconds=500)
    fill = NativeFill(fill_id="exchange-fill", order_id="native-order",
        client_order_id=placed.client_order_id, quantity="100", price=".45",
        cost_usd=".45", gross_payout_usd="1", fee_usd="0",
        filled_at=observed+timedelta(milliseconds=150), observed_at=observed,
        source="novig.rest.fill")
    stager = type("Stager", (), {"_fill_evidence": {}})()
    staged, changed = NativeOrderManager._stage_fills(stager, placed, [fill])
    assert list(staged) == ["exchange-fill"] and changed == [fill]
    with pytest.raises(ValueError, match="Future native execution evidence"):
        NativeOrderManager._stage_fills(stager, placed,
            [fill.model_copy(update={"filled_at": observed+timedelta(seconds=2)})])


async def test_durable_intent_precedes_submission_ack_is_not_fill(system, market):
    manager, risk, storage, adapter = system
    original_submit = adapter.submit_intent
    async def checked_submit(intent, authorization):
        row = await (await storage.connection.execute("SELECT status FROM native_orders WHERE id=?", (intent.id,))).fetchone()
        assert row == ("submitting",)
        return await original_submit(intent, authorization)
    adapter.submit_intent = checked_submit
    result = await manager.execute(order(market))
    assert result.status == OrderStatus.OPEN
    assert result.filled_quantity == result.fees == 0
    assert not result.reconciliation_complete
    assert risk.exposures() == D(".47")


async def test_manual_permit_routes_only_one_reviewed_native_intent_while_automation_stays_off(system, market):
    manager, risk, storage, adapter = system
    risk.auto_trade = False
    approved = order(market).model_copy(update={"execution_origin": "manual"})
    other = order(market).model_copy(update={"execution_origin": "manual"})
    with pytest.raises(RiskRejected, match="disabled"):
        await manager.execute(approved)
    assert adapter.submissions == []
    with risk.manual_order(approved.id):
        with pytest.raises(RiskRejected, match="disabled"):
            await manager.execute(other)
        placed = await manager.execute(approved)
        assert placed.execution_origin == "manual"
    assert not risk.auto_trade
    assert len(adapter.submissions) == 1
    row = await (await storage.connection.execute("SELECT data FROM native_orders WHERE id=?", (approved.id,))).fetchone()
    assert NativeOrder.model_validate_json(row[0]).execution_origin == "manual"
    with pytest.raises(RiskRejected):
        await manager.execute(approved)


async def test_one_native_attempt_cap_survives_restart_and_unknown_outcome(system, market):
    manager, risk, storage, adapter = system
    risk.config = risk.config.model_copy(update={"max_native_order_attempts": 1})
    adapter.fail = True
    first = await manager.execute(order(market))
    assert first.status == OrderStatus.UNKNOWN
    assert len(adapter.submissions) == 1
    risk.kill_switch = False
    risk.auto_trade = True
    risk.native_authorization_expires_at = utcnow() + timedelta(minutes=5)
    with pytest.raises(RiskRejected, match="Native order attempt limit reached"):
        await manager.execute(order(market))
    risk.native_orders.clear()
    await manager.start()
    risk.kill_switch = False
    risk.auto_trade = True
    risk.native_authorization_expires_at = utcnow() + timedelta(minutes=5)
    with pytest.raises(RiskRejected, match="Native order attempt limit reached"):
        await manager.execute(order(market))
    assert len(adapter.submissions) == 1


async def test_recurring_daily_attempt_cap_counts_unknown_intent_after_restart(system, market):
    manager, risk, _, adapter = system
    risk.config = risk.config.model_copy(update={
        "max_native_order_attempts": None, "max_native_daily_attempts": 1,
        "max_native_daily_debit": D("5")})
    adapter.fail = True
    first = await manager.execute(order(market))
    assert first.status == OrderStatus.UNKNOWN
    assert len(adapter.submissions) == 1
    risk.native_orders.clear()
    await manager.start()
    risk.kill_switch = False
    risk.auto_trade = True
    risk.native_authorization_expires_at = utcnow() + timedelta(minutes=5)
    with pytest.raises(RiskRejected, match="Daily native order attempt limit reached"):
        await manager.execute(order(market))
    assert len(adapter.submissions) == 1


async def test_recurring_daily_debit_cap_uses_worst_case_intents(system, market):
    manager, risk, _, adapter = system
    risk.config = risk.config.model_copy(update={
        "max_native_order_attempts": None, "max_native_daily_attempts": 3,
        "max_native_daily_debit": D(".90")})
    first = await manager.execute(order(market))
    assert first.reserved_cost == D(".47")
    with pytest.raises(RiskRejected, match="Daily native upfront debit limit reached"):
        await manager.execute(order(market))
    assert len(adapter.submissions) == 1


async def test_external_position_blocks_same_sport_without_becoming_oms_order(system, market):
    from types import SimpleNamespace
    manager, risk, _, adapter = system
    risk.external_positions = (SimpleNamespace(venue="novig", environment="production",
        sport=market.sport, cost_usd=D(".97175")),)
    with pytest.raises(RiskRejected, match="external native position blocks this sport"):
        await manager.execute(order(market))
    assert not adapter.submissions and not risk.native_orders
    assert risk.exposures() == D(".97175")


async def test_unknown_transmission_never_retried_and_survives_restart(system, market):
    manager, risk, storage, adapter = system
    adapter.fail = True
    intent = order(market)
    result = await manager.execute(intent)
    assert result.status == OrderStatus.UNKNOWN and risk.kill_switch
    with pytest.raises(RiskRejected): await manager.execute(intent)
    assert len(adapter.submissions) == 1
    risk.native_orders.clear()
    await manager.start()
    assert risk.native_orders[result.id].status == OrderStatus.UNKNOWN
    assert risk.exposures() == D(".47")


async def test_opposing_position_blocked_across_native_orders(system, market):
    manager, risk, storage, adapter = system
    await manager.execute(order(market))
    with pytest.raises(RiskRejected, match="Opposing"):
        await manager.execute(order(market, outcome="away"))
    assert len(adapter.submissions) == 1


async def test_partial_cancel_late_fill_never_releases_filled_exposure(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = report(placed)
    partial = await manager.reconcile(placed.id)
    assert partial.filled_quantity == 40
    assert risk.exposures() == D(".47")
    cancelled = await manager.cancel(placed.id)
    assert cancelled.status == OrderStatus.CANCEL_PENDING and risk.exposures() == D(".47")
    adapter.report = report(placed, quantity="50", cost=".225", status="cancelled", terminal=True)
    final = await manager.reconcile(placed.id)
    assert final.filled_quantity == 50 and final.status == OrderStatus.CANCELLED
    assert risk.exposures() == D(".226")
    await manager.reconcile(placed.id)
    assert risk.exposures() == D(".226")
    with pytest.raises(RiskRejected): await manager.execute(order(market, outcome="away"))


async def test_incomplete_financial_evidence_cannot_free_reservation(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = NativeOrderReport(native_order_id=placed.venue_order_id, client_order_id=placed.client_order_id,
        status="cancelled", terminal=True, financial_complete=False)
    result = await manager.reconcile(placed.id)
    assert result.status == OrderStatus.UNKNOWN and risk.exposures() == D(".47")
    assert risk.kill_switch


async def test_changed_limits_after_durability_prevent_network_call(system, market):
    manager, risk, storage, adapter = system
    count = 0
    original_save = manager._save
    async def save_then_lower(item):
        nonlocal count
        result = await original_save(item)
        count += 1
        if count == 2: risk.config.live_max_order_stake = D(".10")
        return result
    manager._save = save_then_lower
    result = await manager.execute(order(market))
    assert result.status == OrderStatus.REJECTED and not adapter.submissions


async def test_fee_breach_halts_preserves_larger_reported_liability(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = report(placed, fees="2")
    result = await manager.reconcile(placed.id)
    assert result.status == OrderStatus.UNKNOWN and risk.kill_switch
    assert risk.exposures() == D("2.18")


async def test_settlement_requires_final_account_identity_and_is_idempotent(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = report(placed, quantity="100", cost=".45", status="filled", terminal=True)
    await manager.reconcile(placed.id)
    settlement = NativeSettlement(order_id=placed.id, venue="novig", account_fingerprint="a"*64,
        environment="sandbox", market_fingerprint=market.identity(), settlement_id="settle-1", payout_usd="1",
        settled_at=utcnow(), evidence="OFFLINE authenticated settlement fixture")
    await manager.settle(settlement)
    await manager.settle(settlement)
    assert risk.exposures() == 0 and risk.daily_realized == D(".549")
    with pytest.raises(ValueError):
        await manager.settle(settlement.model_copy(update={"payout_usd": D(0)}))


@pytest.mark.parametrize("payout,fee_refund,expected_pnl", [
    (".45", ".001", "0"),  # actual cash/fee refund on an exceptional grade
    (".225", "0", "-.226"),  # actual partial payout is not a fabricated total loss
])
async def test_exceptional_cash_settlement_uses_authoritative_payout_not_binary_loss(
        system, market, payout, fee_refund, expected_pnl):
    manager, risk, _, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = report(placed, quantity="100", cost=".45", fees=".001",
                            status="filled", terminal=True)
    await manager.reconcile(placed.id)
    result = NativeSettlement(order_id=placed.id, venue="novig", account_fingerprint="a"*64,
        environment="sandbox", market_fingerprint=market.identity(), settlement_id="offline-cash-grade",
        payout_usd=payout, fee_refund_usd=fee_refund, settled_at=utcnow(),
        evidence="OFFLINE authoritative cash-settlement fixture")
    await manager.settle(result)
    assert risk.daily_realized == D(expected_pnl)
    assert manager.realized_today() == D(expected_pnl)
    assert risk.exposures() == 0


async def test_stale_reconciliation_cannot_reopen_terminal_order(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = report(placed, quantity="100", cost=".45", status="filled", terminal=True)
    final = await manager.reconcile(placed.id)
    adapter.report = report(placed, observed_at=utcnow()-timedelta(seconds=2))
    assert await manager.reconcile(placed.id) == final


async def test_connection_loss_freezes_and_keeps_risk_budget(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    await manager.invalidate("novig")
    assert risk.native_orders[placed.id].status == OrderStatus.UNKNOWN
    assert risk.kill_switch and risk.exposures() == D(".47")


async def test_lower_revision_cannot_release_remainder_even_with_newer_receipt(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    first = report(placed)
    first = first.model_copy(update={"snapshot": first.snapshot.model_copy(update={"revision": 10})})
    adapter.report = first
    partial = await manager.reconcile(placed.id)
    stale = report(placed, quantity="0", cost="0", fees="0", status="cancelled", terminal=True)
    adapter.report = stale.model_copy(update={"snapshot": stale.snapshot.model_copy(update={"revision": 9})})
    current = await manager.reconcile(placed.id)
    assert current == partial
    assert current.last_snapshot.revision == 10
    assert risk.exposures() == D(".47")


async def test_settlement_invalidated_by_late_fee_stays_invalid_after_disk_recovery(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = report(placed, quantity="100", cost=".45", fees=".001", status="filled", terminal=True)
    await manager.reconcile(placed.id)
    settlement = NativeSettlement(order_id=placed.id, venue="novig", account_fingerprint="a"*64,
        environment="sandbox", market_fingerprint=market.identity(), settlement_id="settle-late-fee",
        payout_usd="1", settled_at=utcnow(), evidence="OFFLINE authenticated settlement fixture")
    await manager.settle(settlement)
    assert risk.exposures() == 0
    adapter.report = report(placed, quantity="100", cost=".45", fees=".002", status="filled", terminal=True)
    await manager.reconcile(placed.id)
    assert placed.id not in risk.settled
    restored_risk = RiskEngine(risk.config)
    recovered = NativeOrderManager(restored_risk, storage, manager.bus, Metrics(), lock=asyncio.Lock(),
        revalidate=lambda o: None, preflight=lambda o: None)
    await recovered.start()
    assert placed.id not in restored_risk.settled
    assert restored_risk.exposures() >= D(".452")
    assert restored_risk.daily_realized == 0


async def test_settlement_pnl_recovery_is_not_applied_twice_on_reinitialization(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = report(placed, quantity="100", cost=".45", status="filled", terminal=True)
    await manager.reconcile(placed.id)
    result = NativeSettlement(order_id=placed.id, venue="novig", account_fingerprint="a"*64,
        environment="sandbox", market_fingerprint=market.identity(), settlement_id="settle-restore",
        payout_usd="1", settled_at=utcnow(), evidence="OFFLINE authenticated settlement fixture")
    await manager.settle(result)
    initial = risk.daily_realized
    await manager.start()
    assert risk.daily_realized == initial


async def test_transient_cancel_retries_backoff_bounded_without_resubmission(system, market, monkeypatch):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    async def temporarily_matching(native_id, intent):
        adapter.cancellations.append(native_id)
        return NativeCancelReport(native_order_id=native_id, client_order_id=intent.client_order_id,
                                  retry_cancel_allowed=True, reason="OFFLINE order_is_matching fixture")
    adapter.cancel_native = temporarily_matching
    pending = await manager.cancel(placed.id)
    assert pending.cancel_retry_after is not None and len(adapter.cancellations) == 1
    await manager.cancel(placed.id)
    assert len(adapter.cancellations) == 1
    for _ in range(3):
        clock = risk.native_orders[placed.id].cancel_retry_after + timedelta(seconds=1)
        monkeypatch.setattr("evplatform.core.native_oms.utcnow", lambda: clock)
        await manager.cancel(placed.id)
    assert len(adapter.cancellations) == 3
    assert len(adapter.submissions) == 1
    assert risk.kill_switch and risk.exposures() == D(".47")


async def test_account_changed_cannot_reconcile_or_cancel_original_wallet(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.bound_account_fingerprint = "b" * 64
    adapter.report = report(placed, quantity="0", cost="0", fees="0", status="cancelled", terminal=True)
    await manager.reconcile(placed.id)
    await manager.cancel(placed.id)
    assert not adapter.cancellations
    assert risk.exposures() == D(".47") and risk.kill_switch


async def test_disputed_fee_after_settlement_reserves_actual_larger_liability(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = report(placed, quantity="100", cost=".45", status="filled", terminal=True)
    await manager.reconcile(placed.id)
    result = NativeSettlement(order_id=placed.id, venue="novig", account_fingerprint="a"*64,
        environment="sandbox", market_fingerprint=market.identity(), settlement_id="settle-dispute",
        payout_usd="1", settled_at=utcnow(), evidence="OFFLINE authenticated settlement fixture")
    await manager.settle(result)
    adapter.report = report(placed, quantity="100", cost=".45", fees="2", status="filled", terminal=True)
    disputed = await manager.reconcile(placed.id)
    assert disputed.status == OrderStatus.UNKNOWN
    assert placed.id not in risk.settled
    assert risk.exposures() >= D("2.45")
    restored_risk = RiskEngine(risk.config)
    recovered = NativeOrderManager(restored_risk, storage, manager.bus, Metrics(), lock=asyncio.Lock(),
        revalidate=lambda o: None, preflight=lambda o: None)
    await recovered.start()
    assert placed.id not in restored_risk.settled
    assert restored_risk.exposures() >= D("2.45")


async def test_paper_order_cannot_oppose_reserved_native_position(system, market, quotes, book, pricing):
    manager, risk, storage, adapter = system
    await manager.execute(order(market))
    opposite_book = book.model_copy(update={"outcome": "away"})
    candidate = opportunity(market, fair_value(market, quotes, pricing), opposite_book, pricing)
    with pytest.raises(RiskRejected, match="Opposing"):
        risk.reserve(candidate, market, opposite_book, "paper")
    assert not risk.orders


async def test_native_order_cannot_oppose_reserved_paper_position(system, market, quotes, book, pricing):
    manager, risk, storage, adapter = system
    candidate = opportunity(market, fair_value(market, quotes, pricing), book, pricing)
    risk.reserve(candidate, market, book, "paper")
    with pytest.raises(RiskRejected, match="Opposing"):
        await manager.execute(order(market, outcome="away"))
    assert not adapter.submissions


async def test_native_exposure_counts_toward_paper_total_limit_for_other_event(system, market, quotes, book, pricing):
    manager, risk, storage, adapter = system
    await manager.execute(order(market))
    risk.config.max_total_exposure = D("1")
    risk.config.max_order_stake = D("1")
    other_market = market.model_copy(update={"id": "other-event:moneyline", "event_id": "other-event"})
    other_quotes = [q.model_copy(update={"market_id": other_market.id}) for q in quotes]
    other_book = book.model_copy(update={"market_id": other_market.id})
    candidate = opportunity(other_market, fair_value(other_market, other_quotes, pricing), other_book, pricing)
    with pytest.raises(RiskRejected, match="Total exposure"):
        risk.reserve(candidate, other_market, other_book, "paper")
    assert risk.exposures() == D(".47")


async def test_slow_reconciliation_does_not_delay_cancel_or_erase_cancel_pending(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    started, release = asyncio.Event(), asyncio.Event()
    async def slow_read(native_id, intent):
        started.set()
        await release.wait()
        return report(placed)
    adapter.reconcile_native = slow_read
    read = asyncio.create_task(manager.reconcile(placed.id))
    await started.wait()
    try:
        cancelled = await asyncio.wait_for(manager.cancel(placed.id), 1)
        assert cancelled.status == OrderStatus.CANCEL_PENDING
        assert adapter.cancellations == [placed.venue_order_id]
    finally:
        release.set()
        result = await read
    assert result.status == OrderStatus.CANCEL_PENDING
    assert result.filled_quantity == 40 and result.filled_cost_usd == D(".18")
    assert result.last_snapshot.status == OrderStatus.PARTIAL  # Preserve actual venue evidence.
    assert not result.reconciliation_complete
    assert risk.exposures() == D(".47")


async def test_concurrent_reconciliations_share_one_native_read(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    async def slow_read(native_id, intent):
        calls.append(native_id)
        started.set()
        await release.wait()
        return report(placed)
    adapter.reconcile_native = slow_read
    first = asyncio.create_task(manager.reconcile(placed.id))
    await started.wait()
    second = asyncio.create_task(manager.reconcile(placed.id))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert results[0] == results[1]
    assert calls == [placed.venue_order_id]
    assert not manager._reconcile_reads
    rows = await (await storage.connection.execute("SELECT COUNT(*) FROM native_reconciliations")).fetchone()
    assert rows == (1,)


async def test_cancelled_reconciliation_stops_network_read_and_preserves_reservation(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def blocked_read(native_id, intent):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    adapter.reconcile_native = blocked_read
    task = asyncio.create_task(manager.reconcile(placed.id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and not manager._reconcile_reads
    assert risk.native_orders[placed.id] == placed
    assert risk.exposures() == D(".47")
    adapter.reconcile_native = lambda native_id, intent: asyncio.sleep(0, result=report(placed))
    assert (await manager.reconcile(placed.id)).filled_quantity == 40


@pytest.mark.parametrize("changed", ["adapter", "account", "client", "intent", "native_order_id"])
async def test_binding_change_during_native_read_rejects_old_response(system, market, changed):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    started, release = asyncio.Event(), asyncio.Event()
    adapter.client = object()
    async def slow_read(native_id, intent):
        started.set()
        await release.wait()
        return report(placed, quantity="0", cost="0", fees="0", status="cancelled", terminal=True)
    adapter.reconcile_native = slow_read
    task = asyncio.create_task(manager.reconcile(placed.id))
    await started.wait()
    async with manager.lock:
        if changed == "adapter":
            manager.adapters["novig"] = Adapter(risk)
        elif changed == "account":
            adapter.bound_account_fingerprint = "b" * 64
        elif changed == "client":
            adapter.client = object()
        elif changed == "intent":
            await manager._save(placed.model_copy(update={"intent": placed.intent.model_copy(update={"limit_price": D(".44")})}))
        else:
            await manager._save(placed.model_copy(update={"venue_order_id": "different-order"}))
        expected = risk.native_orders[placed.id]
    release.set()
    assert await task == expected
    assert risk.kill_switch and not expected.reconciliation_complete
    assert risk.exposures() >= D(".46")
    rows = await (await storage.connection.execute("SELECT COUNT(*) FROM native_reconciliations")).fetchone()
    assert rows == (0,)


async def test_older_inflight_read_is_applied_against_latest_reconciled_totals(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    started, release = asyncio.Event(), asyncio.Event()
    older = report(placed)
    older = older.model_copy(update={"snapshot": older.snapshot.model_copy(update={"revision": 9})})
    async def slow_read(native_id, intent):
        started.set()
        await release.wait()
        return older
    adapter.reconcile_native = slow_read
    task = asyncio.create_task(manager.reconcile(placed.id))
    await started.wait()
    latest = report(placed, quantity="100", cost=".45", status="filled", terminal=True)
    latest = latest.model_copy(update={"snapshot": latest.snapshot.model_copy(update={"revision": 10})})
    async with manager.lock:
        expected = await manager._apply_report(risk.native_orders[placed.id], latest)
    release.set()
    assert await task == expected
    assert expected.status == OrderStatus.FILLED and expected.filled_quantity == 100
    assert not risk.kill_switch


async def test_read_started_before_unknown_cancel_cannot_reopen_order(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    started, release = asyncio.Event(), asyncio.Event()
    async def slow_read(native_id, intent):
        started.set()
        await release.wait()
        return report(placed)
    async def unknown_cancel(native_id, intent):
        raise TimeoutError("OFFLINE cancellation outcome unknown")
    adapter.reconcile_native, adapter.cancel_native = slow_read, unknown_cancel
    task = asyncio.create_task(manager.reconcile(placed.id))
    await started.wait()
    pending = await asyncio.wait_for(manager.cancel(placed.id), 1)
    release.set()
    assert await task == pending
    assert pending.status == OrderStatus.UNKNOWN and pending.filled_quantity == 0
    assert risk.kill_switch and risk.exposures() == D(".47")


async def test_identity_discovery_does_not_hold_global_lock(system, market):
    manager, risk, storage, adapter = system
    adapter.fail = True
    unknown = await manager.execute(order(market))
    started, release = asyncio.Event(), asyncio.Event()
    async def resolve(intent):
        started.set()
        await release.wait()
        return "recovered-native-order"
    adapter.resolve_client_order = resolve
    bound = unknown.model_copy(update={"venue_order_id": "recovered-native-order"})
    adapter.report = report(bound, quantity="0", cost="0", fees="0", status="cancelled", terminal=True)
    task = asyncio.create_task(manager.reconcile(unknown.id))
    await started.wait()
    try:
        await asyncio.wait_for(manager.lock.acquire(), 1)
        manager.lock.release()
    finally:
        release.set()
    result = await task
    assert result.venue_order_id == bound.venue_order_id and result.status == OrderStatus.CANCELLED
    assert risk.exposures() == 0
