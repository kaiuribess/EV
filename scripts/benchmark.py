"""Run a measured local synthetic session; emits JSON, never connects to remote services."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from evplatform.core.config import Settings
from evplatform.engine import Engine


async def benchmark(seconds):
    engine = Engine(Settings(database_path=f"data/benchmark-{uuid4().hex}.db", mode="paper", auto_trade=True))
    await engine.start()
    try:
        await asyncio.sleep(seconds)
        state = engine.state()
        report = {
            "duration_seconds": seconds, "synthetic": True,
            "market_count": len(state["markets"]), "quote_count": len(state["quotes"]),
            "positive_opportunities": sum(c["status"] == "POSITIVE EV" for c in state["opportunities"]),
            "negative_opportunities": sum(c["status"] == "NEGATIVE EV" for c in state["opportunities"]),
            "orders": len(engine.risk.orders),
            "orders_with_fills": sum(o.filled_quantity > 0 for o in engine.risk.orders.values()),
            "kill_switch": engine.risk.kill_switch, "metrics": state["metrics"],
            "notes": "Windows local synthetic run. Not provider network latency, live fill speed, or profitability evidence.",
        }
    finally:
        await engine.stop()
    Path("artifacts").mkdir(exist_ok=True)
    Path("artifacts/benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=15)
    asyncio.run(benchmark(parser.parse_args().seconds))

