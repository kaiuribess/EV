"""Offline concurrent account/settlement lifecycle tests; no native requests."""
import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.native_oms import NativeSettlement
from evplatform.venues.account_probe import NativeAccountSnapshot
from evplatform.venues.kalshi import KalshiClient, KalshiSettings

from test_native_oms import system, order, report
from test_native_runtime_maintenance import runtime_for


def prepare(system):
    runtime = runtime_for(system)
    adapter = system[3]
    runtime.profile = SimpleNamespace(venue="novig", environment="sandbox")
    runtime.mapper = SimpleNamespace(mappings=())
    runtime.engine.registry = SimpleNamespace(records={"novig": SimpleNamespace(plugin=SimpleNamespace(client=adapter.client))})
    return runtime


def snapshot(*, reconciled=True):
    return NativeAccountSnapshot(venue="novig", environment="sandbox", account_fingerprint="a"*64,
        identity_verified=True, observed_at=utcnow(), balance_usd="10", cash_available_verified=reconciled,
        reconciled=reconciled, reason="OFFLINE account fixture")


async def test_kalshi_runtime_read_only_snapshot_loads_without_executable_mapping(system):
    manager, risk, _, _ = system
    runtime = runtime_for(system)
    risk.kill_switch = True
    risk.auto_trade = False
    ticker = "OFFLINE-NBA-TICKER"
    event = "OFFLINE-NBA-EVENT"
    client = KalshiClient(KalshiSettings(api_key_id="fixture-key", environment="sandbox"))
    calls = []
    async def request(method, path, **kwargs):
        assert method == "GET"
        calls.append(path)
        if path == f"/markets/{ticker}":
            return {"market": {"ticker": ticker, "event_ticker": event, "exchange_index": 3}}
        if path == "/api_keys":
            return {"api_keys": [{"api_key_id": "fixture-key", "scopes": ["read"]}]}
        if path == "/portfolio/balance":
            assert kwargs["params"] == {"subaccount": 0, "exchange_index": 3}
            return {"balance_dollars": "25.00", "balance_breakdown": [
                {"exchange_index": 3, "balance": "25.00"}]}
        if path == "/portfolio/orders": return {"orders": [], "cursor": ""}
        if path == "/portfolio/positions": return {"market_positions": [], "cursor": ""}
        pytest.fail("Placement or unknown endpoint")
    client.request = request
    runtime.profile = SimpleNamespace(venue="kalshi", environment="sandbox", market_ids=("nba-pilot",))
    runtime.structural_review = SimpleNamespace(native_venue="kalshi", canonical_market_id="nba-pilot",
        native_outcomes={"home": ticker, "away": ticker}, native_event_id=event)
    runtime.mapper = SimpleNamespace(mappings=())
    runtime.engine.registry = SimpleNamespace(records={"kalshi": SimpleNamespace(
        plugin=SimpleNamespace(client=client))})
    manager.adapters.pop("kalshi", None)
    await runtime.refresh_account()
    assert runtime.account_snapshots["kalshi"].exchange_index == 3
    assert runtime.account_snapshots["kalshi"].cash_available_verified
    assert "kalshi" not in manager.adapters
    assert risk.kill_switch and not risk.auto_trade
    assert calls.count(f"/markets/{ticker}") == 1
    runtime._last_account_probes["kalshi"] = 0
    await runtime.refresh_account()
    assert calls.count(f"/markets/{ticker}") == 1
    await client.disconnect()


async def filled_order(system, market, *, venue="novig"):
    manager, _, _, adapter = system
    candidate = order(market)
    if venue == "kalshi":
        from evplatform.core.native_accounting import NativeInstrument
        instrument = NativeInstrument.model_validate({**candidate.intent.instrument.model_dump(),
            "venue": "kalshi", "quantity_unit": "contracts", "payout_per_unit_usd": "1", "native_outcome": "yes"})
        candidate = candidate.model_copy(update={"intent": candidate.intent.model_copy(update={
            "instrument": instrument, "quantity": Decimal(1)})})
    placed = await manager.execute(candidate)
    adapter.report = report(placed, quantity="1" if venue == "kalshi" else "100", cost=".45", fees=".001", status="filled", terminal=True)
    if venue == "kalshi":
        adapter.report = adapter.report.model_copy(update={"snapshot": adapter.report.snapshot.model_copy(update={
            "cumulative_gross_payout_usd": Decimal(1)})})
    return await manager.reconcile(placed.id)


def cash_result(filled):
    return NativeSettlement(order_id=filled.id, venue=filled.venue, environment=filled.mode,
        account_fingerprint=filled.account_fingerprint, market_fingerprint=filled.market_fingerprint,
        settlement_id="OFFLINE-paid", payout_usd="1", settled_at=utcnow(), evidence="OFFLINE wallet payment fixture")


@pytest.mark.parametrize("change", ["private_update", "ledger_fill", "settled_set", "client", "profile"])
async def test_in_flight_account_result_cannot_restore_obsolete_trust(system, market, monkeypatch, change):
    manager, risk, _, _ = system
    runtime = prepare(system)
    placed = await manager.execute(order(market))
    began, finish = asyncio.Event(), asyncio.Event()
    async def delayed_probe(*args, **kwargs):
        began.set()
        await finish.wait()
        return snapshot()
    monkeypatch.setattr("evplatform.venues.account_probe.probe_native_account", delayed_probe)
    task = asyncio.create_task(runtime.refresh_account())
    await began.wait()
    assert "novig" in runtime._account_probes and "novig" not in runtime.account_snapshots
    if change == "private_update": runtime.invalidate_account("novig")
    elif change == "ledger_fill":
        risk.native_orders[placed.id] = placed.model_copy(update={"filled_quantity": Decimal(1)})
    elif change == "settled_set": risk.settled.add(placed.id)
    elif change == "client": runtime.engine.registry.records["novig"].plugin.client = object()
    elif change == "profile": runtime.profile = SimpleNamespace(venue="novig", environment="production")
    finish.set()
    await task
    assert "novig" not in runtime.account_snapshots and "novig" not in runtime._account_snapshot_fences
    assert not runtime._account_probes and not risk.kill_switch
    assert runtime.last_account_probe == 0


async def test_concurrent_probes_do_not_race_installation_and_cancel_clears_trust(system, monkeypatch):
    runtime = prepare(system)
    began, finish = asyncio.Event(), asyncio.Event()
    async def probe(*args, **kwargs):
        began.set()
        await finish.wait()
        return snapshot()
    mock = AsyncMock(side_effect=probe)
    monkeypatch.setattr("evplatform.venues.account_probe.probe_native_account", mock)
    task = asyncio.create_task(runtime.refresh_account())
    await began.wait()
    await runtime.refresh_account()
    assert mock.await_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not runtime.account_snapshots and not runtime._account_probes
    assert not runtime._account_probe_lock.locked()


async def test_fresh_snapshot_is_bound_to_values_and_not_object_identity(system, market, monkeypatch):
    manager, risk, _, _ = system
    runtime = prepare(system)
    placed = await manager.execute(order(market))
    monkeypatch.setattr("evplatform.venues.account_probe.probe_native_account", AsyncMock(return_value=snapshot()))
    await runtime.refresh_account()
    before = runtime._account_snapshot_fences["novig"]
    assert before == runtime._ledger_fence()
    risk.native_orders[placed.id] = placed.model_copy(update={"fees": Decimal(1)})
    assert before != runtime._ledger_fence()


@pytest.mark.parametrize("venue", ["novig", "kalshi"])
async def test_disappearing_filled_position_gets_actual_settlement_then_reprobe(system, market, monkeypatch, venue):
    manager, risk, storage, _ = system
    runtime = prepare(system)
    if venue == "kalshi":
        manager.adapters[venue] = manager.adapters["novig"]
        runtime.profile.venue = venue
        runtime.execution_lease.scope = (venue, "sandbox")
        runtime.engine.registry.records[venue] = runtime.engine.registry.records["novig"]
    filled = await filled_order(system, market, venue=venue)
    result = cash_result(filled)
    checked = []
    async def account_probe(client, *, managed_orders, settled_order_ids):
        assert runtime._account_probes == {venue} and not runtime.account_snapshots
        checked.append(settled_order_ids)
        return snapshot(reconciled=filled.id in settled_order_ids).model_copy(update={"venue": venue})
    async def settlement_probe(client, owned, inventory):
        assert owned.id == filled.id and filled.id not in risk.settled
        assert runtime._account_probes and not runtime.account_snapshots
        assert risk.exposures() > 0  # Disappearance itself never releases it.
        return result
    monkeypatch.setattr("evplatform.venues.account_probe.probe_native_account", account_probe)
    module = "kalshi_settlement" if venue == "kalshi" else "settlement"
    monkeypatch.setattr(f"evplatform.venues.{module}.reconcile_{venue}_settlement", settlement_probe)
    runtime.last_settlement_probe = __import__("time").monotonic()  # Recovery bypasses the periodic timer.
    await runtime.refresh_account()
    assert checked == [frozenset(), frozenset({filled.id})]
    assert runtime.account_snapshots[venue].reconciled and not risk.kill_switch
    assert risk.exposures() == 0 and manager.settlements[filled.id] == result
    row = await (await storage.connection.execute("SELECT order_id FROM native_settlements")).fetchone()
    assert row == (filled.id,)


@pytest.mark.parametrize("settlement", ["missing", "error"])
async def test_unproven_disappearance_reprobes_once_then_halts_without_releasing(system, market, monkeypatch, settlement):
    manager, risk, _, _ = system
    runtime = prepare(system)
    filled = await filled_order(system, market)
    probe = AsyncMock(return_value=snapshot(reconciled=False))
    monkeypatch.setattr("evplatform.venues.account_probe.probe_native_account", probe)
    reconcile = AsyncMock(return_value=None)
    if settlement == "error": reconcile.side_effect = ValueError("OFFLINE incompatible payment")
    monkeypatch.setattr("evplatform.venues.settlement.reconcile_novig_settlement", reconcile)
    await runtime.refresh_account()
    assert probe.await_count == 2 and reconcile.await_count == 1
    assert risk.kill_switch and filled.id not in risk.settled
    assert not manager.settlements and risk.exposures() > 0
    assert not runtime.account_snapshots["novig"].reconciled


@pytest.mark.parametrize("deferred", ["coordinator_busy", "inner_timeout", "outer_timeout"])
async def test_deferred_portfolio_settlement_keeps_exposure_and_retries_without_false_halt(system, market, monkeypatch, deferred):
    manager, risk, storage, _ = system
    runtime = prepare(system)
    runtime.portfolio = SimpleNamespace(profiles=(runtime.profile,))
    runtime.execution_lease.scope = ("portfolio", "sandbox")
    filled = await filled_order(system, market)
    exposure = risk.exposures()
    runtime.account_snapshots["novig"] = snapshot()
    runtime._account_snapshot_fences["novig"] = runtime._ledger_fence()

    async def account_probe(client, *, managed_orders, settled_order_ids):
        return snapshot(reconciled=filled.id in settled_order_ids)

    probe = AsyncMock(side_effect=account_probe)
    settlement = AsyncMock(return_value=cash_result(filled))
    monkeypatch.setattr("evplatform.venues.account_probe.probe_native_account", probe)
    monkeypatch.setattr("evplatform.venues.settlement.reconcile_novig_settlement", settlement)
    original_reconcile = runtime.reconcile_settlements
    if deferred == "coordinator_busy":
        await runtime._settlement_probe_lock.acquire()
    elif deferred == "inner_timeout":
        settlement.side_effect = TimeoutError("OFFLINE bounded venue deadline")
    else:
        runtime.reconcile_settlements = AsyncMock(side_effect=TimeoutError("OFFLINE bounded recovery deadline"))
    try:
        await runtime.refresh_account()
    finally:
        if deferred == "coordinator_busy":
            runtime._settlement_probe_lock.release()

    assert probe.await_count == 1  # An incomplete check is not a second inventory verdict.
    assert not risk.kill_switch and risk.exposures() == exposure > 0
    assert filled.id not in risk.settled and not manager.settlements
    assert not runtime.account_snapshots and not runtime._account_snapshot_fences
    assert not runtime._account_probes and "novig" not in runtime._last_account_probes
    assert runtime.last_account_probe == 0
    row = await (await storage.connection.execute("SELECT COUNT(*) FROM native_settlements")).fetchone()
    assert row == (0,)

    # No clock advance: clearing the per-venue timer allows the next maintenance
    # pass to obtain actual payment evidence and then verify available cash.
    settlement.side_effect = None
    runtime.reconcile_settlements = original_reconcile
    await runtime.refresh_account()
    assert probe.await_count == 3 and filled.id in manager.settlements
    assert runtime.account_snapshots["novig"].reconciled and not risk.kill_switch
    assert risk.exposures() == 0


async def test_successful_payment_cannot_hide_unrelated_remaining_mismatch(system, market, monkeypatch):
    manager, risk, _, _ = system
    runtime = prepare(system)
    filled = await filled_order(system, market)
    probe = AsyncMock(return_value=snapshot(reconciled=False))
    monkeypatch.setattr("evplatform.venues.account_probe.probe_native_account", probe)
    monkeypatch.setattr("evplatform.venues.settlement.reconcile_novig_settlement", AsyncMock(return_value=cash_result(filled)))
    await runtime.refresh_account()
    assert probe.await_count == 2 and risk.kill_switch
    assert filled.id in manager.settlements  # Only proven payment is recorded.
    assert not runtime.account_snapshots["novig"].reconciled


async def test_private_update_during_second_probe_discards_recovery_snapshot(system, market, monkeypatch):
    manager, risk, _, _ = system
    runtime = prepare(system)
    filled = await filled_order(system, market)
    calls = 0
    async def probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            runtime.invalidate_account("novig")
        return snapshot(reconciled=calls == 2)
    monkeypatch.setattr("evplatform.venues.account_probe.probe_native_account", probe)
    monkeypatch.setattr("evplatform.venues.settlement.reconcile_novig_settlement", AsyncMock(return_value=cash_result(filled)))
    await runtime.refresh_account()
    assert filled.id in manager.settlements and not risk.kill_switch
    assert not runtime.account_snapshots and not runtime._account_snapshot_fences


async def test_settlement_acceptance_invalidates_preexisting_available_cash(system, market, monkeypatch):
    manager, _, _, _ = system
    runtime = prepare(system)
    filled = await filled_order(system, market)
    runtime.account_snapshots["novig"] = snapshot()
    runtime._account_snapshot_fences["novig"] = runtime._ledger_fence()
    runtime._last_account_probes.update(novig=100, kalshi=200)
    monkeypatch.setattr("evplatform.venues.settlement.reconcile_novig_settlement", AsyncMock(return_value=cash_result(filled)))
    assert await runtime.reconcile_settlements() == 1
    assert not runtime.account_snapshots and not runtime._account_snapshot_fences
    assert runtime._last_account_probes == {"kalshi": 200}
    assert filled.id in manager.settlements


@pytest.mark.parametrize("change", ["paused", "killed", "lease", "expired", "future", "intent", "fee_bound"])
async def test_dispatch_guard_rechecks_paced_order_before_any_write(system, market, change):
    from evplatform.core.risk import RiskRejected
    _, risk, _, _ = system
    runtime = prepare(system)
    current = order(market).model_copy(update={"status": OrderStatus.SUBMITTING})
    if change in {"expired", "future"}:
        shift = timedelta(seconds=-2 if change == "expired" else 2)
        current = current.model_copy(update={"intent": current.intent.model_copy(update={"created_at": utcnow()+shift})})
    risk.native_orders[current.id] = current
    runtime.require_preflight = lambda _: None
    runtime.revalidate_order = lambda _: None
    if change == "paused": risk.auto_trade = False
    elif change == "killed": risk.kill_switch = True
    elif change == "lease": runtime.execution_lease.held = False
    elif change == "intent": risk.native_orders.pop(current.id)
    elif change == "fee_bound":
        def fail(_): raise RiskRejected("OFFLINE changed fee/reference proof")
        runtime.revalidate_order = fail
    with pytest.raises(RiskRejected):
        runtime.dispatch_guard(current.intent)


async def test_dispatch_guard_preserves_single_use_permit_and_rechecks_limits(system, market):
    _, risk, _, _ = system
    runtime = prepare(system)
    current = order(market).model_copy(update={"status": OrderStatus.SUBMITTING})
    risk.native_orders[current.id] = current
    checks = []
    runtime.require_preflight = lambda _: checks.append("preflight")
    runtime.revalidate_order = lambda _: checks.append("prices")
    permit = risk.authority.issue(current.id)
    runtime.dispatch_guard(current.intent)
    assert checks == ["preflight", "prices"]
    assert risk.authority.consume(permit, current.id)


async def test_polymarket_private_wrapper_invalidates_inventory_before_mapping_await(system):
    runtime = prepare(system)
    runtime.account_snapshots["polymarket_us"] = snapshot()
    runtime._account_snapshot_fences["polymarket_us"] = runtime._ledger_fence()
    runtime.bind_client = lambda *args: None
    runtime.mapper.process = lambda *args: []
    await runtime.on_frame("polymarket_us", object(), {
        "type": "polymarket_us_monitor", "channel": "private", "kind": "data", "payload": {}})
    assert "polymarket_us" not in runtime.account_snapshots
    assert "polymarket_us" not in runtime._account_snapshot_fences
    assert runtime._account_generations["polymarket_us"] == 1


async def test_polymarket_execution_receipt_saves_before_frame_finishes(system, market):
    runtime = prepare(system)
    current = order(market)
    current = current.model_copy(update={"intent": current.intent.model_copy(update={
        "instrument": current.intent.instrument.model_copy(update={"venue": "polymarket_us"})}),
        "venue_order_id": "owned-native"})
    runtime.engine.risk.native_orders[current.id] = current
    runtime.account_snapshots["polymarket_us"] = snapshot()
    calls = []
    adapter = SimpleNamespace(observe_private_frame=lambda _: calls.append("observed"),
        execution_fills=lambda native_id, intent: ("OFFLINE exact bound fill",))
    runtime.bind_client = lambda *args: adapter
    runtime.mapper.process = lambda *args: []
    async def persist(order_id, fills, *, adapter):
        assert "polymarket_us" not in runtime.account_snapshots
        assert order_id == current.id and fills == ("OFFLINE exact bound fill",)
        calls.append("persisted")
    runtime.manager.record_private_fills = persist
    await runtime.on_frame("polymarket_us", object(), {"type": "polymarket_us_monitor",
        "channel": "private", "kind": "data", "payload": {}})
    assert calls == ["observed", "persisted"]
    assert "polymarket_us" in runtime.dirty_venues


async def test_polymarket_sync_requires_snapshot_boundary_and_exact_private_fence(system):
    from evplatform.venues.polymarket_us_execution import PolymarketUSExecutionVenue
    runtime = prepare(system)
    client = SimpleNamespace(venue_id="polymarket_us", settings=SimpleNamespace(environment="production"), _ready={"private": True})
    adapter = PolymarketUSExecutionVenue.__new__(PolymarketUSExecutionVenue)
    adapter.client, adapter.private_snapshot_complete = client, True
    adapter.private_generation, adapter.private_revision = 1, 7
    runtime.manager.adapters["polymarket_us"] = adapter
    record = SimpleNamespace(enabled=True, health={"authenticated": True, "streaming": True},
        plugin=SimpleNamespace(client=client))
    account = snapshot().model_copy(update={"venue": "polymarket_us", "environment": "production",
                                           "private_connection_generation": 1, "private_revision": 7})
    assert runtime.private_synchronized(record, account)
    adapter.private_revision += 1
    assert not runtime.private_synchronized(record, account)
    adapter.private_revision = 7
    client._ready["private"] = False
    assert not runtime.private_synchronized(record, account)


async def test_polymarket_bind_restores_exact_persisted_fill_evidence(system, market, monkeypatch):
    runtime = prepare(system)
    manager, risk, _, _ = system
    candidate = order(market)
    instrument = candidate.intent.instrument.model_copy(update={"venue": "polymarket_us",
        "quantity_unit": "contracts", "payout_per_unit_usd": Decimal(1), "native_outcome": "yes"})
    restored = candidate.model_copy(update={"intent": candidate.intent.model_copy(update={
        "instrument": instrument, "quantity": Decimal(1), "mode": "production"}),
        "venue_order_id": "native-owned", "status": OrderStatus.OPEN})
    risk.native_orders[restored.id] = restored
    runtime.profile = SimpleNamespace(venue="polymarket_us", environment="production")
    runtime.execution_lease.scope = ("polymarket_us", "production")
    runtime.mapper.mappings = [SimpleNamespace(market=market, instrument=instrument,
                                               fee_bound=SimpleNamespace(verified=True))]
    persisted = ("OFFLINE exact journal marker",)
    manager.recovered_fills = lambda local_id: persisted if local_id == restored.id else ()
    calls = []
    class Adapter:
        def __init__(self, client, authority, *, mappings):
            self.client = client
            assert mappings[0].native_instrument_id == instrument.instrument_id
        def bind_native_intent(self, native_id, intent): calls.append(("bind", native_id, intent))
        def seed_fills(self, native_id, intent, fills): calls.append(("seed", native_id, intent, fills))
    monkeypatch.setattr("evplatform.venues.polymarket_us_execution.PolymarketUSExecutionVenue", Adapter)
    client = SimpleNamespace(settings=SimpleNamespace(environment="production"))
    adapter = runtime.bind_client("polymarket_us", client)
    assert calls == [("bind", "native-owned", restored.intent), ("seed", "native-owned", restored.intent, persisted)]
    assert adapter.dispatch_guard == runtime.dispatch_guard
    bound_guard = adapter.dispatch_guard
    assert runtime.bind_client("polymarket_us", client) is adapter
    assert adapter.dispatch_guard is bound_guard
