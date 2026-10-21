"""Live-input paper mode uses observed prices but never claims executable offers."""
import asyncio
import json
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from evplatform.core.config import Settings
from evplatform.core.models import Envelope, OrderStatus, ProviderFrame, Quote, OrderBook, utcnow
from evplatform.engine import Engine
from evplatform.integrations.base import ProviderEnvelope
from evplatform.core.profile_import import compile_priors
from evplatform.core.pricing import NoSignal
from evplatform.plugins.parlay_paper import ParlayPaperEV, ParlayPaperNormalizer
from evplatform.plugins.external import ProviderBridge
from evplatform.integrations.parlay import ParlayGameBoardWebSocket


SPORT = "basketball_nba"


def shared_pricing():
    path = Path(__file__).parents[1] / "config" / "book_weight_priors_2026-10-20.json"
    pricing = compile_priors(json.loads(path.read_text(encoding="utf-8")))
    return type(pricing).model_validate({**pricing.model_dump(),
        "enabled_reference_profiles": [*pricing.enabled_reference_profiles, "nba_nfl_mainlines"]})


def frame(*, complete=True, age=0, rows=None, kind="initial_state"):
    now = utcnow()
    observed = now - timedelta(seconds=age)
    base = {"event_id": "parlay-game-1", "home_team": "Atlanta Hawks",
            "away_team": "Boston Celtics", "commence_time": (now + timedelta(hours=4)).isoformat(),
            "kind": "game", "market_key": "h2h", "last_update": observed.isoformat()}
    if rows is None:
        rows = [
            {**base, "bookmaker": "pinnacle", "home_ml": -120, "away_ml": 110},
            {**base, "bookmaker": "draftkings", "home_ml": -115, "away_ml": 105},
            {**base, "bookmaker": "fanduel", "home_ml": 110, "away_ml": -125},
        ]
    return ProviderEnvelope("parlay_game_ws", kind, {"data": rows}, now, 1,
        published_at=now, snapshot_complete=complete, metadata={"sport": SPORT})


def heartbeat(*, books=("pinnacle", "draftkings", "fanduel"), poll_age=0):
    now = utcnow()
    polled = int((now - timedelta(seconds=poll_age)).timestamp() * 1000)
    return ProviderEnvelope("parlay_game_ws", "heartbeat", {"bookmaker_freshness": [
        {"bookmaker": book, "polled_at_ms": polled, "status": "observed"} for book in books]},
        now, 2, published_at=now, metadata={"sport": SPORT})


def normalized():
    normalizer = ParlayPaperNormalizer()
    updates = normalizer.apply(frame())
    updates += normalizer.apply(heartbeat())
    market = next(item for item in updates if item.__class__.__name__ == "Market")
    quotes = list({item.bookmaker: item for item in updates if isinstance(item, Quote)}.values())
    books = list({(item.venue, item.outcome): item for item in updates if isinstance(item, OrderBook)}.values())
    return normalizer, market, quotes, books


def test_only_complete_baseline_and_exact_opposite_sides_make_paper_market():
    normalizer = ParlayPaperNormalizer()
    assert normalizer.apply(frame(complete=False)) == []
    assert normalizer.apply(frame(kind="odds_update")) == []
    updates = normalizer.apply(frame())
    assert len([item for item in updates if isinstance(item, Quote)]) == 3
    assert all(item.synthetic is False for item in updates if isinstance(item, Quote))
    assert all(item.synthetic is True for item in updates if isinstance(item, OrderBook))
    assert all(item.status == "suspended" for item in updates if isinstance(item, OrderBook))
    assert all(item.status == "active" for item in normalizer.apply(heartbeat()) if isinstance(item, OrderBook))
    assert all(item.fee.source.startswith("hypothetical") for item in updates if isinstance(item, OrderBook))
    assert all(item.extensions["paper_only"] for item in updates if item.__class__.__name__ == "Market")


def test_stale_or_missing_outcome_cannot_create_signal():
    assert not ParlayPaperNormalizer().apply(frame(age=601))
    single = frame().payload["data"][0].copy()
    single.pop("away_ml")
    assert not ParlayPaperNormalizer().apply(frame(rows=[single]))
    normalizer, market, quotes, books = normalized()
    withdrawn = normalizer.apply(ProviderEnvelope("parlay_game_ws", "feed_invalidated",
        {"sport": SPORT}, utcnow(), 2, metadata={"sport": SPORT}))
    assert any(isinstance(item, Quote) and item.status == "withdrawn" for item in withdrawn)
    assert any(isinstance(item, OrderBook) and item.status == "suspended" for item in withdrawn)
    assert not normalizer.active


def test_opposing_sides_with_different_price_clocks_are_rejected():
    rows = frame().payload["data"]
    one = dict(rows[0])
    one.pop("away_ml")
    other = dict(rows[0])
    other.pop("home_ml")
    other["last_update"] = (utcnow() - timedelta(seconds=45)).isoformat()
    normalizer = ParlayPaperNormalizer()
    updates = normalizer.apply(frame(rows=[one, other]))
    assert not any(isinstance(item, Quote) for item in updates)
    assert normalizer.rejected == 1


def test_missing_or_old_book_poll_suspends_paper_capacity():
    normalizer = ParlayPaperNormalizer()
    normalizer.apply(frame())
    good = normalizer.apply(heartbeat())
    assert any(isinstance(item, OrderBook) and item.status == "active" for item in good)
    stale = normalizer.apply(heartbeat(books=("draftkings", "fanduel"), poll_age=181))
    assert all(item.quality == 0 for item in stale if isinstance(item, Quote))
    assert all(item.status == "suspended" for item in stale if isinstance(item, OrderBook))


async def test_one_sport_reconnect_keeps_other_paper_baseline():
    """A scoped upstream gap must not erase healthy sport sockets."""
    normalizer = ParlayPaperNormalizer()
    normalizer.baselines.update({"basketball_nba", "americanfootball_nfl"})
    published = asyncio.Event()
    hold_stream = asyncio.Event()

    class Bus:
        async def publish(self, event):
            if event.schema_name == "provider.frame":
                frame = event.payload
                normalizer.apply(ProviderEnvelope(frame.provider_id, frame.kind,
                    frame.payload, frame.received_at, 0, metadata=frame.metadata))
                published.set()

    class Client:
        config = SimpleNamespace(sports=["basketball_nba", "americanfootball_nfl"])

        async def stream_updates(self):
            yield ProviderEnvelope("parlay_game_ws", "feed_invalidated",
                {"sport": "americanfootball_nfl"}, utcnow(), 1,
                metadata={"sport": "americanfootball_nfl"})
            await hold_stream.wait()

    engine = SimpleNamespace(registry=SimpleNamespace(records={
        "parlay_paper_input": SimpleNamespace(enabled=True)}),
        quotes={}, metrics=SimpleNamespace(counters=Counter()))
    bridge = ProviderBridge(ParlayGameBoardWebSocket, engine)
    bridge.context = SimpleNamespace(bus=Bus())
    bridge.client = Client()
    bridge.mapper = lambda raw: []
    task = asyncio.create_task(bridge._run())
    try:
        await asyncio.wait_for(published.wait(), 1)
        await asyncio.sleep(0)
        assert normalizer.baselines == {"basketball_nba"}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_observed_per_outcome_parlay_shape_pairs_only_exact_event_book_and_side():
    now = utcnow()
    base = {"event_id": "2026-10-22_Memphis_Grizzlies_Utah_Jazz",
        "home_team": "Memphis Grizzlies", "away_team": "Utah Jazz",
        "commence_time": (now + timedelta(hours=4)).isoformat(),
        "kind": "game", "market_key": "h2h", "bookmaker": "fanduel",
        "last_update": int(now.timestamp() * 1000)}
    home = {**base, "team": "Memphis Grizzlies", "price": -120, "over_price": -120}
    away = {**base, "team": "Utah Jazz", "price": 110, "over_price": 110}
    normalizer = ParlayPaperNormalizer()
    assert not [item for item in normalizer.apply(frame(rows=[home])) if isinstance(item, Quote)]
    updates = normalizer.apply(frame(rows=[away], kind="odds_update"))
    quote = next(item for item in updates if isinstance(item, Quote))
    assert quote.bookmaker == "fanduel"
    assert quote.decimal_odds["HOME"] > Decimal("1.8")
    assert quote.decimal_odds["AWAY"] > Decimal("2")
    invalid = {**away, "event_id": "different-game"}
    assert not [item for item in ParlayPaperNormalizer().apply(frame(rows=[home, invalid]))
                if isinstance(item, Quote)]


async def test_independent_reference_and_negative_signal(tmp_path):
    engine = Engine(Settings(database_path=str(tmp_path / "reference.db"), mode="paper",
                             sample_feed_enabled=False, pricing=shared_pricing()))
    strategy = engine.registry.records["parlay_paper_ev"].plugin
    await strategy.start(engine.context("parlay_paper_ev"))
    _, market, quotes, books = normalized()
    home = next(book for book in books if book.venue == "parlay_paper:fanduel" and book.outcome == "HOME")
    candidate = strategy._candidate(market, quotes, home, engine.settings.pricing)
    assert candidate.ev_per_contract > Decimal(".015")
    assert candidate.execution_eligible
    assert candidate.synthetic and not candidate.live_execution_allowed
    assert {ref["bookmaker"] for ref in candidate.references} == {"pinnacle", "draftkings"}
    assert {ref["provider"] for ref in candidate.references} == {"parlay_paper_input"}
    assert {ref["bookmaker"]: ref["weight"] for ref in candidate.references} == {"pinnacle": "35", "draftkings": "10"}
    away = next(book for book in books if book.venue == "parlay_paper:fanduel" and book.outcome == "AWAY")
    negative = strategy._candidate(market, quotes, away, engine.settings.pricing)
    assert negative.ev_per_contract < 0
    assert not negative.execution_eligible
    with pytest.raises(NoSignal, match="Insufficient fresh independent references|Required reference-profile anchor unavailable"):
        strategy._candidate(market, [q for q in quotes if q.bookmaker != "pinnacle"], home,
                            engine.settings.pricing)
    aged_target = [q.model_copy(update={"source_time": utcnow() - timedelta(seconds=601)})
                   if q.bookmaker == "fanduel" else q for q in quotes]
    with pytest.raises(NoSignal, match="Target sportsbook quote is expired"):
        strategy._candidate(market, aged_target, home, engine.settings.pricing)


async def test_existing_parlay_frame_to_risk_and_simulated_fill(tmp_path):
    engine = Engine(Settings(database_path=str(tmp_path / "flow.db"), mode="paper",
        sample_feed_enabled=False, auto_trade=True,
        pricing=shared_pricing()))
    await engine.start()
    try:
        engine.registry.configure("parlay_paper_input", {"sports": [SPORT]})
        await engine.toggle_plugin("parlay_paper_input", True)
        source = frame()
        await engine.bus.publish(Envelope(schema_name="provider.frame", source="parlay_game_ws",
            payload=ProviderFrame(provider_id=source.provider_id, kind=source.kind,
                payload=source.payload, received_at=source.received_at,
                published_at=source.published_at, snapshot_complete=True, metadata=source.metadata)))
        pulse = heartbeat()
        await engine.bus.publish(Envelope(schema_name="provider.frame", source="parlay_game_ws",
            payload=ProviderFrame(provider_id=pulse.provider_id, kind=pulse.kind,
                payload=pulse.payload, received_at=pulse.received_at,
                published_at=pulse.published_at, metadata=pulse.metadata)))
        for _ in range(80):
            await asyncio.sleep(.05)
            if any(o.filled_quantity > 0 for o in engine.risk.orders.values()):
                break
        assert len(engine.markets) == 1
        assert len(engine.quotes[next(iter(engine.markets))]) == 3
        assert engine.risk.orders, engine.signals
        assert all(order.synthetic and order.mode == "paper" for order in engine.risk.orders.values())
        assert any(order.status in {OrderStatus.PARTIAL, OrderStatus.FILLED} for order in engine.risk.orders.values())
        assert not engine.risk.kill_switch, engine.risk.kill_reason
        public = engine.state()["opportunities"]
        assert any(item["paper_only"] and item["target_quote_age_ms"] is not None for item in public)
        assert engine.state()["metrics"]["latency"]["internal_processing_ms"]["count"] > 0
        await engine.bus.publish(Envelope(schema_name="provider.frame", source="parlay_game_ws",
            payload=ProviderFrame(provider_id="parlay_game_ws", kind="feed_invalidated",
                payload={"sport": SPORT}, received_at=utcnow(), metadata={"sport": SPORT})))
        await engine.registry.records["parlay_paper_input"].plugin.queue.join()
        await engine.process_queue.join()
        assert not engine.opportunities
        assert all(q.status == "withdrawn" for quotes in engine.quotes.values() for q in quotes.values())
    finally:
        await engine.stop()


async def test_paper_input_cannot_start_in_live_mode(tmp_path):
    engine = Engine(Settings(database_path=str(tmp_path / "live.db"), mode="production",
        sample_feed_enabled=False, live_enabled=True))
    engine.registry.configure("parlay_paper_input", {"sports": [SPORT]})
    await engine.registry.start("parlay_paper_input", engine.context("parlay_paper_input"))
    record = engine.registry.records["parlay_paper_input"]
    assert not record.enabled
    assert record.health["status"] == "error"
