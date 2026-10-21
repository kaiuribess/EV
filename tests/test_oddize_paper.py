"""Offline Oddize-to-Parlay paper comparison; no provider account requests."""
import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from evplatform.core.config import Settings
from evplatform.core.models import Envelope, Market, ProviderFrame, Quote, Settlement, utcnow
from evplatform.core.profile_import import compile_priors
from evplatform.core.pricing import NoSignal
from evplatform.engine import Engine
from evplatform.integrations.base import ProviderEnvelope
from evplatform.plugins.oddize_paper import OddizePaperNormalizer
from evplatform.plugins.parlay_paper import ParlayPaperNormalizer


def market(*, home="Atlanta Hawks", away="Boston Celtics", start=None):
    return Market(id="paper:parlay:h2h:test", event_id="paper:parlay:event:test",
        event_name=f"{away} at {home}", sport="basketball", league="NBA",
        participants=(home, away), start_time=start or utcnow() + timedelta(hours=2),
        market_type="moneyline", outcomes=("HOME", "AWAY"),
        settlement=Settlement(period="full_game", scope="event",
            overtime="paper_assumed_included", participation="not_applicable",
            void_rule="paper_completed_game_assumption_only", push_rule="not_applicable"),
        extensions={"paper_only": True})


def snapshot(when, *, start=None, home="ATL", away="BOS", book="circa",
             home_price=-110, away_price=100, missing_away=False,
             line=None, market_key="ml", complete=True, side_skew=0):
    def price(team, value, stamp):
        return {"book": book, "offer_type": market_key, "team": team,
            "line": line, "american_odds": value,
            "ts": int(stamp.timestamp()), "timestamp": stamp.replace(microsecond=0).isoformat()}
    odds = [price(home, home_price, when)]
    if not missing_away:
        odds.append(price(away, away_price, when + timedelta(seconds=side_skew)))
    return ProviderFrame(provider_id="oddize_rest", kind="snapshot", received_at=utcnow(),
        snapshot_complete=complete, metadata={"sport": "nba", "delta": False},
        payload={"events": [{"event_id": "oddize-event-1", "sport": "nba",
            "team1": away, "team2": home, "start_date": (
                start or utcnow() + timedelta(hours=2, minutes=10)).isoformat(),
            "odds": odds}]})


def test_exact_nba_match_preserves_observation_and_excludes_stale():
    source = OddizePaperNormalizer()
    expected = market()
    source.register_market(expected)
    now = utcnow() - timedelta(seconds=20)
    quotes = source.apply_frame(snapshot(now - timedelta(seconds=10), start=expected.start_time))
    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.market_id == expected.id and quote.bookmaker == "circa"
    assert quote.decimal_odds["HOME"] == Decimal("1.909090909090909090909090909")
    assert quote.status == "active" and quote.quality == 1
    assert quote.observation_verified and quote.observation_complete
    assert not quote.market_status_verified and not quote.synthetic
    assert abs((quote.source_time - (now - timedelta(seconds=10))).total_seconds()) < 1
    expired = source.reconcile(now + timedelta(seconds=601))
    assert len(expired) == 1 and expired[0].status == "suspended"
    assert expired[0].quality == 0 and expired[0].source_time == quote.source_time


def test_unknown_team_wrong_event_and_ambiguous_match_are_quarantined():
    source = OddizePaperNormalizer()
    expected = market()
    source.register_market(expected)
    assert source.apply_frame(snapshot(utcnow(), home="UNK", start=expected.start_time)) == []
    assert source.apply_frame(snapshot(utcnow(), home="LAL", away="GSW", start=expected.start_time)) == []
    assert source.apply_frame(snapshot(utcnow(), start=expected.start_time + timedelta(minutes=16))) == []
    source.markets["paper:second"] = expected.model_copy(update={"id": "paper:second"})
    assert source.apply_frame(snapshot(utcnow(), start=expected.start_time)) == []
    assert source.ambiguous > 0


def test_additional_real_book_codes_are_exactly_normalized_for_paper():
    expected = market()
    for source_code, canonical in (("czr", "caesars"), ("b365", "bet365"),
                                   ("mgm", "betmgm")):
        source = OddizePaperNormalizer()
        source.register_market(expected)
        quote = source.apply_frame(snapshot(utcnow(), start=expected.start_time,
            book=source_code))[0]
        assert quote.bookmaker == canonical and quote.status == "active"


def test_incomplete_mismatched_or_conflicting_observations_cannot_price():
    source = OddizePaperNormalizer()
    expected = market()
    source.register_market(expected)
    now = utcnow() - timedelta(seconds=20)
    assert not source.apply_frame(snapshot(now, start=expected.start_time, missing_away=True))
    assert not source.apply_frame(snapshot(now, start=expected.start_time, side_skew=1))
    assert not source.apply_frame(snapshot(now, start=expected.start_time, line=1.5))
    first = source.apply_frame(snapshot(now, start=expected.start_time))
    assert first and first[0].status == "active"
    conflict = source.apply_frame(snapshot(now, start=expected.start_time, home_price=-120))
    assert len(conflict) == 1 and conflict[0].status == "suspended"
    assert conflict[0].quality == 0
    recovered = source.apply_frame(snapshot(now + timedelta(seconds=2), start=expected.start_time,
        home_price=-120))
    assert len(recovered) == 1 and recovered[0].status == "active"
    assert source.apply_frame(snapshot(now + timedelta(seconds=3), start=expected.start_time,
        complete=False))[0].status == "withdrawn"


def test_snapshot_removal_and_feed_invalidation_withdraw():
    source = OddizePaperNormalizer()
    expected = market()
    source.register_market(expected)
    source.apply_frame(snapshot(utcnow(), start=expected.start_time))
    updates = source.apply_frame(ProviderFrame(provider_id="oddize_rest", kind="feed_invalidated",
        received_at=utcnow(), payload={}))
    assert len(updates) == 1 and updates[0].status == "withdrawn"
    assert not source.quotes


def test_parlay_board_replacement_drops_old_market_mapping():
    source = OddizePaperNormalizer()
    expected = market()
    source.register_market(expected)
    source.apply_frame(snapshot(utcnow(), start=expected.start_time))
    removed = source.invalidate_parlay_board()
    assert len(removed) == 1 and removed[0].status == "withdrawn"
    assert not source.markets and not source.quotes
    replacement = expected.model_copy(update={"id": "paper:replacement"})
    restored = source.register_market(replacement)
    assert len(restored) == 1 and restored[0].market_id == replacement.id


def pricing():
    raw = json.loads((Path(__file__).parents[1] / "config" /
        "book_weight_priors_2026-10-20.json").read_text(encoding="utf-8"))
    base = compile_priors(raw)
    return type(base).model_validate({**base.model_dump(), "enabled_reference_profiles":
        [*base.enabled_reference_profiles, "nba_nfl_mainlines"]})


async def test_oddize_circa_joins_paper_blend_without_double_counting_pinnacle(tmp_path):
    engine = Engine(Settings(database_path=str(tmp_path / "paper.db"), mode="paper",
        sample_feed_enabled=False, pricing=pricing()))
    strategy = engine.registry.records["parlay_paper_ev"].plugin
    await strategy.start(engine.context("parlay_paper_ev"))
    now = utcnow() - timedelta(seconds=20)
    start = now + timedelta(hours=2)
    rows = [{"event_id": "parlay-game-1", "home_team": "Atlanta Hawks",
        "away_team": "Boston Celtics", "commence_time": start.isoformat(),
        "kind": "game", "market_key": "h2h", "last_update": now.isoformat(),
        "bookmaker": book, "home_ml": home, "away_ml": away}
        for book, home, away in [("pinnacle", -120, 110),
            ("draftkings", -115, 105), ("fanduel", 110, -125)]]
    normalizer = ParlayPaperNormalizer()
    updates = normalizer.apply(ProviderEnvelope("parlay_game_ws", "initial_state", {"data": rows},
        now, 1, published_at=now, snapshot_complete=True, metadata={"sport": "basketball_nba"}))
    updates += normalizer.apply(ProviderEnvelope("parlay_game_ws", "heartbeat",
        {"bookmaker_freshness": [{"bookmaker": book, "polled_at_ms": int(now.timestamp() * 1000),
            "status": "observed"} for book in ("pinnacle", "draftkings", "fanduel")]},
        utcnow(), 2, metadata={"sport": "basketball_nba"}))
    parlay_market = next(item for item in updates if isinstance(item, Market))
    parlay_quotes = list({item.bookmaker: item for item in updates if isinstance(item, Quote)}.values())
    oddize = OddizePaperNormalizer()
    oddize.register_market(parlay_market)
    oddize.apply_frame(snapshot(now, start=start, book="circa"))
    oddize.apply_frame(ProviderFrame(provider_id="oddize_rest", kind="snapshot",
        received_at=utcnow(), snapshot_complete=True, metadata={"sport": "nba"},
        payload={"events": [snapshot(now + timedelta(seconds=1), start=start,
            book="pin").payload["events"][0], snapshot(now, start=start,
            book="circa").payload["events"][0]]}))
    oddize_quotes = list(oddize.quotes.values())
    home_book = next(item for item in reversed(updates) if item.__class__.__name__ == "OrderBook"
        and item.venue == "parlay_paper:fanduel" and item.outcome == "HOME")
    candidate = strategy._candidate(parlay_market, parlay_quotes + oddize_quotes,
        home_book, engine.settings.pricing)
    assert {ref["bookmaker"] for ref in candidate.references} == {
        "pinnacle", "draftkings", "circa"}
    assert len(candidate.references) == 3
    assert {ref["bookmaker"]: ref["weight"] for ref in candidate.references}["circa"] == "35"
    assert not candidate.live_execution_allowed
    circa_only = [q for q in oddize_quotes if q.bookmaker == "circa"]
    no_pinnacle = strategy._candidate(parlay_market,
        [q for q in parlay_quotes if q.bookmaker != "pinnacle"] + circa_only,
        home_book, engine.settings.pricing)
    assert {ref["bookmaker"] for ref in no_pinnacle.references} == {"draftkings", "circa"}
    pinnacle_target = next(item for item in reversed(updates)
        if item.__class__.__name__ == "OrderBook" and item.venue == "parlay_paper:pinnacle"
        and item.outcome == "HOME")
    target = strategy._candidate(parlay_market, parlay_quotes + circa_only,
        pinnacle_target, engine.settings.pricing)
    assert "pinnacle" not in {ref["bookmaker"] for ref in target.references}
    assert "circa" in {ref["bookmaker"] for ref in target.references}
    duplicate = parlay_quotes + [q for q in oddize_quotes if q.bookmaker == "circa"]
    conflict = next(q for q in oddize_quotes if q.bookmaker == "pinnacle")
    conflict = conflict.model_copy(update={"source_time": next(q.source_time for q in parlay_quotes
        if q.bookmaker == "pinnacle"), "decimal_odds": {"HOME": Decimal("2.1"), "AWAY": Decimal("1.8")}})
    with pytest.raises(NoSignal, match="Conflicting same-clock"):
        strategy._candidate(parlay_market, duplicate + [conflict], home_book,
            engine.settings.pricing)


async def test_oddize_paper_plugin_rejects_production_mode(tmp_path):
    engine = Engine(Settings(database_path=str(tmp_path / "live.db"), mode="production",
        sample_feed_enabled=False, live_enabled=True))
    await engine.registry.start("oddize_paper_input", engine.context("oddize_paper_input"))
    assert engine.registry.records["oddize_paper_input"].health["status"] == "error"


async def test_cached_oddize_snapshot_joins_later_parlay_board_on_event_bus(tmp_path):
    engine = Engine(Settings(database_path=str(tmp_path / "join.db"), mode="paper",
        sample_feed_enabled=False, auto_trade=False, pricing=pricing()))
    await engine.start()
    try:
        engine.registry.configure("parlay_paper_input", {"sports": ["basketball_nba"]})
        await engine.toggle_plugin("parlay_paper_input", True)
        await engine.toggle_plugin("oddize_paper_input", True)
        now = utcnow() - timedelta(seconds=3)
        start = now + timedelta(hours=2)
        await engine.bus.publish(Envelope(schema_name="provider.frame", source="oddize_rest",
            payload=snapshot(now, start=start)))
        rows = [{"event_id": "parlay-game-1", "home_team": "Atlanta Hawks",
            "away_team": "Boston Celtics", "commence_time": start.isoformat(),
            "kind": "game", "market_key": "h2h", "last_update": now.isoformat(),
            "bookmaker": book, "home_ml": home, "away_ml": away}
            for book, home, away in [("pinnacle", -120, 110),
                ("draftkings", -115, 105), ("fanduel", 110, -125)]]
        await engine.bus.publish(Envelope(schema_name="provider.frame", source="parlay_game_ws",
            payload=ProviderFrame(provider_id="parlay_game_ws", kind="initial_state",
                received_at=now, published_at=now, snapshot_complete=True,
                metadata={"sport": "basketball_nba"}, payload={"data": rows})))
        for _ in range(50):
            await asyncio.sleep(.02)
            if any(q.provider == "oddize_paper_input" for values in engine.quotes.values()
                   for q in values.values()):
                break
        matched = [q for values in engine.quotes.values() for q in values.values()
            if q.provider == "oddize_paper_input"]
        assert len(matched) == 1 and matched[0].bookmaker == "circa"
        assert matched[0].status == "active" and not matched[0].market_status_verified
        assert engine.quote_public(matched[0])["paper_comparison_usable"]
        assert not engine.risk.orders and not engine.settings.live_enabled
        await engine.bus.publish(Envelope(schema_name="provider.frame", source="oddize_rest",
            payload=ProviderFrame(provider_id="oddize_rest", kind="feed_invalidated",
                received_at=utcnow(), payload={})))
        await engine.registry.records["oddize_paper_input"].plugin.queue.join()
        await engine.process_queue.join()
        assert next(q for values in engine.quotes.values() for q in values.values()
            if q.provider == "oddize_paper_input").status == "withdrawn"
    finally:
        await engine.stop()
