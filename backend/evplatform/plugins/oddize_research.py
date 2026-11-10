"""Read-only Oddize observations joined to exact Parlay research identities.

This consumes the existing metered Oddize snapshot; it makes no API calls.
Unknown event/period aliases and any start-time difference stay unmatched.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from ..core.models import Envelope, ProviderFrame, Quote, utcnow
from ..core.plugins import InputPlugin, Manifest
from ..integrations.base import ProviderEnvelope, parse_timestamp
from ..integrations.normalization import extract_offers
from ..integrations.team_aliases import NBA_TEAMS
from .oddize_paper import BOOKS

SPORTS = {"nba": "NBA", "nfl": "NFL", "mlb": "MLB", "nhl": "NHL",
          "ncaaf": "NCAAF", "ncaabm": "NCAABM"}


class OddizeResearchMatcher:
    def __init__(self):
        self.markets = {}
        self.offers = []
        self.quotes = {}
        self.unmatched = 0
        self.ambiguous = 0

    @staticmethod
    def _participants(offer):
        names = offer.context.get("participants")
        if not isinstance(names, list) or len(names) != 2:
            return None
        if offer.context.get("event_sport") == "nba":
            names = [NBA_TEAMS.get(name, name) for name in names]
        return tuple(names) if all(isinstance(name, str) and name.strip() for name in names) else None

    @staticmethod
    def _odds(offer, market):
        names = offer.context["participants"]
        if offer.market_key == "ml" and market.market_type == "moneyline" and offer.line is None:
            return ({canonical: offer.prices[source] for source, canonical in
                     zip(names, market.participants)} if set(offer.prices) == set(names) else None)
        if offer.market_key == "t" and market.market_type == "total":
            if (offer.line is not None and market.line == Decimal(offer.line)
                    and set(offer.prices) == {"Over", "Under"}
                    and set(offer.outcome_points.values()) == {offer.line}):
                return {side.lower(): offer.prices[side] for side in ("Over", "Under")}
        if offer.market_key == "s" and market.market_type == "spread":
            if (offer.line is not None and market.line == abs(Decimal(offer.line))
                    and set(offer.prices) == set(names)
                    and Decimal(offer.outcome_points[names[0]]) ==
                        -Decimal(offer.outcome_points[names[1]])):
                return {f"{canonical} {Decimal(offer.outcome_points[source]):+g}":
                        offer.prices[source] for source, canonical in zip(names, market.participants)}
        return None

    def reconcile(self, now=None):
        now = now or utcnow()
        proposed, collisions = {}, set()
        self.unmatched = self.ambiguous = 0
        market_index = {}
        for market in self.markets.values():
            if market.extensions.get("research_only") and market.status == "scheduled":
                market_index.setdefault((market.league, market.start_time, market.participants), []).append(market)
        for offer in self.offers:
            if (offer.bookmaker not in BOOKS or offer.status in {"suspended", "withdrawn"}
                    or not offer.observation_verified or not offer.observation_complete
                    or offer.source_observed_at is None or offer.period is not None):
                continue
            participants = self._participants(offer)
            start = parse_timestamp(offer.context.get("scheduled_start"))
            if participants is None or start is None or start <= now:
                continue
            matches = []
            for market in market_index.get((SPORTS.get(offer.context.get("event_sport")),
                                            start, participants), ()):
                if market.settlement.period != "FULL":
                    continue
                odds = self._odds(offer, market)
                if odds is not None and set(odds) == set(market.outcomes):
                    matches.append((market, odds))
            if len(matches) != 1:
                self.ambiguous += bool(matches)
                self.unmatched += not matches
                continue
            market, odds = matches[0]
            key = market.id, BOOKS[offer.bookmaker]
            if key in proposed:
                collisions.add(key)
                continue
            proposed[key] = Quote(market_id=market.id, market_fingerprint=market.identity(),
                provider="oddize_rest", bookmaker=key[1], decimal_odds=odds, status="unknown",
                source_time=offer.observed_at, source_observed_at=offer.source_observed_at,
                observation_verified=True, observation_complete=True,
                market_status_verified=False, normal_outcome_verified=False,
                settlement_equivalence_verified=False,
                provider_time=offer.provider_time, received_at=offer.received_at or now,
                original_market_id=offer.event_id, synthetic=False,
                verification_reason="oddize_price_observation_status_unknown")
        for key in collisions:
            proposed.pop(key, None)
            self.ambiguous += 1
        updates = []
        for key, old in list(self.quotes.items()):
            new = proposed.get(key)
            if new is None:
                updates.append(old.model_copy(update={"status": "withdrawn",
                    "verification_reason": "oddize_snapshot_missing_or_unmatched"}))
                del self.quotes[key]
            elif (new.source_observed_at < old.source_observed_at or
                  new.source_observed_at == old.source_observed_at and
                  new.decimal_odds != old.decimal_odds):
                proposed.pop(key)
        for key, quote in proposed.items():
            if key not in self.quotes or quote.source_observed_at > self.quotes[key].source_observed_at:
                self.quotes[key] = quote
                updates.append(quote)
        return updates

    def apply_frame(self, frame: ProviderFrame):
        if frame.kind == "feed_invalidated":
            self.offers = []
            return self.reconcile(frame.received_at)
        if frame.kind != "snapshot" or not frame.snapshot_complete or frame.metadata.get("delta"):
            return []
        envelope = ProviderEnvelope(frame.provider_id, frame.kind, frame.payload,
            frame.received_at, 0, published_at=frame.published_at,
            snapshot_complete=frame.snapshot_complete, metadata=frame.metadata)
        self.offers = extract_offers(envelope)
        return self.reconcile(frame.received_at)

    def register_market(self, market):
        if market.extensions.get("research_only"):
            self.markets[market.id] = market
        return []  # The bounded timer batches the Parlay board's many rows.

    def invalidate_parlay(self, sport=None, now=None):
        self.markets = {key: market for key, market in self.markets.items()
            if sport is not None and market.extensions.get("provider_sport") != sport}
        return self.reconcile(now)


class OddizeResearchInput(InputPlugin):
    manifest = Manifest(name="oddize_research_input", kind="input", version="1.0.0",
        description="Exact, read-only Oddize cross-provider sportsbook observations",
        sports=["discovered"], markets=["moneyline", "spread", "total"],
        transports=["event_bus"], input_schemas=["provider.frame@1", "sports.market@1"],
        output_schemas=["odds.quote@1"], capabilities={"research_only": True,
            "extra_api_requests": False, "active_status_verified": False},
        permissions=["research_quotes"],
        documentation=["https://oddize.com/api/v1/openapi.json"])

    def __init__(self, engine):
        self.engine, self.matcher, self.queue, self.task = engine, OddizeResearchMatcher(), None, None
        self.reconcile_task = None

    async def start(self, context):
        await self.stop()
        await super().start(context)
        self.queue = self.engine.bus.subscribe("oddize-research-input",
            ["provider.frame", "sports.market"], critical=True)
        self.task = asyncio.create_task(self._run(), name="oddize-research-input")
        self.reconcile_task = asyncio.create_task(self._reconcile_loop(), name="oddize-research-reconcile")

    async def _publish(self, updates):
        for quote in updates:
            await self.context.bus.publish(Envelope(schema_name="odds.quote",
                source=self.manifest.name, payload=quote))

    async def _reconcile_loop(self):
        while True:
            await asyncio.sleep(5)
            if self.matcher.offers:
                await self._publish(self.matcher.reconcile())

    async def _run(self):
        while True:
            event = await self.queue.get()
            try:
                if event.schema_name == "provider.frame" and event.payload.provider_id == "oddize_rest":
                    try:
                        updates = self.matcher.apply_frame(event.payload)
                    except (ValueError, TypeError, KeyError, ArithmeticError):
                        self.matcher.offers = []
                        updates = self.matcher.reconcile(event.payload.received_at)
                elif (event.schema_name == "provider.frame" and
                      event.payload.provider_id == "parlay_game_ws" and
                      event.payload.kind in {"initial_state", "feed_invalidated", "disconnected", "reconnect"}):
                    scope = event.payload.metadata.get("sport") or (
                        event.payload.payload.get("sport") if isinstance(event.payload.payload, dict) else None)
                    updates = self.matcher.invalidate_parlay(scope if isinstance(scope, str) else None,
                        event.payload.received_at)
                elif event.schema_name == "sports.market" and event.source.startswith("parlay"):
                    updates = self.matcher.register_market(event.payload)
                else:
                    updates = []
                await self._publish(updates)
            finally:
                self.queue.task_done()

    async def stop(self):
        for task in (self.task, self.reconcile_task):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.task, self.reconcile_task) if task),
                             return_exceptions=True)
        self.task = self.reconcile_task = None
        self.matcher.offers = []
        if hasattr(self, "context"):
            await self._publish(self.matcher.reconcile())
        if self.queue:
            self.engine.bus.unsubscribe(self.queue)
            self.queue = None

    async def health_check(self):
        return {"status": "connected" if self.task and not self.task.done() else "blocked",
            "matched_quotes": len(self.matcher.quotes), "unmatched_offers": self.matcher.unmatched,
            "ambiguous_offers": self.matcher.ambiguous, "extra_api_requests": 0}
