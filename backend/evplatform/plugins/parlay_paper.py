"""Parlay-priced, sportsbook-vs-sportsbook *paper* observations.

This adapter deliberately creates hypothetical $1 binary paper offers from
real pregame Parlay prices. It is never a source of reviewed market mappings,
executable sportsbook liquidity, native orders, or live trading permission.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ..core.config import PricingConfig
from ..core.models import Envelope, FeeSchedule, Level, Market, OrderBook, ProviderFrame, Quote, Settlement, utcnow
from ..core.plugins import InputPlugin, Manifest, StrategyPlugin
from ..core.pricing import NoSignal, fair_value, opportunity
from ..core.reference_profiles import profile_for_market
from ..integrations.base import ProviderEnvelope, parse_timestamp
from ..integrations.normalization import SourceOffer, extract_offers
from ..integrations.parlay import ParlayGameBoardConfig


SPORTS = {
    "basketball_nba": ("basketball", "NBA"),
    "americanfootball_nfl": ("football", "NFL"),
    "baseball_mlb": ("baseball", "MLB"),
    "icehockey_nhl": ("hockey", "NHL"),
}
PAPER_PREFIX = "parlay_paper:"


class ParlayPaperInputConfig(ParlayGameBoardConfig):
    max_poll_age_seconds: int = Field(default=180, ge=1, le=600)


@dataclass(frozen=True)
class _Side:
    price: Decimal
    time: datetime


class ParlayPaperNormalizer:
    """Pair exact provider-event/participant sides; never guess missing odds."""

    def __init__(self, *, max_age_seconds: int = 600, max_poll_age_seconds: int = 180,
                 max_side_skew_seconds: int = 30,
                 paper_quantity: int = 10):
        self.max_age_seconds = max_age_seconds
        self.max_poll_age_seconds = max_poll_age_seconds
        self.max_side_skew_seconds = max_side_skew_seconds
        self.paper_quantity = paper_quantity
        self.baselines: set[str] = set()
        self.sides: dict[tuple[str, str, str], dict[str, _Side]] = {}
        self.markets: dict[tuple[str, str, str], Market] = {}
        self.active: dict[tuple[str, str, str], tuple[Market, Quote]] = {}
        self.polls: dict[tuple[str, str], datetime] = {}
        self.sequences: dict[tuple[str, str], int] = {}
        self.rejected = 0

    @staticmethod
    def _market(sport: str, offer: SourceOffer) -> Market | None:
        if (sport not in SPORTS or offer.market_key != "h2h" or offer.player_key is not None
                or offer.line is not None or offer.period not in (None, "full_game")
                or not offer.event_id or not offer.bookmaker):
            return None
        names = offer.context.get("participants")
        if (not isinstance(names, list) or len(names) != 2 or
                any(not isinstance(name, str) or not name.strip() for name in names) or
                names[0] == names[1]):
            return None
        start = parse_timestamp(offer.context.get("scheduled_start"))
        if start is None or start <= utcnow():
            return None
        sport_name, league = SPORTS[sport]
        identity = hashlib.sha256(f"{sport}|{offer.event_id}|{start.isoformat()}|{names[0]}|{names[1]}".encode()).hexdigest()[:24]
        return Market(id=f"paper:parlay:h2h:{identity}", event_id=f"paper:parlay:event:{identity}",
            event_name=f"{names[1]} at {names[0]}", sport=sport_name, league=league,
            participants=(names[0], names[1]), start_time=start, market_type="moneyline",
            outcomes=("HOME", "AWAY"), settlement=Settlement(
                period="full_game", scope="event", overtime="paper_assumed_included",
                participation="not_applicable", void_rule="paper_completed_game_assumption_only",
                push_rule="not_applicable", payout=Decimal(1)),
            original_ids={"parlay_game_ws": offer.event_id},
            extensions={"paper_only": True, "live_input": "ParlayAPI", "target_offer": "hypothetical sportsbook price",
                        "depth": "simulated capacity, not sportsbook liquidity",
                        "settlement_equivalence_verified": False, "active_quote_verified": False,
                        "book_poll_is_not_quote_status": True})

    def _sequence(self, market_id: str, venue: str) -> int:
        key = market_id, venue
        self.sequences[key] = self.sequences.get(key, 0) + 1
        return self.sequences[key]

    def _books(self, market: Market, quote: Quote, *, active: bool) -> list[OrderBook]:
        result = []
        venue = PAPER_PREFIX + quote.bookmaker
        for outcome in market.outcomes:
            sequence = self._sequence(market.id, f"{venue}:{outcome}")
            price = Decimal(1) / quote.decimal_odds[outcome]
            result.append(OrderBook(market_id=market.id, venue=venue, outcome=outcome,
                asks=(Level(price=price, quantity=self.paper_quantity),) if active else (),
                sequence=sequence, synchronized=active, status="active" if active else "suspended",
                # This is a synthetic paper book constructed at local receipt.
                # The actual Parlay price-change clock stays on the Quote and
                # controls fair-value eligibility; it is never refreshed here.
                source_time=quote.received_at, provider_time=quote.provider_time,
                received_at=quote.received_at if active else utcnow(), synthetic=True,
                fee=FeeSchedule(model="none", verified=True,
                    source="hypothetical paper sportsbook wager; no claimed exchange fee")))
        return result

    def _withdraw(self, key: tuple[str, str, str]) -> list:
        previous = self.active.pop(key, None)
        if previous is None:
            return []
        market, quote = previous
        withdrawn = quote.model_copy(update={"status": "withdrawn", "received_at": utcnow()})
        return [withdrawn, *self._books(market, withdrawn, active=False)]

    def _invalidate_sport(self, sport: str) -> list:
        updates = []
        for key in list(self.active):
            if key[0] == sport:
                updates.extend(self._withdraw(key))
        for key in list(self.sides):
            if key[0] == sport:
                del self.sides[key]
                self.markets.pop(key, None)
        self.baselines.discard(sport)
        for key in list(self.polls):
            if key[0] == sport:
                del self.polls[key]
        return updates

    def _poll_current(self, sport: str, bookmaker: str, now: datetime) -> bool:
        polled = self.polls.get((sport, bookmaker))
        return bool(polled and 0 <= (now - polled).total_seconds() <= self.max_poll_age_seconds)

    def _heartbeat(self, sport: str, envelope: ProviderEnvelope) -> list:
        if sport not in self.baselines:
            return []
        rows = envelope.payload.get("bookmaker_freshness", [])
        if not isinstance(rows, list):
            return []
        observed = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("bookmaker"), str):
                continue
            key = sport, row["bookmaker"]
            observed.add(key)
            stamp = parse_timestamp(row.get("polled_at_ms"), epoch_unit="milliseconds")
            if (row.get("status") == "observed" and stamp is not None
                    and 0 <= (envelope.received_at - stamp).total_seconds() <= self.max_poll_age_seconds):
                self.polls[key] = stamp
            else:
                self.polls.pop(key, None)
        for key in list(self.polls):
            if key[0] == sport and key not in observed:
                del self.polls[key]
        updates = []
        for key, (market, quote) in list(self.active.items()):
            if key[0] != sport:
                continue
            current = (self._poll_current(sport, key[2], envelope.received_at)
                and quote.source_time is not None
                and 0 <= (envelope.received_at - quote.source_time).total_seconds() <= self.max_age_seconds)
            revised = quote.model_copy(update={"quality": 1.0 if current else 0.0,
                "verification_reason": ("paper_only_book_polled_quote_status_unverified" if current
                    else "paper_only_book_poll_missing_or_stale")})
            self.active[key] = market, revised
            updates.append(revised)
            updates.extend(self._books(market, revised.model_copy(update={
                "received_at": envelope.received_at}), active=current))
        return updates

    def apply(self, envelope: ProviderEnvelope) -> list:
        sport = envelope.metadata.get("sport")
        if not isinstance(sport, str) or sport not in SPORTS:
            return []
        if envelope.kind in {"feed_invalidated", "disconnected", "reconnect"}:
            return self._invalidate_sport(sport)
        if envelope.kind == "heartbeat":
            return self._heartbeat(sport, envelope)
        if envelope.kind == "initial_state":
            updates = self._invalidate_sport(sport)
            if not envelope.snapshot_complete:
                return updates
            self.baselines.add(sport)
        elif envelope.kind == "odds_update":
            if sport not in self.baselines:
                return []
            updates = []
        else:
            return []
        for offer in extract_offers(envelope):
            if offer.market_key != "h2h":
                continue
            market = self._market(sport, offer)
            if market is None:
                self.rejected += 1
                continue
            key = sport, offer.event_id, offer.bookmaker
            previous_market = self.markets.get(key)
            if previous_market is not None and previous_market.id != market.id:
                updates.extend(self._withdraw(key))
                self.sides.pop(key, None)
            self.markets[key] = market
            sides = self.sides.setdefault(key, {})
            names = {"home": "HOME", "away": "AWAY", market.participants[0]: "HOME",
                     market.participants[1]: "AWAY"}
            if (offer.status != "active" or not offer.prices or
                    set(offer.prices) - set(names) or offer.observed_at is None or
                    offer.observed_at > envelope.received_at or
                    (envelope.received_at - offer.observed_at).total_seconds() > self.max_age_seconds):
                updates.extend(self._withdraw(key))
                sides.clear()
                self.rejected += 1
                continue
            for source_name, decimal_odds in offer.prices.items():
                side = names[source_name]
                if side in sides and offer.observed_at < sides[side].time:
                    continue
                sides[side] = _Side(decimal_odds, offer.observed_at)
            if set(sides) != {"HOME", "AWAY"}:
                continue
            oldest = min(item.time for item in sides.values())
            newest = max(item.time for item in sides.values())
            if ((envelope.received_at - oldest).total_seconds() > self.max_age_seconds
                    or (newest - oldest).total_seconds() > self.max_side_skew_seconds):
                updates.extend(self._withdraw(key))
                sides.clear()
                self.rejected += 1
                continue
            quote = Quote(market_id=market.id, provider="parlay_paper_input", bookmaker=offer.bookmaker,
                decimal_odds={side: sides[side].price for side in market.outcomes},
                status="active", source_time=oldest, provider_time=envelope.published_at,
                received_at=envelope.received_at, synthetic=False,
                original_market_id=offer.event_id, observation_verified=False,
                observation_complete=False, market_status_verified=False,
                quality=1.0 if self._poll_current(sport, offer.bookmaker, envelope.received_at) else 0.0,
                verification_reason="paper_only_book_poll_and_retained_price_assumption")
            prior = self.active.get(key)
            if prior is not None and prior[1].decimal_odds == quote.decimal_odds and prior[1].source_time == oldest:
                continue
            self.active[key] = market, quote
            updates.extend((market, quote, *self._books(market, quote, active=quote.quality == 1.0)))
        return updates

    def expire(self, now: datetime | None = None) -> list:
        now = now or utcnow()
        updates = []
        for key, (market, quote) in list(self.active.items()):
            if (now >= market.start_time or quote.source_time is None or
                    (now - quote.source_time).total_seconds() > self.max_age_seconds):
                updates.extend(self._withdraw(key))
                self.sides.pop(key, None)
        return updates


class ParlayPaperInput(InputPlugin):
    config_model = ParlayPaperInputConfig
    manifest = Manifest(name="parlay_paper_input", kind="input", version="1.0.0",
        description="Live Parlay pregame moneylines converted to hypothetical sportsbook paper offers",
        sports=["basketball", "football", "baseball", "hockey"], markets=["moneyline"],
        transports=["event_bus"], authentication=[],
        input_schemas=["provider.frame@1"],
        output_schemas=["sports.market@1", "odds.quote@1", "exchange.book@1"],
        capabilities={"paper_only": True, "actual_depth": False, "live_execution": False,
                      "settlement_verified": False, "requires_complete_baseline": True,
                      "upstream_feed": "parlay_game_ws"},
        permissions=["paper_quotes"], documentation=["https://api.parlay-api.com/docs/websocket"],
        config_schema=ParlayPaperInputConfig.model_json_schema())

    def __init__(self, engine):
        self.engine = engine
        self.queue = engine.bus.subscribe("parlay-paper-input", ["provider.frame"], critical=True)
        self.normalizer = ParlayPaperNormalizer()
        self.allowed_sports: set[str] = set()
        self.allowed_bookmakers: set[str] = set()
        self.task = None
        self.expiry_task = None
        self.errors = 0

    async def start(self, context):
        await self.stop()
        await super().start(context)
        if self.engine.settings.mode != "paper" or self.engine.settings.sample_feed_enabled or self.engine.settings.live_enabled:
            raise PermissionError("Parlay hypothetical offers run only in sample-free, native-disabled paper mode")
        self.allowed_sports = set(context.config["sports"]) & set(SPORTS)
        self.allowed_bookmakers = set(context.config["bookmakers"])
        if not self.allowed_sports or not self.allowed_bookmakers:
            raise ValueError("Paper Parlay requires named supported sports and bookmakers")
        self.normalizer = ParlayPaperNormalizer(max_age_seconds=context.config["max_age_seconds"],
            max_poll_age_seconds=context.config["max_poll_age_seconds"])
        self.task = asyncio.create_task(self._run(), name="parlay-paper-input")
        self.expiry_task = asyncio.create_task(self._expire(), name="parlay-paper-expiry")

    async def _publish(self, updates):
        for item in updates:
            schema = "sports.market" if isinstance(item, Market) else "odds.quote" if isinstance(item, Quote) else "exchange.book"
            await self.context.bus.publish(Envelope(schema_name=schema, source=self.manifest.name, payload=item))

    async def _run(self):
        try:
            while True:
                frame: ProviderFrame = (await self.queue.get()).payload
                try:
                    if frame.provider_id != "parlay_game_ws" or frame.metadata.get("sport") not in self.allowed_sports:
                        continue
                    if frame.kind in {"initial_state", "odds_update"}:
                        frame = frame.model_copy(update={"payload": {**frame.payload,
                            "data": [row for row in frame.payload.get("data", [])
                                     if row.get("bookmaker") in self.allowed_bookmakers]}})
                    envelope = ProviderEnvelope(frame.provider_id, frame.kind, frame.payload,
                        frame.received_at, 0, published_at=frame.published_at,
                        snapshot_complete=frame.snapshot_complete, metadata=frame.metadata)
                    await self._publish(self.normalizer.apply(envelope))
                except ValueError:
                    self.errors += 1
                    sport = frame.metadata.get("sport")
                    if isinstance(sport, str):
                        await self._publish(self.normalizer._invalidate_sport(sport))
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors += 1
            await self.engine.notice("error", f"Paper Parlay stream stopped: {type(exc).__name__}", self.manifest.name)
        finally:
            for sport in list(self.normalizer.baselines):
                await self._publish(self.normalizer._invalidate_sport(sport))

    async def _expire(self):
        while True:
            await asyncio.sleep(1)
            await self._publish(self.normalizer.expire())

    async def stop(self):
        for task in (self.task, self.expiry_task):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.task, self.expiry_task) if task), return_exceptions=True)
        self.task = self.expiry_task = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        if hasattr(self, "context"):
            for sport in list(self.normalizer.baselines):
                await self._publish(self.normalizer._invalidate_sport(sport))

    async def health_check(self):
        upstream = self.engine.registry.records.get("parlay_game_ws")
        health = upstream.health if upstream else {}
        status = "error" if self.task and self.task.done() else "connected" if self.task else "blocked"
        return {"status": status, "message": "Existing Parlay game stream; hypothetical sportsbook paper offers only",
            "authenticated": health.get("authenticated", False), "streaming": bool(self.normalizer.baselines),
            "ready_sports": health.get("ready_sports", []),
            "complete_baseline_sports": sorted(self.normalizer.baselines),
            "paper_markets": len({market.id for market, _ in self.normalizer.active.values()}),
            "paper_bookmakers": len(self.normalizer.active), "rejected_rows": self.normalizer.rejected,
            "errors": self.errors, "synthetic": False, "hypothetical_offers": True,
            "actual_executable_liquidity": False, "native_execution": False}


class ParlayPaperPricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_quote_age_ms: int = Field(default=600000, ge=1000, le=600000)
    min_references: int = Field(default=2, ge=2, le=6)


class ParlayPaperEV(StrategyPlugin):
    config_model = ParlayPaperPricingConfig
    manifest = Manifest(name="parlay_paper_ev", kind="strategy", version="1.0.0",
        description="Paper-only cross-book EV using live Parlay prices and simulated fills",
        sports=["basketball", "football", "baseball", "hockey"], markets=["moneyline"],
        input_schemas=["sports.market@1", "odds.quote@1", "exchange.book@1"],
        output_schemas=["trade.candidate@1"],
        capabilities={"paper_only": True, "native_execution": False, "target_self_reference": False,
                      "devig": ["power", "proportional"]},
        permissions=["paper_candidates"], config_schema=ParlayPaperPricingConfig.model_json_schema())

    def _config(self, market: Market, target: str, shared: PricingConfig, *, devig: str) -> tuple[PricingConfig, str]:
        settings = ParlayPaperPricingConfig.model_validate(self.context.config)
        if shared.reference_profile_mode != "strict":
            raise NoSignal("Paper comparison requires the same configured reference profile used by live pricing")
        try:
            selected = profile_for_market(shared, market, utcnow())
        except ValueError as exc:
            raise NoSignal(str(exc)) from exc
        if selected is None:
            raise NoSignal("No matching shared sportsbook-weight profile for paper comparison")
        weights = selected[1].weights_percent
        required_count = max(settings.min_references, selected[1].min_distinct_books)
        if target not in weights or target == "pinnacle":
            raise NoSignal("Paper target has no independent Pinnacle-anchored reference policy")
        return PricingConfig(devig_method=devig, min_references=required_count,
            eligible_books=list(weights), required_books=["pinnacle"], weights=weights,
            max_quote_age_ms=settings.max_quote_age_ms,
            max_book_age_ms=2500, max_disagreement=shared.max_disagreement,
            min_ev=shared.min_ev, min_roi=shared.min_roi), selected[0]

    def _candidate(self, market: Market, quotes: list[Quote], book: OrderBook, shared: PricingConfig):
        if not market.extensions.get("paper_only") or not book.venue.startswith(PAPER_PREFIX) or not book.synthetic:
            raise NoSignal("Only Parlay hypothetical paper books are supported")
        target = book.venue.removeprefix(PAPER_PREFIX)
        settings = ParlayPaperPricingConfig.model_validate(self.context.config)
        target_quotes = [q for q in quotes if q.provider == "parlay_paper_input"
                         and q.bookmaker == target and q.status == "active" and q.quality == 1.0]
        if len(target_quotes) != 1 or target_quotes[0].source_time is None:
            raise NoSignal("Target sportsbook quote is not eligible for paper comparison")
        target_quote = target_quotes[0]
        age_ms = (utcnow() - target_quote.source_time).total_seconds() * 1000
        if (age_ms < 0 or age_ms > settings.max_quote_age_ms or book.outcome not in target_quote.decimal_odds
                or not book.asks or book.asks[0].price != Decimal(1) / target_quote.decimal_odds[book.outcome]):
            raise NoSignal("Target sportsbook quote is expired or differs from the assumed paper offer")
        independent = [q for q in quotes if q.provider == "parlay_paper_input"
                       and q.bookmaker != target and q.quality == 1.0]
        primary_config, profile_name = self._config(market, target, shared, devig="power")
        sensitivity_config, _ = self._config(market, target, shared, devig="proportional")
        primary = fair_value(market, independent, primary_config)
        sensitivity = fair_value(market, independent, sensitivity_config)
        candidate = opportunity(market, primary, book, primary_config)
        price = candidate.price
        probability = sensitivity.probabilities[book.outcome]
        sensitivity_ev = probability - price
        reason = "Power and proportional estimates must both meet the paper edge threshold"
        eligible = (candidate.execution_eligible and sensitivity_ev >= primary_config.min_ev
                    and sensitivity_ev / price >= primary_config.min_roi)
        return candidate.model_copy(update={"strategy": self.manifest.name, "synthetic": True,
            "live_execution_allowed": False, "reference_profile_id": profile_name,
            "reference_profile_status": "shared_weights_paper_assumptions_only",
            "sensitivity_model": "proportional", "sensitivity_fair_probability": probability,
            "sensitivity_ev_per_contract": sensitivity_ev,
            "sensitivity_roi": sensitivity_ev / price, "execution_eligible": eligible,
            "ineligibility_reasons": candidate.ineligibility_reasons + (() if eligible else (reason,))})

    async def evaluate(self, event, view):
        market = view["market"]
        if not market.extensions.get("paper_only"):
            return []
        results, errors = [], []
        for book in view["books"]:
            try:
                results.append(self._candidate(market, view["quotes"], book, view["config"]))
            except NoSignal as exc:
                errors.append(str(exc))
        if errors and not results:
            raise NoSignal("; ".join(dict.fromkeys(errors)))
        return results

    def revalidate(self, candidate, view):
        return self._candidate(view["market"], view["quotes"], view["book"], view["config"])
