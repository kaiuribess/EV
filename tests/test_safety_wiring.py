"""Integration checks at durability and fill boundaries; no external trading."""
from decimal import Decimal as D

import pytest

from evplatform.core.config import Settings
from evplatform.core.models import Fill, Level, OrderStatus, utcnow
from evplatform.core.pricing import fair_value, opportunity
from evplatform.engine import Engine


async def prepared(tmp_path, market, quotes, book):
    engine = Engine(Settings(database_path=str(tmp_path / "boundary.db"), mode="paper"))
    await engine.storage.open()
    engine.markets[market.id] = market
    engine.quotes[market.id] = {(q.provider, q.bookmaker): q for q in quotes}
    engine.books[market.id] = {(book.venue, book.outcome): book}
    engine.risk.auto_trade = True
    return engine


def candidate(engine, market, quotes, book):
    return opportunity(market, fair_value(market, quotes, engine.settings.pricing), book, engine.settings.pricing)


@pytest.mark.parametrize("barrier", [OrderStatus.INTENT, OrderStatus.SUBMITTING])
async def test_lowered_stake_limit_blocks_transmission_after_durable_barrier(tmp_path, market, quotes, book, barrier):
    engine = await prepared(tmp_path, market, quotes, book)
    original = engine.storage.save_order

    async def lower_limit(order):
        await original(order)
        if order.status == barrier:
            engine.risk.config = engine.settings.risk = engine.risk.config.model_copy(update={"max_order_stake": D("1")})

    engine.storage.save_order = lower_limit
    try:
        order = await engine.oms.execute(candidate(engine, market, quotes, book), market, book, "paper")
        assert order.status == OrderStatus.REJECTED
        assert not engine.paper.live_orders
        assert engine.risk.exposures() == 0
    finally:
        await engine.storage.close()


async def test_market_rule_change_during_durable_intent_blocks_transmission(tmp_path, market, quotes, book):
    engine = await prepared(tmp_path, market, quotes, book)
    original = engine.storage.save_order

    async def change_identity(order):
        await original(order)
        if order.status == OrderStatus.INTENT:
            engine.markets[market.id] = market.model_copy(update={"line": D("1")})

    engine.storage.save_order = change_identity
    try:
        order = await engine.oms.execute(candidate(engine, market, quotes, book), market, book, "paper")
        assert order.status == OrderStatus.REJECTED
        assert "identity" in order.reason.lower()
        assert not engine.paper.live_orders
    finally:
        await engine.storage.close()


async def test_standing_order_checks_original_limit_economics_before_filling(tmp_path, market, quotes, book):
    engine = await prepared(tmp_path, market, quotes, book)
    try:
        order = await engine.oms.execute(candidate(engine, market, quotes, book), market, book, "paper")
        moved_quotes = [q.model_copy(update={
            "decimal_odds": {"home": D(1) / D(".46"), "away": D(1) / D(".54")},
            "source_time": utcnow(),
        }) for q in quotes]
        engine.quotes[market.id] = {(q.provider, q.bookmaker): q for q in moved_quotes}
        # The new offer still has a positive edge, but filling at .45 does not.
        engine.books[market.id][(book.venue, book.outcome)] = book.model_copy(update={
            "asks": (Level(price=D(".40"), quantity=50),), "source_time": utcnow(), "sequence": 2})
        await engine.oms.tick()
        assert engine.risk.orders[order.id].status == OrderStatus.CANCELLED
        assert engine.risk.orders[order.id].filled_quantity == 0
    finally:
        await engine.storage.close()


async def test_daily_loss_stop_cancels_standing_residual_before_another_fill(tmp_path, market, quotes, book):
    engine = await prepared(tmp_path, market, quotes, book)
    try:
        order = await engine.oms.execute(candidate(engine, market, quotes, book), market, book, "paper")
        engine.risk.daily_realized = -engine.risk.config.daily_loss_limit
        await engine.oms.tick()
        assert engine.risk.orders[order.id].status == OrderStatus.CANCELLED
        assert engine.risk.orders[order.id].filled_quantity == 0
    finally:
        await engine.storage.close()


async def test_late_fill_after_cancel_accounts_fill_and_halts_conflicting_position(tmp_path, market, quotes, book):
    engine = await prepared(tmp_path, market, quotes, book)
    try:
        first = await engine.oms.execute(candidate(engine, market, quotes, book), market, book, "paper")
        await engine.oms.cancel(first.id)
        opposite_book = book.model_copy(update={"venue": "other_paper", "outcome": "away",
            "asks": (Level(price=D(".30"), quantity=50),), "source_time": utcnow()})
        engine.books[market.id][(opposite_book.venue, opposite_book.outcome)] = opposite_book
        other = await engine.oms.execute(candidate(engine, market, quotes, opposite_book), market, opposite_book, "paper")
        assert other.status == OrderStatus.OPEN
        late = Fill(id="late-cancel-fill", order_id=first.id, venue=first.venue,
                    quantity=1, price=first.price, fee=D(0), synthetic=True)
        async with engine.oms.lock:
            await engine.oms.apply_fill(late)
        assert engine.risk.orders[first.id].filled_quantity == 1
        assert late.id in await engine.storage.load_fill_ids()
        assert engine.risk.kill_switch
        assert "conflicting position" in engine.risk.kill_reason
        await engine.oms.tick()
        assert engine.risk.orders[other.id].status == OrderStatus.CANCELLED
    finally:
        await engine.storage.close()
