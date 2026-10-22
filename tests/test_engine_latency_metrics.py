"""Processing latency must not be inflated by retained paper quote age."""
from datetime import timedelta
from unittest.mock import AsyncMock

from evplatform.core.config import Settings
from evplatform.core.models import Envelope, utcnow
from evplatform.engine import Engine
from evplatform.plugins.demo import demo_frame


async def test_republished_paper_quote_uses_bus_clock_not_old_receipt(tmp_path):
    engine = Engine(Settings(_env_file=None, database_path=str(tmp_path / "unused.db")))
    frame = demo_frame(0, origin=utcnow())
    market = next(payload for schema, payload in frame if schema == "sports.market")
    quote = next(payload for schema, payload in frame
                 if schema == "odds.quote" and payload.market_id == market.id)
    engine.markets[market.id] = market
    engine.recalculate = AsyncMock()
    retained = quote.model_copy(update={"provider": "parlay_paper_input",
        "received_at": utcnow() - timedelta(seconds=30), "synthetic": False})
    await engine.process_event(Envelope(schema_name="odds.quote", source="parlay_paper_input",
                                        payload=retained))
    assert len(engine.metrics.samples["bus_publish_to_calculation_ms"]) == 1
    assert engine.metrics.samples["bus_publish_to_calculation_ms"][0] < 1000
    assert not engine.metrics.samples["receipt_to_calculation_ms"]

    actual = retained.model_copy(update={"provider": "parlay_rest"})
    await engine.process_event(Envelope(schema_name="odds.quote", source="parlay_rest", payload=actual))
    assert len(engine.metrics.samples["receipt_to_calculation_ms"]) == 1
    assert engine.metrics.samples["receipt_to_calculation_ms"][0] >= 30000
