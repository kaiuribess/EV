"""Full OFFLINE native pipeline rehearsal, with no credentials or network.

Venue/account/evidence facts below are injected test doubles. Their live-schema
flags exercise gates; they are never persisted as genuine launch evidence.
This test is not proof of exchange connectivity or an actual sandbox fill.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import httpx

from evplatform.core.config import RiskConfig, Settings
from evplatform.core.models import Envelope, OrderStatus, utcnow
from evplatform.core.native_pricing import NativeLevel, instrument_fingerprint
from evplatform.core.readiness import digest_json
from evplatform.engine import Engine
from evplatform.venues.account_probe import NativeAccountSnapshot
from evplatform.venues.novig import NovigClient, NovigSettings

from test_launch_readiness import ready_fixture
from test_native_oms import Adapter, report
from test_native_pricing import setup_native


def bind_offline_launch(engine, market, monkeypatch):
    profile, _, _ = ready_fixture("sandbox")
    limits = profile.limits
    risk = engine.risk.config
    for field, value in {
        "max_order_stake": limits.max_order_stake_usd, "live_max_order_stake": limits.max_order_stake_usd,
        "max_event_exposure": limits.max_event_exposure_usd, "max_player_exposure": limits.max_player_exposure_usd,
        "max_venue_exposure": limits.max_venue_exposure_usd, "max_correlated_exposure": limits.max_correlated_exposure_usd,
        "max_total_exposure": limits.max_total_exposure_usd, "daily_loss_limit": limits.daily_loss_limit_usd,
        "max_open_orders": limits.max_open_orders, "max_orders_per_minute": limits.max_orders_per_minute,
    }.items():
        setattr(risk, field, value)
    profile = profile.model_copy(update={"venue": "novig", "market_ids": (market.id,),
        "strategy_ids": ("native_positive_ev",), "provider_ids": ("parlay_rest",),
        "build_id": engine.native.build_id,
        "pricing_digest": digest_json(engine.settings.pricing.model_dump(mode="json"))})
    evidence = tuple(item.model_copy(update={"venue": "novig", "profile_digest": profile.digest(),
        "build_id": engine.native.build_id, "verified_by": "OFFLINE fixture; not real account evidence"})
        for item in profile.evidence)
    engine.native.profile = profile.model_copy(update={"evidence": evidence})
    engine.native.execution_lease = SimpleNamespace(held=True, scope=("novig", "sandbox"), release=lambda: None)
    engine.native.mapper = SimpleNamespace(mapping_digest=profile.mapping_digest, mappings=())
    for name in ("parlay_rest", "novig"):
        record = engine.registry.records[name]
        record.enabled = True
        record.credential_status = "OFFLINE injected test transport"
        record.plugin.task = asyncio.create_task(asyncio.Event().wait(), name=f"OFFLINE-{name}-transport")
        record.health = {"authenticated": True, "state": "connected", "streaming": True,
                         "quota": {"provider_remaining": 100, "minute_remaining": 30, "cooldown_seconds": 0,
                                   "local_session_budget": 30, "reserved_cost": 1}}
    def no_network(request):
        raise AssertionError("OFFLINE pipeline must never issue a network request")
    client = NovigClient(NovigSettings(environment="sandbox"),
                         httpx.AsyncClient(transport=httpx.MockTransport(no_network)))
    client.authenticated = True  # Injected authentication fact, no real key.
    client.health = "streaming"
    client.book.sequences.values.update({"PRIVATE:orders": 0, "PRIVATE:positions": 0})
    class OfflineSocket:
        async def close(self):
            pass
    client._ws = OfflineSocket()
    engine.registry.records["novig"].plugin.client = client
    engine.native.account_snapshots["novig"] = NativeAccountSnapshot(
        venue="novig", environment="sandbox", account_fingerprint=profile.account_fingerprint,
        identity_verified=True, observed_at=utcnow(), balance_usd="10", open_order_count=0,
        position_count=0, reconciled=True, order_sequence=0, position_sequence=0,
        reason="OFFLINE account inventory fixture")
    engine.native._account_snapshot_fences["novig"] = engine.native._ledger_fence()
    original_facts = engine.native.runtime_facts
    # The fake adapter supplies settlement/reconciliation fixtures. It cannot
    # satisfy the real native adapter capability and account-evidence checks.
    monkeypatch.setattr(engine.native, "runtime_facts", lambda: original_facts().model_copy(
        update={"settlement_reconciliation_supported": True}))
    return client


async def test_offline_native_engine_stream_risk_fill_audit_and_restart(tmp_path, monkeypatch, market, quotes):
    monkeypatch.chdir(tmp_path)  # Never load a user's runtime launch profile.
    settings = Settings(database_path=str(tmp_path / "native-flow.db"), mode="sandbox", live_enabled=True,
                        risk=RiskConfig(live_max_order_stake=D("1")))
    engine = Engine(settings)
    engine.registry.records["positive_ev"].enabled = False
    engine.registry.records["native_positive_ev"].enabled = True
    await engine.start()
    submitted, client = None, None
    try:
        market, quotes, positive, _, _, _ = setup_native(market, quotes)
        quotes = [q.model_copy(update={"provider": "parlay_rest"}) for q in quotes]
        client = bind_offline_launch(engine, market, monkeypatch)
        adapter = Adapter(engine.risk)
        adapter.client = client
        engine.native.manager.register("novig", adapter)
        negative_instrument = positive.instrument.model_copy(update={"outcome": "away", "native_outcome": "native-away"})
        negative = positive.model_copy(update={"instrument": negative_instrument,
            "asks": (NativeLevel(price="0.650", quantity="100"),),
            "fee_bound": positive.fee_bound.model_copy(update={"instrument_fingerprint": instrument_fingerprint(negative_instrument)})})
        await engine.bus.publish(Envelope(schema_name="sports.market", source="OFFLINE fixture", payload=market))
        for quote in quotes:
            await engine.bus.publish(Envelope(schema_name="odds.quote", source="OFFLINE fixture", payload=quote))
        await engine.bus.publish(Envelope(schema_name="exchange.native_book", source="OFFLINE fixture", payload=negative))
        await asyncio.wait_for(engine.process_queue.join(), 2)
        assert engine.native.signals[market.id].status == "NEGATIVE EV"
        assert not adapter.submissions

        engine.risk.auto_trade = True
        await engine.bus.publish(Envelope(schema_name="exchange.native_book", source="OFFLINE fixture", payload=positive))
        await asyncio.wait_for(engine.process_queue.join(), 2)
        await asyncio.wait_for(engine.risk_queue.join(), 2)
        assert len(adapter.submissions) == 1, (engine.native.report().blocking_items, list(engine.events))
        submitted = next(iter(engine.risk.native_orders.values()))
        assert submitted.status == OrderStatus.OPEN and submitted.filled_quantity == 0
        assert submitted.quantity == 100
        assert submitted.intent.minimum_gross_payout_usd == 1
        assert engine.native.signals[market.id].status == "POSITIVE EV"
        assert any(not c.qualified for c in engine.native.candidates.values())

        adapter.report = report(submitted)
        partial = await engine.native.manager.reconcile(submitted.id)
        assert partial.filled_quantity == 40 and partial.filled_cost_usd == D(".18")
        await engine.native.manager.cancel(submitted.id)
        adapter.report = report(submitted, quantity="50", cost=".225", status="cancelled", terminal=True)
        cancelled = await engine.native.manager.reconcile(submitted.id)
        assert cancelled.filled_quantity == 50 and engine.risk.exposures() == D(".226")
        await engine.native.manager.reconcile(submitted.id)
        assert engine.risk.exposures() == D(".226")
        await asyncio.wait_for(engine.audit_queue.join(), 2)
        await asyncio.wait_for(engine.storage.queue.join(), 2)
        rows = await (await engine.storage.connection.execute("SELECT data FROM events WHERE schema_name='native.signal'")).fetchall()
        assert any('NEGATIVE EV' in row[0] for row in rows)
        assert any('POSITIVE EV' in row[0] for row in rows)
        assert engine.state()["native_orders"][0]["filled_quantity"] == "50"
    finally:
        await engine.stop()
        if client:
            await client.http.aclose()

    recovered = Engine(Settings(database_path=settings.database_path, mode="sandbox"))
    await recovered.start()
    try:
        restored = recovered.risk.native_orders[submitted.id]
        assert restored.status == OrderStatus.CANCELLED
        assert restored.filled_quantity == 50 and restored.filled_cost_usd == D(".225")
        assert recovered.risk.exposures() == D(".226")
        assert not recovered.risk.auto_trade
        assert not recovered.native.report().safe_to_arm
        assert not recovered.native.manager.adapters
    finally:
        await recovered.stop()
