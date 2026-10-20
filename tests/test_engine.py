import asyncio
from datetime import timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from evplatform.api import create_app
from evplatform.core.config import RiskConfig, Settings
from evplatform.core.models import Envelope, Fill, OrderStatus, utcnow
from evplatform.core.pricing import fair_value, opportunity
from evplatform.core.risk import RiskEngine, RiskRejected
from evplatform.engine import Engine
from evplatform.plugins.demo import demo_frame


def test_risk_gates_and_one_use_authority(market, quotes, book, pricing):
    candidate = opportunity(market, fair_value(market, quotes, pricing), book, pricing)
    risk = RiskEngine(RiskConfig())
    with pytest.raises(RiskRejected, match="disabled"):
        risk.reserve(candidate, market, book, "paper")
    risk.auto_trade = True
    order = risk.reserve(candidate, market, book, "paper")
    assert order.reserved_cost <= risk.config.max_order_stake
    with pytest.raises(RiskRejected, match="Duplicate"):
        risk.reserve(candidate, market, book, "paper")
    token = risk.authority.issue(order.id)
    assert risk.authority.consume(token, order.id)
    assert not risk.authority.consume(token, order.id)
    risk.halt("test")
    with pytest.raises(RiskRejected, match="kill"):
        risk.reserve(candidate, market, book, "paper")


@pytest.mark.parametrize("limit", ["max_total_exposure", "max_event_exposure", "max_venue_exposure", "max_correlated_exposure"])
def test_position_limits(limit, market, quotes, book, pricing):
    risk = RiskEngine(RiskConfig(**{limit: D(".10")}))
    risk.auto_trade = True
    candidate = opportunity(market, fair_value(market, quotes, pricing), book, pricing)
    with pytest.raises(RiskRejected, match="exposure"):
        risk.reserve(candidate, market, book, "paper")


async def test_entire_paper_flow_and_restart(tmp_path):
    settings = Settings(database_path=str(tmp_path / "audit.db"), mode="paper", auto_trade=True, demo_interval=.5)
    engine = Engine(settings)
    await engine.start()
    try:
        for _ in range(60):
            await asyncio.sleep(.05)
            if any(o.status == OrderStatus.FILLED for o in engine.risk.orders.values()):
                break
        assert engine.markets and any(m.player for m in engine.markets.values())
        assert any(c.ev_per_contract > 0 for c in engine.opportunities.values())
        assert any(c.ev_per_contract < 0 for c in engine.opportunities.values())
        assert engine.risk.orders
        assert any(o.filled_quantity > 0 for o in engine.risk.orders.values())
        assert not engine.risk.kill_switch, engine.risk.kill_reason
        assert engine.state()["metrics"]["latency"]["internal_processing_ms"]["count"] > 0
        count = len(engine.risk.orders)
        await engine.control(kill_switch=True)
        assert all(o.status not in {OrderStatus.OPEN, OrderStatus.PARTIAL} for o in engine.risk.orders.values())
        await asyncio.sleep(.2)
        assert len(engine.risk.orders) == count
    finally:
        await engine.stop()
    recovered = Engine(Settings(database_path=settings.database_path, mode="paper"))
    await recovered.start()
    try:
        assert len(recovered.risk.orders) == count
        assert recovered.risk.kill_switch
        assert not recovered.risk.auto_trade
    finally:
        await recovered.stop()


async def prepare_engine(tmp_path, market, quotes, book):
    engine = Engine(Settings(database_path=str(tmp_path / "audit.db"), mode="paper"))
    await engine.storage.open()
    engine.registry.records["positive_ev"].enabled = True
    engine.markets[market.id] = market
    engine.quotes[market.id] = {(q.provider, q.bookmaker): q for q in quotes}
    engine.books[market.id] = {(book.venue, book.outcome): book}
    engine.risk.auto_trade = True
    return engine


async def test_partial_fill_duplicate_and_cancellation_race(tmp_path, market, quotes, book, pricing):
    engine = await prepare_engine(tmp_path, market, quotes, book)
    try:
        c = opportunity(market, fair_value(market, quotes, pricing), book, pricing)
        order = await engine.oms.execute(c, market, book, "paper")
        assert order.status == OrderStatus.OPEN
        await engine.oms.tick()
        partial = engine.risk.orders[order.id]
        assert partial.status == OrderStatus.PARTIAL
        cancelled = await engine.oms.cancel(order.id)
        assert cancelled.status == OrderStatus.CANCELLED
        fill = Fill(id="late-fill", order_id=order.id, venue=order.venue, quantity=1, price=order.price, fee=D(0), synthetic=True)
        await engine.oms.apply_fill(fill)
        after = engine.risk.orders[order.id]
        await engine.oms.apply_fill(fill)
        assert engine.risk.orders[order.id].filled_quantity == after.filled_quantity
        assert after.status == OrderStatus.CANCELLED
        assert engine.risk.exposures() > 0
    finally:
        await engine.storage.close()


async def test_unknown_outcome_not_retried(tmp_path, market, quotes, book, pricing):
    engine = await prepare_engine(tmp_path, market, quotes, book)
    calls = []
    async def uncertain(order, authorization):
        calls.append(order.id)
        raise TimeoutError("simulated ambiguous transport")
    engine.paper.submit = uncertain
    try:
        candidate = opportunity(market, fair_value(market, quotes, pricing), book, pricing)
        order = await engine.oms.execute(candidate, market, book, "paper")
        assert order.status == OrderStatus.UNKNOWN
        assert engine.risk.kill_switch
        assert engine.risk.exposures() > 0
        await engine.oms.cancel_all()
        assert engine.risk.orders[order.id].status == OrderStatus.UNKNOWN
        with pytest.raises(RiskRejected):
            await engine.oms.execute(candidate, market, book, "paper")
        assert len(calls) == 1
    finally:
        await engine.storage.close()


async def test_stale_reference_blocks_orders(tmp_path, market, quotes, book, pricing):
    engine = await prepare_engine(tmp_path, market, quotes, book)
    try:
        candidate = opportunity(market, fair_value(market, quotes, pricing), book, pricing)
        engine.quotes[market.id] = {key: q.model_copy(update={"source_time": utcnow() - timedelta(seconds=40)})
                                   for key, q in engine.quotes[market.id].items()}
        with pytest.raises(RiskRejected):
            await engine.oms.execute(candidate, market, book, "paper")
        assert not engine.risk.orders
    finally:
        await engine.storage.close()


async def test_stale_after_durable_intent_rejected(tmp_path, market, quotes, book, pricing):
    engine = await prepare_engine(tmp_path, market, quotes, book)
    original_save = engine.storage.save_order
    async def save_and_invalidate(order):
        await original_save(order)
        engine.books[market.id][(book.venue, book.outcome)] = book.model_copy(update={"synchronized": False})
    engine.storage.save_order = save_and_invalidate
    try:
        candidate = opportunity(market, fair_value(market, quotes, pricing), book, pricing)
        order = await engine.oms.execute(candidate, market, book, "paper")
        assert order.status == OrderStatus.REJECTED
        assert not engine.paper.live_orders
    finally:
        await engine.storage.close()


def test_api_default_disabled_security_and_config(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "api.db")))
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert not state["auto_trade"]
        assert client.post("/api/control", json={"auto_trade": True}).status_code == 403
        assert client.post("/api/control", headers={"X-EV-Control": "local", "Origin": "https://evil.example"},
                           json={"auto_trade": True}).status_code == 403
        headers = {"X-EV-Control": "local"}
        assert client.post("/api/control", headers=headers, json={"auto_trade": True}).status_code == 200
        assert client.post("/api/settings", headers=headers, json={"min_ev": ".03"}).status_code == 200
        assert client.post("/api/settings", headers=headers, json={"max_order_stake": -5}).status_code == 422
        assert client.post("/api/plugins/parlay_ws/toggle", headers=headers, json={"enabled": True}).status_code == 200
        plugin = next(p for p in client.get("/api/state").json()["plugins"] if p["manifest"]["name"] == "parlay_ws")
        assert not plugin["enabled"]
        assert plugin["health"]["status"] == "blocked"
        with client.websocket_connect("/ws") as socket:
            data = socket.receive_json()
            assert data["type"] == "state"
        assert client.post("/api/control", headers=headers, json={"kill_switch": True}).status_code == 200

