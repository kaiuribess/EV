"""Independent regression checks for the central execution and accounting boundaries."""
import asyncio
from datetime import timedelta
from decimal import Decimal as D

import pytest

from evplatform.core.config import Settings
from evplatform.core.models import Envelope, Fill, OrderStatus, SettlementResult, utcnow
from evplatform.core.plugins import Manifest, StrategyPlugin
from evplatform.core.pricing import NoSignal, fair_value, opportunity
from evplatform.core.risk import RiskRejected
from evplatform.engine import Engine


async def prepared(tmp_path, market, quotes, book):
    engine = Engine(Settings(database_path=str(tmp_path / "review.db"), mode="paper"))
    await engine.storage.open()
    engine.registry.records["positive_ev"].enabled = True
    engine.markets[market.id] = market
    engine.quotes[market.id] = {(q.provider, q.bookmaker): q for q in quotes}
    engine.books[market.id] = {(book.venue, book.outcome): book}
    engine.risk.auto_trade = True
    return engine


def candidate_for(market, quotes, book, pricing):
    return opportunity(market, fair_value(market, quotes, pricing), book, pricing)


def settlement_for(market):
    return SettlementResult(market_id=market.id, outcome_payouts={"home": D(1), "away": D(0)},
                            synthetic=True, evidence="Deterministic test result")


async def test_fill_dedup_survives_restart(tmp_path, market, quotes, book, pricing):
    engine = await prepared(tmp_path, market, quotes, book)
    try:
        order = await engine.oms.execute(candidate_for(market, quotes, book, pricing), market, book, "paper")
        fill = Fill(id="durable-fill-id", order_id=order.id, venue=order.venue,
                    quantity=2, price=order.price, fee=D(".01"), synthetic=True)
        async with engine.oms.lock:
            await engine.oms.apply_fill(fill)
        before = engine.risk.orders[order.id]
    finally:
        await engine.storage.close()
    recovered = await prepared(tmp_path, market, quotes, book)
    try:
        await recovered.oms.recover()
        async with recovered.oms.lock:
            await recovered.oms.apply_fill(fill)
        after = recovered.risk.orders[order.id]
        assert after.filled_quantity == before.filled_quantity == 2
        assert after.fees == before.fees
        assert after.status == OrderStatus.CANCELLED
        assert recovered.risk.exposures() == after.average_fill_price * 2 + after.fees
    finally:
        await recovered.storage.close()


async def test_atomic_reservation_prevents_concurrent_duplicate_orders(tmp_path, market, quotes, book, pricing):
    engine = await prepared(tmp_path, market, quotes, book)
    try:
        candidate = candidate_for(market, quotes, book, pricing)
        results = await asyncio.gather(*(engine.oms.execute(candidate, market, book, "paper")
                                         for _ in range(12)), return_exceptions=True)
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert all(isinstance(result, RiskRejected) for result in results if isinstance(result, Exception))
        assert len(engine.risk.orders) == len(engine.paper.live_orders) == 1
        assert engine.risk.exposures() <= engine.risk.config.max_order_stake
    finally:
        await engine.storage.close()


async def test_kill_switch_during_intent_persistence_prevents_submission(tmp_path, market, quotes, book, pricing):
    engine = await prepared(tmp_path, market, quotes, book)
    save = engine.storage.save_order
    async def stop_after_save(order):
        await save(order)
        engine.risk.halt("Test emergency stop during durable intent")
    engine.storage.save_order = stop_after_save
    try:
        order = await engine.oms.execute(candidate_for(market, quotes, book, pricing), market, book, "paper")
        assert order.status == OrderStatus.REJECTED
        assert not engine.paper.live_orders
        assert engine.risk.exposures() == 0
    finally:
        await engine.storage.close()


@pytest.mark.parametrize("source_time", [None, utcnow() - timedelta(seconds=60)])
async def test_new_delivery_does_not_refresh_unknown_or_old_source_time(tmp_path, market, quotes, book, source_time):
    engine = await prepared(tmp_path, market, quotes, book)
    engine.risk.auto_trade = False
    try:
        for quote in quotes:
            incoming = quote.model_copy(update={"source_time": source_time, "received_at": utcnow()})
            # Repeated payloads arrive after the original has expired.
            engine.quotes[market.id].pop((quote.provider, quote.bookmaker))
            await engine.process_event(Envelope(schema_name="odds.quote", source=quote.provider, payload=incoming))
        assert engine.signals[market.id]["status"] == "NO SIGNAL"
        assert not engine.opportunities
    finally:
        await engine.storage.close()


async def test_future_clock_observation_does_not_poison_future_updates(tmp_path, market, quotes, book):
    engine = await prepared(tmp_path, market, quotes, book)
    engine.risk.auto_trade = False
    try:
        quote = quotes[0]
        future = quote.model_copy(update={"source_time": utcnow() + timedelta(hours=1)})
        await engine.process_event(Envelope(schema_name="odds.quote", source=quote.provider, payload=future))
        valid = quote.model_copy(update={"source_time": utcnow()})
        await engine.process_event(Envelope(schema_name="odds.quote", source=quote.provider, payload=valid))
        stored = engine.quotes[market.id][(quote.provider, quote.bookmaker)]
        assert stored.source_time == valid.source_time
        assert stored.fresh(5000)
    finally:
        await engine.storage.close()


class IndependentStrategy(StrategyPlugin):
    manifest = Manifest(name="independent_test", kind="strategy", description="Independent forecast",
                        input_schemas=["exchange.book@1"], output_schemas=["trade.candidate@1"],
                        sports=["basketball"], markets=["moneyline"])

    def __init__(self, candidate):
        self.candidate = candidate.model_copy(update={"strategy": self.manifest.name})

    async def evaluate(self, event, view):
        return [self.candidate]

    def revalidate(self, candidate, view):
        return self.candidate


async def test_independent_strategy_uses_central_risk_and_paper_execution(tmp_path, market, quotes, book, pricing):
    engine = await prepared(tmp_path, market, quotes, book)
    strategy = IndependentStrategy(candidate_for(market, quotes, book, pricing))
    engine.registry.register(strategy, enabled=True)
    engine.registry.records["positive_ev"].enabled = False
    try:
        await engine.recalculate(market.id)
        assert all(c.strategy == strategy.manifest.name for c in engine.opportunities.values())
        assert engine.risk_queue.qsize() == 1
        event = await engine.risk_queue.get()
        order = await engine.oms.execute(event.payload, market, book, "paper")
        assert order.strategy == strategy.manifest.name
        assert order.status == OrderStatus.OPEN
        engine.risk_queue.task_done()
    finally:
        await engine.storage.close()


@pytest.mark.parametrize("book_update", [{"synchronized": False}, {"status": "suspended"},
                                         {"source_time": None}, {"source_time": utcnow() - timedelta(minutes=1)}])
async def test_central_risk_rejects_bad_book_even_with_custom_strategy(tmp_path, market, quotes, book, pricing, book_update):
    engine = await prepared(tmp_path, market, quotes, book)
    strategy = IndependentStrategy(candidate_for(market, quotes, book, pricing))
    engine.registry.register(strategy, enabled=True)
    engine.books[market.id][(book.venue, book.outcome)] = book.model_copy(update=book_update)
    try:
        with pytest.raises(RiskRejected):
            await engine.oms.execute(strategy.candidate, market, book, "paper")
        assert not engine.paper.live_orders
    finally:
        await engine.storage.close()


async def test_strategy_cannot_change_candidate_identity_during_revalidation(tmp_path, market, quotes, book, pricing):
    engine = await prepared(tmp_path, market, quotes, book)
    strategy = IndependentStrategy(candidate_for(market, quotes, book, pricing))
    engine.registry.register(strategy, enabled=True)
    original = strategy.candidate
    strategy.candidate = original.model_copy(update={"venue": "unrelated_venue"})
    try:
        with pytest.raises(RiskRejected):
            await engine.oms.execute(original, market, book, "paper")
        assert not engine.paper.live_orders
    finally:
        await engine.storage.close()


@pytest.mark.parametrize("fill_update", [{"venue": "wrong_venue"}, {"price": D(".99")}, {"synthetic": False}])
async def test_fill_must_match_order_contract(tmp_path, market, quotes, book, pricing, fill_update):
    engine = await prepared(tmp_path, market, quotes, book)
    try:
        order = await engine.oms.execute(candidate_for(market, quotes, book, pricing), market, book, "paper")
        fill = Fill(id="invalid-fill", order_id=order.id, venue=order.venue, quantity=1,
                    price=order.price, fee=D(0), synthetic=True).model_copy(update=fill_update)
        async with engine.oms.lock:
            await engine.oms.apply_fill(fill)
        assert engine.risk.orders[order.id].filled_quantity == 0
        assert engine.risk.kill_switch
        assert fill.id not in await engine.storage.load_fill_ids()
    finally:
        await engine.storage.close()


async def test_settlement_is_idempotent_and_releases_filled_exposure(tmp_path, market, quotes, book, pricing):
    engine = await prepared(tmp_path, market, quotes, book)
    try:
        order = await engine.oms.execute(candidate_for(market, quotes, book, pricing), market, book, "paper")
        async with engine.oms.lock:
            await engine.oms.apply_fill(Fill(id="settled-fill", order_id=order.id, venue=order.venue,
                                            quantity=2, price=order.price, fee=D(".01"), synthetic=True))
        result = settlement_for(market)
        await engine.apply_settlement(result)
        expected = D(2) - order.price * 2 - D(".01")
        assert engine.risk.daily_realized == expected
        assert engine.risk.exposures() == 0
        await engine.apply_settlement(result)
        assert engine.risk.daily_realized == expected
        saved = await engine.storage.load_config()
        assert saved["accounting"]["settled_order_ids"] == [order.id]
        with pytest.raises(RiskRejected, match="settled"):
            engine.revalidate(candidate_for(market, quotes, book, pricing))
    finally:
        await engine.storage.close()


async def test_settlement_cannot_release_unknown_order_exposure(tmp_path, market, quotes, book, pricing):
    engine = await prepared(tmp_path, market, quotes, book)
    async def timeout(order, authorization):
        raise TimeoutError("Ambiguous acknowledgement")
    engine.paper.submit = timeout
    try:
        order = await engine.oms.execute(candidate_for(market, quotes, book, pricing), market, book, "paper")
        assert order.status == OrderStatus.UNKNOWN
        with pytest.raises((RiskRejected, ValueError)):
            await engine.apply_settlement(settlement_for(market))
        assert engine.risk.exposures() > 0
        assert order.id not in engine.risk.settled
    finally:
        await engine.storage.close()


async def test_post_settlement_fill_requires_reconciliation(tmp_path, market, quotes, book, pricing):
    engine = await prepared(tmp_path, market, quotes, book)
    try:
        order = await engine.oms.execute(candidate_for(market, quotes, book, pricing), market, book, "paper")
        await engine.apply_settlement(settlement_for(market))
        before = engine.risk.orders[order.id]
        async with engine.oms.lock:
            await engine.oms.apply_fill(Fill(id="post-settlement-fill", order_id=order.id, venue=order.venue,
                                            quantity=1, price=order.price, fee=D(0), synthetic=True))
        assert engine.risk.orders[order.id].filled_quantity == before.filled_quantity
        assert engine.risk.kill_switch
    finally:
        await engine.storage.close()


async def test_disabled_execution_plugin_cannot_receive_orders(tmp_path, market, quotes, book, pricing):
    engine = await prepared(tmp_path, market, quotes, book)
    try:
        await engine.registry.stop("paper_execution")
        # An unrelated control toggle must not reactivate a disabled execution plugin.
        await engine.control(auto_trade=True)
        with pytest.raises(RiskRejected):
            await engine.oms.execute(candidate_for(market, quotes, book, pricing), market, book, "paper")
        assert not engine.paper.live_orders
    finally:
        await engine.storage.close()


def test_fair_value_from_other_market_is_not_usable(market, quotes, book, pricing):
    fair = fair_value(market, quotes, pricing).model_copy(update={"market_id": "unrelated-market"})
    with pytest.raises(NoSignal):
        opportunity(market, fair, book, pricing)
