"""Conservative, paper-only matching of Oddize NBA odds to Parlay game boards.

Oddize supplies an observation clock but not verified market-active or settlement
rules. This module never creates a native market mapping or an executable book.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import Envelope, Market, ProviderFrame, Quote, utcnow
from ..core.plugins import InputPlugin, Manifest
from ..integrations.base import ProviderEnvelope, parse_timestamp
from ..integrations.normalization import SourceOffer, extract_offers
from ..integrations.team_aliases import NBA_TEAMS


# Unknown team codes are deliberately not guessed.
BOOKS = {"pin": "pinnacle", "circa": "circa", "dk": "draftkings",
         "fd": "fanduel", "czr": "caesars", "b365": "bet365",
         "boag": "betonline", "mgm": "betmgm"}


class OddizePaperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sports: list[str] = Field(default_factory=lambda: ["nba"])
    max_observation_age_seconds: int = Field(default=600, ge=1, le=600)
    max_start_difference_minutes: int = Field(default=15, ge=0, le=15)


class OddizePaperNormalizer:
    """Keep complete source observations even when they are too old to price."""

    def __init__(self, config: OddizePaperConfig | None = None):
        self.config = config or OddizePaperConfig()
        self.markets: dict[str, Market] = {}
        self.offers: list[SourceOffer] = []
        self.quotes: dict[tuple[str, str], Quote] = {}
        self.matched = 0
        self.stale = 0
        self.unmatched = 0
        self.ambiguous = 0
        self.rejected = 0

    def _candidate_market(self, offer: SourceOffer, now: datetime) -> Market | None:
        if (offer.context.get("event_sport") != "nba" or "nba" not in self.config.sports
                or offer.market_key != "ml" or offer.player_key is not None or offer.line is not None
                or offer.period is not None or offer.bookmaker not in BOOKS
                or offer.status != "unknown" or not offer.observation_verified
                or not offer.observation_complete or offer.observed_at is None):
            return None
        teams = offer.context.get("participants")
        if (not isinstance(teams, list) or len(teams) != 2 or
                any(code not in NBA_TEAMS for code in teams) or teams[0] == teams[1] or
                set(offer.prices) != set(teams)):
            return None
        start = parse_timestamp(offer.context.get("scheduled_start"))
        if start is None or start <= now:
            return None
        matches = [market for market in self.markets.values()
            if (market.extensions.get("paper_only") is True and market.sport == "basketball"
                and market.league == "NBA" and market.market_type == "moneyline"
                and market.status == "scheduled" and market.start_time > now
                and market.player is None and market.line is None
                and market.settlement.period == "full_game"
                and market.participants == tuple(NBA_TEAMS[code] for code in teams)
                and abs((market.start_time - start).total_seconds())
                    <= self.config.max_start_difference_minutes * 60)]
        if len(matches) != 1:
            if matches:
                self.ambiguous += 1
            else:
                self.unmatched += 1
            return None
        return matches[0]

    def _quote(self, offer: SourceOffer, market: Market, now: datetime) -> Quote:
        teams = offer.context["participants"]
        age = (now - offer.observed_at).total_seconds()
        current = 0 <= age <= self.config.max_observation_age_seconds
        if not current:
            self.stale += 1
        return Quote(market_id=market.id, provider="oddize_paper_input",
            bookmaker=BOOKS[offer.bookmaker],
            decimal_odds={"HOME": offer.prices[teams[0]], "AWAY": offer.prices[teams[1]]},
            status="unknown" if current else "suspended", quality=1.0 if current else 0.0,
            source_time=offer.observed_at, source_observed_at=offer.source_observed_at,
            provider_time=offer.provider_time, received_at=now, synthetic=False,
            original_market_id=offer.event_id, observation_verified=True,
            observation_complete=True, market_status_verified=False,
            verification_reason=("paper_only_oddize_observation_status_and_settlement_unverified"
                if current else "paper_only_oddize_observation_stale"))

    def reconcile(self, now: datetime | None = None) -> list[Quote]:
        now = now or utcnow()
        self.matched = self.stale = self.unmatched = self.ambiguous = self.rejected = 0
        proposed: dict[tuple[str, str], Quote] = {}
        collisions = set()
        for offer in self.offers:
            market = self._candidate_market(offer, now)
            if market is None:
                self.rejected += 1
                continue
            quote = self._quote(offer, market, now)
            key = quote.market_id, quote.bookmaker
            if key in proposed:
                collisions.add(key)
            else:
                proposed[key] = quote
        for key in collisions:
            proposed.pop(key, None)
            self.ambiguous += 1
        self.matched = len(proposed)
        updates = []
        for key, old in list(self.quotes.items()):
            new = proposed.get(key)
            if new is None:
                withdrawn = old.model_copy(update={"status": "withdrawn", "quality": 0.0,
                    "received_at": now, "verification_reason": "paper_only_oddize_snapshot_missing_or_invalid"})
                updates.append(withdrawn)
                del self.quotes[key]
            elif new.source_time < old.source_time:
                # An older API snapshot cannot roll an observation backward.
                proposed.pop(key)
            elif (new.source_time == old.source_time and new.decimal_odds != old.decimal_odds):
                suspended = old.model_copy(update={"status": "suspended", "quality": 0.0,
                    "received_at": now, "verification_reason": "paper_only_oddize_conflicting_same_observation"})
                if old != suspended:
                    self.quotes[key] = suspended
                    updates.append(suspended)
                proposed.pop(key)
            elif old.source_time == new.source_time and old.status == "suspended" and new.status == "unknown":
                # A later HTTP receipt cannot renew an old observation.
                proposed.pop(key)
        for key, quote in proposed.items():
            old = self.quotes.get(key)
            if old is None or (old.status, old.quality, old.source_time, old.decimal_odds) != (
                    quote.status, quote.quality, quote.source_time, quote.decimal_odds):
                self.quotes[key] = quote
                updates.append(quote)
        return updates

    def apply_frame(self, frame: ProviderFrame) -> list[Quote]:
        if frame.kind == "feed_invalidated":
            self.offers = []
            return self.reconcile(frame.received_at)
        if frame.kind != "snapshot" or not frame.snapshot_complete or frame.metadata.get("delta"):
            self.offers = []
            return self.reconcile(frame.received_at)
        envelope = ProviderEnvelope(frame.provider_id, frame.kind, frame.payload,
            frame.received_at, 0, published_at=frame.published_at,
            snapshot_complete=frame.snapshot_complete, metadata=frame.metadata)
        try:
            self.offers = extract_offers(envelope)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            self.offers = []
        return self.reconcile(frame.received_at)

    def register_market(self, market: Market) -> list[Quote]:
        if market.extensions.get("paper_only") and market.league == "NBA":
            self.markets[market.id] = market
            return self.reconcile()
        return []

    def invalidate_parlay_board(self) -> list[Quote]:
        # A complete Parlay replacement withdraws the prior market identity
        # set. Old provider prices may be cached, but cannot remain attached
        # to a board that no longer exists.
        self.markets.clear()
        return self.reconcile()

    def expire(self) -> list[Quote]:
        return self.reconcile()


class OddizePaperInput(InputPlugin):
    config_model = OddizePaperConfig
    manifest = Manifest(name="oddize_paper_input", kind="input", version="1.0.0",
        description="Oddize NBA observations matched to Parlay games for paper reference comparison",
        sports=["basketball"], markets=["moneyline"], transports=["event_bus"],
        input_schemas=["provider.frame@1", "sports.market@1"], output_schemas=["odds.quote@1"],
        capabilities={"paper_only": True, "live_execution": False,
            "market_status_verified": False, "settlement_verified": False,
            "upstream_feed": "oddize_rest", "extra_api_requests": False},
        permissions=["paper_quotes"],
        documentation=["https://oddize.com/api/v1/openapi.json"],
        config_schema=OddizePaperConfig.model_json_schema())

    def __init__(self, engine):
        self.engine = engine
        self.queue = engine.bus.subscribe("oddize-paper-input",
            ["provider.frame", "sports.market"], critical=True)
        self.normalizer = OddizePaperNormalizer()
        self.task = None
        self.expiry_task = None
        self.errors = 0

    async def start(self, context):
        await self.stop()
        await super().start(context)
        if (self.engine.settings.mode != "paper" or self.engine.settings.sample_feed_enabled
                or self.engine.settings.live_enabled):
            raise PermissionError("Oddize comparison runs only in sample-free, native-disabled paper mode")
        self.normalizer = OddizePaperNormalizer(OddizePaperConfig.model_validate(context.config))
        self.task = asyncio.create_task(self._run(), name="oddize-paper-input")
        self.expiry_task = asyncio.create_task(self._expire(), name="oddize-paper-expiry")

    async def _publish(self, quotes: list[Quote]):
        for quote in quotes:
            await self.context.bus.publish(Envelope(schema_name="odds.quote",
                source=self.manifest.name, payload=quote))

    async def _run(self):
        try:
            while True:
                event = await self.queue.get()
                try:
                    if event.schema_name == "provider.frame":
                        frame: ProviderFrame = event.payload
                        if frame.provider_id == "oddize_rest":
                            await self._publish(self.normalizer.apply_frame(frame))
                        elif (frame.provider_id == "parlay_game_ws"
                                and frame.metadata.get("sport") == "basketball_nba"
                                and frame.kind in {"initial_state", "feed_invalidated", "disconnected", "reconnect"}):
                            await self._publish(self.normalizer.invalidate_parlay_board())
                    elif event.schema_name == "sports.market" and event.source == "parlay_paper_input":
                        await self._publish(self.normalizer.register_market(event.payload))
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    self.errors += 1
                    self.normalizer.offers = []
                    await self._publish(self.normalizer.reconcile())
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors += 1
            await self.engine.notice("error", f"Oddize paper input stopped: {type(exc).__name__}", self.manifest.name)
        finally:
            self.normalizer.offers = []
            if hasattr(self, "context"):
                await self._publish(self.normalizer.reconcile())

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
            self.normalizer.offers = []
            await self._publish(self.normalizer.reconcile())

    async def health_check(self):
        upstream = self.engine.registry.records.get("oddize_rest")
        state = upstream.health if upstream else {}
        return {"status": "error" if self.task and self.task.done() else "connected" if self.task else "blocked",
            "message": "Paper-only NBA match; source quote age governs eligibility; market status and settlement unverified",
            "authenticated": state.get("authenticated", False),
            "matched_quotes": self.normalizer.matched, "stale_quotes": self.normalizer.stale,
            "unmatched_offers": self.normalizer.unmatched,
            "ambiguous_offers": self.normalizer.ambiguous, "rejected_offers": self.normalizer.rejected,
            "errors": self.errors, "synthetic": False, "native_execution": False}
