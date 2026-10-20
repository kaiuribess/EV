import asyncio
from datetime import timedelta
from pathlib import Path

from evplatform.core.bus import EventBus, SchemaRegistry
from evplatform.core.config import Settings
from evplatform.core.models import Envelope, GameState, Quote, utcnow
from evplatform.engine import Engine
from evplatform.plugins.replay import replay_jsonl


async def test_jsonl_replay_keeps_old_observation_clock(tmp_path):
    registry = SchemaRegistry()
    registry.register("sports.game_state", 1, GameState)
    bus = EventBus(registry, lambda reason: None)
    queue = bus.subscribe("test", ["sports.game_state"], critical=True)
    old = utcnow() - timedelta(days=1)
    payload = GameState(event_id="fixture:event", status="live", score={"home": 3},
                        source_time=old, received_at=old, synthetic=True)
    envelope = Envelope(id="replay-1", schema_name="sports.game_state", source="fixture",
                        payload=payload, emitted_at=old)
    fixture = tmp_path / "replay.jsonl"
    fixture.write_text(envelope.model_dump_json() + "\n", encoding="utf-8")
    assert await replay_jsonl(fixture, bus, pace_seconds=.001) == 1
    event = await queue.get()
    assert event.payload.source_time == old
    assert not event.payload.fresh(1000)
    queue.task_done()


async def test_automatic_safety_halt_persists_across_restart(tmp_path):
    settings = Settings(database_path=str(tmp_path / "halt.db"), auto_trade=False)
    engine = Engine(settings)
    await engine.start()
    engine.risk.halt("Invalid external data")
    await engine.stop()
    recovered = Engine(settings.model_copy(update={"auto_trade": True}))
    await recovered.start()
    try:
        assert recovered.risk.kill_switch
        assert not recovered.risk.auto_trade
        await recovered.control(kill_switch=False)
        assert not recovered.risk.kill_switch
    finally:
        await recovered.stop()


async def test_stalled_feed_signals_expire(tmp_path):
    settings = Settings(database_path=str(tmp_path / "expire.db"), demo_interval=.1)
    settings.pricing.max_quote_age_ms = 200
    settings.pricing.max_book_age_ms = 200
    settings.pricing.min_references = 1
    engine = Engine(settings)
    await engine.start()
    try:
        await asyncio.sleep(.15)
        await engine.registry.stop("synthetic_schedule")
        await asyncio.sleep(.7)
        assert not engine.opportunities
        assert all(s["status"] == "NO SIGNAL" for s in engine.signals.values())
    finally:
        await engine.stop()
