"""Deterministic synthetic fixtures, never used as fallback for a live plugin."""
import asyncio
import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import Envelope, FeeSchedule, GameState, Level, Market, OrderBook, Quote, Settlement, utcnow
from ..core.plugins import InputPlugin, Manifest


class DemoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval: float = Field(default=.5, ge=.05, le=30)
    seed: int = 73
    simulate_faults: bool = True
    pregame_reference_research: bool = False


def sample_markets(origin=None):
    origin = origin or datetime(2026, 10, 20, 23, tzinfo=timezone.utc)
    rows = [
        ("nba-bos-ny", "basketball", "NBA", "Boston Celtics at New York Knicks", ("Boston Celtics", "New York Knicks")),
        ("nfl-sea-sf", "football", "NFL", "Seattle Seahawks at San Francisco 49ers", ("Seattle Seahawks", "San Francisco 49ers")),
        ("nhl-bos-nyr", "hockey", "NHL", "Boston Bruins at New York Rangers", ("Boston Bruins", "New York Rangers")),
    ]
    result = []
    for idx, (event, sport, league, title, participants) in enumerate(rows):
        for mtype in ("moneyline", "total"):
            result.append(Market(
                id=f"demo:{event}:{mtype}", event_id=f"demo:{event}", event_name=title, sport=sport, league=league,
                participants=participants, start_time=origin + timedelta(minutes=idx * 20),
                status="live", market_type=mtype, line=Decimal("221.5" if idx == 0 else "47.5" if idx == 1 else "5.5") if mtype == "total" else None,
                outcomes=("home", "away") if mtype == "moneyline" else ("over", "under"),
                settlement=Settlement(), original_ids={"synthetic": f"{event}-{mtype}"},
            ))
    for player, pid, stat, line in [
        ("Jayson Tatum", "demo:tatum", "points", "27.5"),
        ("Jalen Brunson", "demo:brunson", "assists", "6.5"),
        ("Jayson Tatum", "demo:tatum", "points", "29.5"),
    ]:
        result.append(Market(
            id=f"demo:nba-bos-ny:{pid}:{stat}:{line}", event_id="demo:nba-bos-ny",
            event_name=rows[0][3], sport="basketball", league="NBA", participants=rows[0][4],
            start_time=origin, status="live", market_type="player_prop", player=player, player_id=pid,
            statistic=stat, line=Decimal(line), outcomes=("over", "under"),
            settlement=Settlement(scope="player", participation="must_play"),
            original_ids={"synthetic": f"{pid}:{stat}:{line}"},
        ))
    return result


def demo_frame(tick: int, seed=73, origin=None, simulate_faults=True, pregame_reference_research=False):
    """Same origin + tick + seed produces byte-equivalent payloads."""
    origin = origin or datetime(2026, 10, 20, 23, tzinfo=timezone.utc)
    now = origin + timedelta(seconds=tick * .5)
    rng = random.Random(seed + tick)
    events = []
    for idx, base in enumerate(sample_markets(origin)):
        phase = tick % 80
        suspended = simulate_faults and idx == 1 and 40 <= phase < 46
        market = base.model_copy(update={"status": "suspended" if suspended else "live"})
        if pregame_reference_research:
            market = market.model_copy(update={"status": "suspended" if suspended else "scheduled",
                "start_time": origin + timedelta(hours=2)})
        events.append(("sports.market", market))
        p = .52 + .07 * math.sin(tick / 15 + idx)
        reference_sources = [
            ("demo_stream", "pinnacle"), ("demo_reference", "circa"),
            ("demo_backup", "pinnacle"), ("demo_reference", "bookmaker"),
        ]
        if pregame_reference_research:
            reference_sources.extend([("demo_stream", "fanduel"), ("demo_reference", "draftkings"),
                                      ("demo_reference", "caesars")])
        for j, (provider, book) in enumerate(reference_sources):
            delta = (j % 3 - 1) * .006 + rng.uniform(-.001, .001)
            probability = p + delta
            odds = {market.outcomes[0]: Decimal(str(1 / (probability * 1.025))).quantize(Decimal(".0001")),
                    market.outcomes[1]: Decimal(str(1 / ((1 - probability) * 1.025))).quantize(Decimal(".0001"))}
            stale = simulate_faults and book == "circa" and 20 <= phase < 35
            delay = 12 if stale else .10 + j * .08
            quote = Quote(market_id=market.id, provider=provider, bookmaker=book, decimal_odds=odds,
                          source_time=now - timedelta(seconds=delay), provider_time=now - timedelta(seconds=.03),
                          received_at=now, synthetic=True, status="suspended" if suspended else "active", sequence=tick + 1,
                          source_observed_at=now - timedelta(seconds=delay) if pregame_reference_research else None,
                          observation_verified=pregame_reference_research, observation_complete=pregame_reference_research,
                          market_status_verified=pregame_reference_research,
                          verification_reason="Deterministic synthetic observation; not provider validation" if pregame_reference_research else None)
            events.append(("odds.quote", quote))
        for vi, venue in enumerate(("kalshi_paper", "novig_paper", "prophetx_paper")):
            outcome = market.outcomes[0]
            # Some offered markets deliberately carry negative EV.
            price = max(.05, min(.95, p - (.045 if (idx + vi) % 3 else -.035) + .009 * math.sin(tick / 5 + vi)))
            price = Decimal(str(price)).quantize(Decimal(".001"))
            gap = simulate_faults and vi == 1 and 55 <= phase < 60
            fee = FeeSchedule(model="fixed", per_contract=Decimal(".004"), verified=True,
                              source="SYNTHETIC sample fee; not an actual venue fee")
            book = OrderBook(
                market_id=market.id, venue=venue, outcome=outcome,
                asks=(Level(price=price, quantity=30 + idx * 5), Level(price=min(Decimal(".99"), price + Decimal(".02")), quantity=90)),
                bids=(Level(price=price - Decimal(".02"), quantity=50),),
                source_time=now - timedelta(milliseconds=20), provider_time=now,
                received_at=now, synthetic=True, sequence=tick + 1, synchronized=not gap,
                status="suspended" if suspended else "active", fee=fee,
            )
            events.append(("exchange.book", book))
    events.append(("sports.game_state", GameState(event_id="demo:nba-bos-ny", status="live",
        score={"away": 54 + tick // 10, "home": 51 + tick // 12},
        clock=f"Q3 {11 - (tick // 120) % 12}:32", source_time=now, received_at=now, synthetic=True)))
    return events


class DemoInput(InputPlugin):
    config_model = DemoConfig
    manifest = Manifest(
        name="synthetic_schedule", kind="input", description="Deterministic synthetic odds, props and order books",
        sports=["basketball", "football", "hockey"], markets=["moneyline", "total", "player_prop"],
        transports=["simulation", "replay"], output_schemas=["sports.market@1", "odds.quote@1", "exchange.book@1", "sports.game_state@1"],
        config_schema=DemoConfig.model_json_schema(),
        capabilities={"streaming": True, "polling": False, "synthetic": True,
                      "faults": ["stale_reference", "suspension", "sequence_gap", "recovery"]},
    )

    def __init__(self):
        self.task = None
        self.tick = 0
        self.config = DemoConfig()

    async def start(self, context):
        await super().start(context)
        self.config = DemoConfig.model_validate(context.config)
        self.task = asyncio.create_task(self._run(), name="synthetic-feed")

    async def _run(self):
        while True:
            # Fresh wall-clock at source. Replays instead retain recorded timestamps.
            origin = utcnow() - timedelta(seconds=self.tick * .5)
            for schema, payload in demo_frame(self.tick, self.config.seed, origin, self.config.simulate_faults,
                                              self.config.pregame_reference_research):
                await self.context.bus.publish(Envelope(schema_name=schema, source=self.manifest.name, payload=payload))
            self.tick += 1
            await asyncio.sleep(self.config.interval)

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def connect(self):
        return True

    async def discover_sports(self):
        return self.manifest.sports

    async def discover_events(self):
        return list({m.event_id: m.event_name for m in sample_markets()}.items())

    async def discover_markets(self):
        return sample_markets()

    async def fetch_snapshot(self):
        return demo_frame(self.tick, self.config.seed)

    async def health_check(self):
        return {"status": "streaming" if self.task and not self.task.done() else "disabled",
                "message": "SYNTHETIC: deterministic demo, no external connection", "tick": self.tick}

