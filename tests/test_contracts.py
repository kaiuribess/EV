import asyncio
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from evplatform.core.bus import EventBus, SafetyHalt, SchemaRegistry
from evplatform.core.health import CircuitBreaker, QuotaExceeded, SequenceGuard, TokenBucket, reconnect_delay
from evplatform.core.models import Envelope, GameState, Settlement, utcnow
from evplatform.core.normalization import MarketMatcher, VerifiedAlias
from evplatform.core.plugins import Manifest, Plugin, PluginRegistry
from evplatform.plugins.demo import demo_frame, sample_markets


def test_prop_identity():
    a, _, b = sample_markets()[-3:]
    assert not MarketMatcher.equivalent(a, b)
    for change in [{"player_id": "another"}, {"statistic": "rebounds"},
                   {"line": Decimal("28.5")}, {"event_id": "other"},
                   {"settlement": Settlement(scope="player", participation="must_start")}]:
        assert not MarketMatcher.equivalent(a, a.model_copy(update=change))
    assert MarketMatcher.equivalent(a, a.model_copy(update={"id": "other-provider-id"}))


def test_verified_alias_and_quarantine():
    matcher = MarketMatcher()
    assert matcher.resolve("team", "new-api", "1") is None
    assert len(matcher.quarantine) == 1
    matcher.add_alias(VerifiedAlias("team", "new-api", "1", "canonical-1", "manual roster verification"))
    assert matcher.resolve("team", "new-api", "1") == "canonical-1"
    with pytest.raises(ValueError):
        matcher.add_alias(VerifiedAlias("team", "new-api", "1", "different", "source"))


def test_canonical_collision(market):
    matcher = MarketMatcher()
    matcher.register(market)
    with pytest.raises(ValueError):
        matcher.register(market.model_copy(update={"settlement": Settlement(overtime="excluded")}))


def test_replay_determinism():
    first = [(schema, data.model_dump(mode="json")) for schema, data in demo_frame(23)]
    second = [(schema, data.model_dump(mode="json")) for schema, data in demo_frame(23)]
    assert first == second
    assert first != [(schema, data.model_dump(mode="json")) for schema, data in demo_frame(24)]


def test_sequence_gap_and_snapshot():
    sequence = SequenceGuard()
    assert not sequence.delta(1)
    sequence.snapshot(5)
    assert sequence.delta(6)
    assert not sequence.delta(8)
    assert not sequence.synchronized
    assert not sequence.delta(9)
    sequence.snapshot(10)
    assert sequence.delta(11)
    sequence.disconnect()
    assert not sequence.delta(12)


def test_quota_and_circuit():
    now = [0.]
    bucket = TokenBucket(2, 1, lambda: now[0])
    bucket.consume(2)
    with pytest.raises(QuotaExceeded):
        bucket.consume()
    now[0] = 1
    bucket.consume()
    breaker = CircuitBreaker(2, 10, lambda: now[0])
    breaker.failure()
    breaker.failure()
    assert not breaker.available
    now[0] = 12
    assert breaker.available
    breaker.success()
    assert breaker.available
    assert 0 < reconnect_delay(100) <= 30


async def test_typed_bus_and_observer_overflow():
    schemas = SchemaRegistry()
    schemas.register("game", 1, GameState)
    halts = []
    bus = EventBus(schemas, halts.append)
    critical = bus.subscribe("critical", ["game"], critical=True, capacity=1)
    observer = bus.subscribe("observer", ["game"], capacity=1)
    data = GameState(event_id="e", score={"home": 1}, status="live", source_time=utcnow())
    event = Envelope(schema_name="game", source="test", payload=data)
    await bus.publish(event)
    assert (await critical.get()).payload == data
    await bus.publish(event)
    assert bus.dropped["observer"] == 1
    with pytest.raises(SafetyHalt):
        await bus.publish(event)
    assert halts
    with pytest.raises(ValueError):
        await bus.publish(event.model_copy(update={"schema_name": "unknown"}))


async def test_stopped_critical_consumer_cannot_halt_unrelated_ingestion():
    schemas = SchemaRegistry()
    schemas.register("game", 1, GameState)
    halts = []
    bus = EventBus(schemas, halts.append)
    stopped = bus.subscribe("paper-only", ["game"], critical=True, capacity=1)
    bus.unsubscribe(stopped)
    event = Envelope(schema_name="game", source="test", payload=GameState(
        event_id="e", score={"home": 1}, status="live", source_time=utcnow()))
    for _ in range(4):
        await bus.publish(event)
    assert bus.counts["game"] == 4
    assert not halts and not bus.subscribers


def test_schema_migration():
    class V1(BaseModel):
        score: int
    class V2(BaseModel):
        home_score: int
    registry = SchemaRegistry()
    registry.register("score", 1, V1)
    registry.register("score", 2, V2)
    registry.add_migration("score", 1, lambda d: {"home_score": d["score"]})
    assert registry.migrate("score", 1, 2, {"score": 5}).home_score == 5
    with pytest.raises(ValueError):
        registry.migrate("score", 2, 1, {"home_score": 5})


def test_plugin_contracts_and_dependencies():
    class Sample(Plugin):
        manifest = Manifest(name="sample", kind="input", description="test", dependencies=["missing"])
    registry = PluginRegistry()
    registry.register(Sample())
    with pytest.raises(ValueError, match="Missing"):
        registry.validate_dependencies()
    with pytest.raises(ValueError, match="Duplicate"):
        registry.register(Sample())
    class Bad(Plugin):
        manifest = Manifest(name="bad", kind="input", description="bad", contract_version=2)
    with pytest.raises(ValueError):
        registry.register(Bad())
    class Healthy(Plugin):
        manifest = Manifest(name="healthy", kind="observer", description="healthy")
    registry.register(Healthy(), enabled=True)
    assert registry.compatible_startup_order() == ["healthy"]
    assert registry.records["healthy"].enabled
    assert registry.records["sample"].health["status"] == "blocked"

