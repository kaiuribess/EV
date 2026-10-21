"""Offline lifecycle ordering; all account operations below are test doubles."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evplatform.core.config import Settings
from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.native_oms import NativeSettlement
from evplatform.core.native_runtime import NativeRuntime
from evplatform.engine import Engine

from test_native_oms import system, order, report


def runtime_for(system):
    manager, risk, storage, adapter = system
    engine = SimpleNamespace(risk=risk, storage=storage, bus=manager.bus,
                             metrics=manager.metrics, oms=SimpleNamespace(lock=manager.lock))
    runtime = NativeRuntime(engine)
    runtime.execution_lease = SimpleNamespace(held=True, scope=("novig", "sandbox"))
    runtime.manager = manager
    adapter.client = SimpleNamespace(settings=SimpleNamespace(environment="sandbox"))
    return runtime


async def test_known_pending_ack_blocks_fresh_inventory_without_false_halt(system, market, monkeypatch):
    manager, risk, _, adapter = system
    runtime = runtime_for(system)
    placed = await manager.execute(order(market))
    runtime.profile = SimpleNamespace(venue="novig", environment="sandbox")
    runtime.mapper = SimpleNamespace(mappings=())
    runtime.engine.registry = SimpleNamespace(records={"novig": SimpleNamespace(plugin=SimpleNamespace(client=adapter.client))})
    snapshot = SimpleNamespace(identity_verified=True, reconciled=False, account_fingerprint=placed.account_fingerprint)
    probe = AsyncMock(return_value=snapshot)
    monkeypatch.setattr("evplatform.venues.account_probe.probe_native_account", probe)
    await runtime.refresh_account()
    assert not risk.kill_switch
    assert runtime.account_snapshots["novig"].reconciled is False
    # Once local reconciliation is complete, a persistent mismatch is external
    # or unexplained and must stop the system.
    risk.native_orders[placed.id] = placed.model_copy(update={"reconciliation_complete": True})
    runtime.last_account_probe = 0
    await runtime.refresh_account()
    assert risk.kill_switch


async def test_account_reprobed_after_order_reconciliation(system, market):
    manager, risk, _, adapter = system
    runtime = runtime_for(system)
    placed = await manager.execute(order(market))
    adapter.report = report(placed)
    runtime.dirty_venues.add("novig")
    runtime._last_account_probes.update(novig=100, kalshi=200)
    runtime.account_snapshots["novig"] = object()
    runtime._account_snapshot_fences["novig"] = runtime._ledger_fence()
    checks = []
    async def account():
        checks.append(risk.native_orders[placed.id].reconciliation_complete)
        if len(checks) == 2:
            assert runtime._last_account_probes == {"kalshi": 200}
            assert "novig" not in runtime.account_snapshots
            assert "novig" not in runtime._account_snapshot_fences
    runtime.refresh_account = account
    runtime.require_preflight = lambda _: None
    runtime.revalidate_order = lambda _: None
    await runtime.maintenance_step()
    assert checks == [False, True]
    assert risk.native_orders[placed.id].status == OrderStatus.PARTIAL
    assert not risk.kill_switch


@pytest.mark.parametrize("failure", ["ungraded", "network", "changed"])
async def test_accepted_settlement_losing_evidence_is_durably_invalidated(system, market, monkeypatch, failure):
    manager, risk, storage, adapter = system
    runtime = runtime_for(system)
    placed = await manager.execute(order(market))
    adapter.report = report(placed, quantity="100", cost=".45", fees=".001", status="filled", terminal=True)
    filled = await manager.reconcile(placed.id)
    settlement = NativeSettlement(order_id=filled.id, venue=filled.venue, environment=filled.mode,
        account_fingerprint=filled.account_fingerprint, market_fingerprint=filled.market_fingerprint,
        settlement_id="OFFLINE-result", payout_usd="1", additional_fee_usd="0", fee_refund_usd="0",
        settled_at=utcnow(), evidence="OFFLINE actual cash fixture")
    await manager.settle(settlement)
    assert filled.id in risk.settled
    assert risk.daily_realized > 0
    probe = AsyncMock(return_value=None)
    if failure == "network":
        probe.side_effect = OSError("OFFLINE transport failure")
    elif failure == "changed":
        probe.return_value = settlement.model_copy(update={"payout_usd": 0})
    monkeypatch.setattr("evplatform.venues.settlement.reconcile_novig_settlement", probe)
    await runtime.reconcile_settlements()
    assert filled.id not in risk.settled
    assert filled.id in manager.invalidated_settlements
    assert risk.kill_switch
    assert risk.daily_realized == 0
    assert risk.exposures() > 0
    row = await (await storage.connection.execute(
        "SELECT order_id FROM native_settlement_invalidations WHERE order_id=?", (filled.id,))).fetchone()
    assert row[0] == filled.id


async def test_operator_kill_calls_native_cancellation_immediately(tmp_path):
    engine = Engine(Settings(database_path=str(tmp_path / "unused.db")))
    engine.storage.save_config = AsyncMock()
    engine.notice = AsyncMock()
    engine.oms.cancel_all = AsyncMock()
    engine.native.manager.cancel_all = AsyncMock()
    await engine.control(kill_switch=True)
    assert engine.risk.kill_switch
    engine.native.manager.cancel_all.assert_awaited_once()


async def test_invalidated_settlement_prevents_operator_kill_reset(tmp_path):
    engine = Engine(Settings(database_path=str(tmp_path / "unused-reset.db")))
    engine.risk.halt("OFFLINE settlement repair required")
    engine.native.manager.invalidated_settlements.add("OFFLINE-order")
    with pytest.raises(ValueError, match="invalidated native settlement"):
        await engine.control(kill_switch=False)
    assert engine.risk.kill_switch


async def test_first_private_snapshot_binds_adapter_before_account_probe(tmp_path, market):
    import httpx
    from evplatform.venues.novig import NovigClient, NovigSettings
    from evplatform.venues.book_normalization import NativeBookMapper
    from test_native_book_normalization import mapping
    engine = Engine(Settings(database_path=str(tmp_path / "unused-first-frame.db")))
    native = engine.native
    native.profile = SimpleNamespace(venue="novig", environment="sandbox")
    native.execution_lease = SimpleNamespace(held=True, scope=("novig", "sandbox"))
    native.mapper = NativeBookMapper([mapping(market, "novig")])
    def no_network(request):
        raise AssertionError("First frame binding must do no network work")
    client = NovigClient(NovigSettings(environment="sandbox"),
                        httpx.AsyncClient(transport=httpx.MockTransport(no_network)))
    try:
        frame = {"snapshot": True, "orders": {"seq": 7, "open": []}}
        await native.on_frame("novig", client, frame)
        adapter = native.manager.adapters["novig"]
        assert adapter.client is client
        assert adapter._private_sequences.values["orders"] == 7
        assert native.account_snapshots == {}  # Binding alone grants no access.
    finally:
        await client.disconnect()


async def test_narrow_settlement_scope_blocks_second_order_after_settlement(system, market):
    from evplatform.core.risk import RiskRejected
    runtime = runtime_for(system)
    manager, risk, _, adapter = system
    prior = await manager.execute(order(market))
    adapter.report = report(prior, quantity="100", cost=".45", fees=".001", status="filled", terminal=True)
    prior = await manager.reconcile(prior.id)
    risk.settled.add(prior.id)
    candidate = order(market)
    runtime.profile = SimpleNamespace(venue="novig", environment="sandbox", account_fingerprint=candidate.account_fingerprint,
        market_ids=(market.id,), strategy_ids=(candidate.strategy,), settlement_policy="novig_ioc_win_loss_halt_on_exception")
    runtime.report = lambda: SimpleNamespace(live_trading_ready=False, sandbox_trading_ready=True)
    with pytest.raises(RiskRejected, match="only one managed order"):
        runtime.require_preflight(candidate)
