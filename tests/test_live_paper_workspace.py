"""Offline live-input paper checks; every incoming transport is a local fixture."""
import asyncio
from decimal import Decimal

from evplatform.core.config import Settings
from evplatform.core.models import Envelope, utcnow
from evplatform.engine import Engine


def test_paper_workspace_can_disable_sample_input_without_touching_native_gate(tmp_path):
    engine = Engine(Settings(_env_file=None, database_path=str(tmp_path / "paper.db"),
        mode="paper", sample_feed_enabled=False, auto_trade=False, live_enabled=False))
    assert not engine.registry.records["synthetic_schedule"].enabled
    assert engine.registry.records["paper_execution"].enabled
    assert engine.state()["sample_feed_enabled"] is False
    assert engine.state()["synthetic"] is True  # Simulated fills remain possible.
    assert engine.settings.live_enabled is False


async def test_real_reference_fixture_and_paper_book_keep_fill_simulated(tmp_path, market, quotes, book):
    engine = Engine(Settings(_env_file=None, database_path=str(tmp_path / "offline-live-paper.db"),
        mode="paper", sample_feed_enabled=False, auto_trade=True, live_enabled=False))
    await engine.start()
    try:
        assert not engine.registry.records["synthetic_schedule"].enabled
        assert not engine.markets and not engine.risk.orders
        now = utcnow()
        local_market = market.model_copy(update={"id": "offline:read-only-reference",
            "event_id": "offline:fixture", "original_ids": {"offline": "fixture"}})
        await engine.bus.publish(Envelope(schema_name="sports.market", source="offline-fixture", payload=local_market))
        for quote in quotes:
            live_reference = quote.model_copy(update={"market_id": local_market.id,
                "provider": "offline-real-reference", "source_time": now, "received_at": now,
                "synthetic": False})
            await engine.bus.publish(Envelope(schema_name="odds.quote", source="offline-fixture", payload=live_reference))
        await asyncio.sleep(.1)
        assert not engine.risk.orders  # Real reference prices alone cannot become an executable offer.
        paper_book = book.model_copy(update={"market_id": local_market.id, "venue": "offline_paper",
            "synthetic": True, "source_time": now, "received_at": now})
        await engine.bus.publish(Envelope(schema_name="exchange.book", source="offline-fixture", payload=paper_book))
        for _ in range(80):
            await asyncio.sleep(.025)
            if any(order.filled_quantity > 0 for order in engine.risk.orders.values()):
                break
        assert all(not quote.synthetic for quote in engine.quotes[local_market.id].values())
        assert engine.books[local_market.id][("offline_paper", paper_book.outcome)].synthetic
        assert engine.risk.orders
        assert all(order.synthetic for order in engine.risk.orders.values())
        assert any(order.filled_quantity > Decimal(0) for order in engine.risk.orders.values())
        assert engine.settings.live_enabled is False
    finally:
        await engine.stop()
