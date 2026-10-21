"""Run a measured local synthetic session; emits JSON, never connects to remote services."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from evplatform.core.config import Settings
from evplatform.core.profile_import import compile_priors
from evplatform.engine import Engine


async def benchmark(seconds, reference_priors=False):
    engine = Engine(Settings(database_path=f"data/benchmark-{uuid4().hex}.db", mode="paper", auto_trade=True))
    if reference_priors:
        source = Path(__file__).resolve().parents[1] / "config/book_weight_priors_2026-10-20.json"
        engine.settings.pricing = compile_priors(json.loads(source.read_text(encoding="utf-8-sig")))
        engine.registry.configure("positive_ev", engine.settings.pricing.model_dump(mode="json"))
        config = engine.registry.records["synthetic_schedule"].config
        engine.registry.configure("synthetic_schedule", {**config, "pregame_reference_research": True})
    await engine.start()
    try:
        await asyncio.sleep(seconds)
        state = engine.state()
        report = {
            "duration_seconds": seconds, "synthetic": True,
            "market_count": len(state["markets"]), "quote_count": len(state["quotes"]),
            "positive_opportunities": sum(c["status"] == "POSITIVE EV" for c in state["opportunities"]),
            "negative_opportunities": sum(c["status"] == "NEGATIVE EV" for c in state["opportunities"]),
            "research_only_opportunities": sum(c["status"] == "RESEARCH ONLY" for c in state["opportunities"]),
            "reference_priors": reference_priors,
            "orders": len(engine.risk.orders),
            "orders_with_fills": sum(o.filled_quantity > 0 for o in engine.risk.orders.values()),
            "kill_switch": engine.risk.kill_switch, "metrics": state["metrics"],
            "notes": "Windows local synthetic run. Not provider network latency, live fill speed, or profitability evidence.",
        }
    finally:
        await engine.stop()
    Path("artifacts").mkdir(exist_ok=True)
    target = "artifacts/benchmark-reference-priors.json" if reference_priors else "artifacts/benchmark.json"
    Path(target).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--reference-priors", action="store_true")
    args = parser.parse_args()
    asyncio.run(benchmark(args.seconds, args.reference_priors))

