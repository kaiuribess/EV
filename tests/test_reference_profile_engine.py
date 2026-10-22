"""Full paper/audit flow under unvalidated research priors; no network requests."""
from datetime import timedelta
import json
from pathlib import Path
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from evplatform.core.config import Settings
from evplatform.core.models import Candidate, Envelope, utcnow
from evplatform.core.launch_setup import launch_setup
from evplatform.core.profile_import import compile_priors
from evplatform.core.risk import RiskRejected
from evplatform.engine import Engine
from evplatform.plugins.demo import demo_frame


def research_config():
    return compile_priors(json.loads((Path(__file__).parents[1] / "config/book_weight_priors_2026-10-20.json").read_text()))


async def test_repeated_no_candidate_evaluations_do_not_flood_durable_audit(tmp_path):
    engine = Engine(Settings(_env_file=None, database_path=str(tmp_path / "bounded-audit.db"),
        mode="paper", sample_feed_enabled=False))
    market = next(payload for schema, payload in demo_frame(0, origin=utcnow())
                  if schema == "sports.market")
    await engine.start()
    try:
        for index in range(100):
            await engine._record_signal(market, "NO SIGNAL", f"Still no complete reference {index}")
        await engine.signal_queue.join()
        await engine.audit_queue.join()
        await engine.storage.queue.join()
        count = (await (await engine.storage.connection.execute(
            "SELECT COUNT(*) FROM events WHERE schema_name='signal.observation'")).fetchone())[0]
        assert count == 1
        assert engine.metrics.counters["coalesced_no_signal_evaluations"] == 99
        identity, status, previous, quote_count = engine._signal_audit_state[market.id]
        engine._signal_audit_state[market.id] = identity, status, previous - 61, quote_count
        await engine._record_signal(market, "NO SIGNAL", "Periodic unchanged-state evidence")
        await engine.signal_queue.join()
        await engine.audit_queue.join()
        await engine.storage.queue.join()
        count = (await (await engine.storage.connection.execute(
            "SELECT COUNT(*) FROM events WHERE schema_name='signal.observation'")).fetchone())[0]
        assert count == 2
        candidate = Candidate(strategy="positive_ev", market_id=market.id, venue="paper:test",
            outcome=market.outcomes[0], price=Decimal(".45"), fair_probability=Decimal(".60"),
            ev_per_contract=Decimal(".15"), roi=Decimal(".333"), max_quantity=1,
            book_sequence=1, references=(), calculated_at=utcnow() - timedelta(seconds=1),
            quote_age_ms=100, synthetic=True)
        await engine._record_signal(market, "POSITIVE EV", "A new candidate", (candidate,))
        await engine._record_signal(market, "NO SIGNAL", "Target was withdrawn")
        await engine._record_signal(market, "NO SIGNAL", "Repeated withdrawn state")
        await engine.signal_queue.join()
        await engine.audit_queue.join()
        await engine.storage.queue.join()
        count = (await (await engine.storage.connection.execute(
            "SELECT COUNT(*) FROM events WHERE schema_name='signal.observation'")).fetchone())[0]
        assert count == 4
        episode = await (await engine.storage.connection.execute(
            "SELECT ended_at FROM signal_episodes WHERE market_id=?", (market.id,))).fetchone()
        assert episode and episode[0] is not None
    finally:
        await engine.stop()


async def test_research_priors_record_rejections_and_both_methods_without_routing(tmp_path):
    engine = Engine(Settings(_env_file=None, database_path=str(tmp_path / "research.db"),
        mode="paper", auto_trade=True, pricing=research_config().model_copy(update={"devig_method": "proportional"})))
    engine.registry.records["synthetic_schedule"].enabled = False
    await engine.start()
    try:
        for schema, payload in demo_frame(0, origin=utcnow(), pregame_reference_research=True):
            await engine.bus.publish(Envelope(schema_name=schema, source="offline-research-test", payload=payload))
        await engine.process_queue.join()
        await engine.signal_queue.join()
        await engine.audit_queue.join()
        await engine.storage.queue.join()
        candidates = list(engine.opportunities.values())
        assert candidates and any(c.ev_per_contract > 0 for c in candidates)
        assert all(c.reference_profile_id == "nba_core_props" for c in candidates)
        assert all(c.sensitivity_fair_probability is not None and not c.execution_eligible for c in candidates)
        assert all("TTL" in " ".join(c.ineligibility_reasons) for c in candidates)
        assert not engine.risk.orders and not engine.risk.native_orders
        assert not engine.risk.kill_switch, engine.risk.kill_reason
        assert all(engine.candidate_public(c)["status"] == "RESEARCH ONLY" for c in candidates)
        with pytest.raises(RiskRejected, match="eligible"):
            engine.revalidate(candidates[0])
        with pytest.raises(RiskRejected, match="Research-only"):
            candidate = candidates[0]
            engine.risk.reserve(candidate, engine.markets[candidate.market_id],
                engine.book(candidate.market_id, candidate.venue, candidate.outcome), "paper")
        rows = await (await engine.storage.connection.execute(
            "SELECT data FROM events WHERE schema_name='signal.observation' ORDER BY seq DESC LIMIT 20")).fetchall()
        records = [json.loads(await engine.storage.decode_event_data(row[0]))["payload"] for row in rows]
        assert any(r["reference_quotes"] and r["status"] == "NO SIGNAL" for r in records)
        assert any(r["reference_quotes"] and r["status"] == "RESEARCH ONLY" for r in records)
        assert all(r["model"] == "power" for r in records if r["candidates"])
        assert all(r["pricing_policy"]["reference_profile_mode"] == "strict" for r in records)
    finally:
        await engine.stop()


async def test_pregame_expiration_scheduled_even_without_further_price_updates(tmp_path):
    engine = Engine(Settings(_env_file=None, database_path=str(tmp_path / "unused.db"), pricing=research_config()))
    market = next(p for schema, p in demo_frame(0, origin=utcnow(), pregame_reference_research=True)
                  if schema == "sports.market" and p.market_type == "player_prop")
    market = market.model_copy(update={"start_time": utcnow() + timedelta(seconds=2)})
    engine.markets[market.id] = market
    engine.schedule_expiration(market.id)
    assert engine.expirations[0][0] == pytest.approx(market.start_time.timestamp() + .001)


async def test_conflicting_equal_observation_is_sticky_until_newer_complete_quote(tmp_path):
    engine = Engine(Settings(_env_file=None, database_path=str(tmp_path / "unused.db"), pricing=research_config()))
    frame = demo_frame(0, origin=utcnow() - timedelta(seconds=1), pregame_reference_research=True)
    market = next(p for schema, p in frame if schema == "sports.market" and p.market_type == "player_prop")
    quote = next(p for schema, p in frame if schema == "odds.quote" and p.market_id == market.id)
    engine.markets[market.id] = market
    engine.recalculate = AsyncMock()
    async def ingest(q):
        await engine.process_event(Envelope(schema_name="odds.quote", source="test", payload=q))
    await ingest(quote)
    conflicting = quote.model_copy(update={"decimal_odds": {"over": Decimal("1.5"), "under": Decimal("2.6")}})
    await ingest(conflicting)
    key = quote.provider, quote.bookmaker
    assert not engine.quotes[market.id][key].observation_complete
    # Repeating the last packet or restoring the old price at the same clock
    # cannot erase an unresolved contradiction.
    await ingest(conflicting)
    await ingest(quote)
    assert engine.quotes[market.id][key].verification_reason == "conflicting_same_observation"
    recovered = quote.model_copy(update={"source_observed_at": utcnow(), "received_at": utcnow()})
    await ingest(recovered)
    assert engine.quotes[market.id][key].observation_complete


def test_imported_profiles_are_an_explicit_pending_launch_requirement(tmp_path):
    engine = Engine(Settings(_env_file=None, database_path=str(tmp_path / "unused.db"), pricing=research_config()))
    report = launch_setup(engine)
    requirement = next(r for r in report["requirements"] if r["code"] == "pricing.profiles")
    assert requirement["status"] == "pending"
    assert not report["safe_to_arm"] and not report["activation"]["available"]


async def test_missing_clock_downgrade_cannot_recover_with_delayed_old_observation(tmp_path):
    engine = Engine(Settings(_env_file=None, database_path=str(tmp_path / "unused.db"), pricing=research_config()))
    frame = demo_frame(0, origin=utcnow() - timedelta(seconds=1), pregame_reference_research=True)
    market = next(p for schema, p in frame if schema == "sports.market" and p.market_type == "player_prop")
    quote = next(p for schema, p in frame if schema == "odds.quote" and p.market_id == market.id)
    quote = quote.model_copy(update={"source_time": quote.source_time - timedelta(hours=1)})
    engine.markets[market.id] = market
    engine.recalculate = AsyncMock()
    async def ingest(q):
        await engine.process_event(Envelope(schema_name="odds.quote", source="test", payload=q))
    await ingest(quote)
    degraded = quote.model_copy(update={"source_observed_at": None, "observation_verified": False,
        "observation_complete": False, "market_status_verified": False})
    await ingest(degraded)
    key = quote.provider, quote.bookmaker
    assert not engine.quotes[market.id][key].observation_verified
    await ingest(quote)
    assert not engine.quotes[market.id][key].observation_complete
    assert "verification_downgrade" in engine.quotes[market.id][key].verification_reason
    recovered = quote.model_copy(update={"source_observed_at": utcnow(), "received_at": utcnow()})
    await ingest(recovered)
    assert engine.quotes[market.id][key].observation_complete
