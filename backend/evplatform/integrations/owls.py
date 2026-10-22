"""Owls Insight REST transport. Coverage is read from metadata, never invented."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from .base import ProviderConfig, ProviderEnvelope, RestProvider, parse_timestamp, provider_manifest, safe_component


class OwlsConfig(ProviderConfig):
    include_props: bool = False
    alternates: bool = False
    exclude_exchanges: bool = True
    league: str | None = None


class OwlsRest(RestProvider):
    provider_id = "owls_rest"
    base_url = "https://api.owlsinsight.com"
    credential_env = "OWLS_API_KEY"
    config_class = OwlsConfig

    def auth(self) -> tuple[dict[str, str], dict[str, str]]:
        return {"Authorization": f"Bearer {self._credential}"}, {}

    async def discover_sports(self) -> ProviderEnvelope:
        # The documented metadata route includes sports/coverage and rate tiers.
        return await self.request("/api/v1/docs/meta")

    async def discover_events(self, sport: str, **filters: Any) -> ProviderEnvelope:
        return await self.request(f"/api/v1/{safe_component(sport)}/events", params=filters)

    async def discover_markets(self, sport: str | None = None) -> ProviderEnvelope:
        # There is no generic `/markets` route on v1. Return the real metadata.
        return await self.request("/api/v1/docs/meta")

    async def fetch_snapshot(self, sport: str, **filters: Any) -> ProviderEnvelope:
        params: dict[str, Any] = {
            "alternates": str(self.config.alternates).lower(),
            "exclude_exchanges": str(self.config.exclude_exchanges).lower(),
        }
        if self.config.bookmakers:
            params["books"] = ",".join(self.config.bookmakers)
        if self.config.league:
            params["league"] = self.config.league
        params.update(filters)
        result = await self.request(f"/api/v1/{safe_component(sport)}/odds", params=params)
        meta = result.payload.get("meta", {}) if isinstance(result.payload, dict) else {}
        # Response timestamp is a publication clock, never quote observation time.
        return replace(result, published_at=parse_timestamp(meta.get("timestamp")),
                       metadata={**result.metadata, "sport": sport, "provider_meta": meta,
                                 "odds_format": "american"})

    async def fetch_props(self, sport: str, **filters: Any) -> ProviderEnvelope:
        params: dict[str, Any] = {}
        if self.config.bookmakers:
            params["books"] = ",".join(self.config.bookmakers)
        params.update(filters)
        result = await self.request(f"/api/v1/{safe_component(sport)}/props", params=params)
        meta = result.payload.get("meta", {}) if isinstance(result.payload, dict) else {}
        return replace(result, kind="props_snapshot", published_at=parse_timestamp(meta.get("timestamp")),
                       metadata={**result.metadata, "sport": sport, "provider_meta": meta})

    async def _poll_sport(self, sport: str):
        yield await self.fetch_snapshot(sport)
        if self.config.include_props:
            yield await self.fetch_props(sport)


class OwlsPinnacleRealtimeRest(OwlsRest):
    """Pinnacle's source-confirmed main lines, isolated from Owls' v1 board.

    The endpoint can serve a PS3838 fallback. A fallback is deliberately not
    labelled Pinnacle, and the raw payload is retained only as provider health
    evidence. This poller uses a separate provider ID so a full realtime board
    cannot replace or invalidate the standard Owls provider's markets.
    """

    provider_id = "owls_pinnacle_realtime"
    config_class = OwlsConfig

    async def fetch_snapshot(self, sport: str, **filters: Any) -> ProviderEnvelope:
        params: dict[str, Any] = {}
        if self.config.league:
            params["league"] = self.config.league
        params.update(filters)
        result = await self.request(f"/api/v1/{safe_component(sport)}/realtime", params=params)
        meta = result.payload.get("meta", {}) if isinstance(result.payload, dict) else {}
        if not isinstance(meta, dict) or meta.get("source") != "pinnacle":
            return replace(result, kind="feed_invalidated", payload={
                "sport": sport,
                "reason": "Pinnacle realtime feed unavailable or serving a different source",
                "source": meta.get("source") if isinstance(meta, dict) else None,
            }, metadata={**result.metadata, "sport": sport})
        return replace(result, kind="snapshot", snapshot_complete=True,
                       published_at=parse_timestamp(meta.get("timestamp")),
                       metadata={**result.metadata, "sport": sport, "provider_meta": meta,
                                 "odds_format": "american"})

    async def _poll_sport(self, sport: str):
        yield await self.fetch_snapshot(sport)


OwlsRest.manifest = provider_manifest(
    "owls_rest", "Owls Insight REST", transports=["rest"], env="OWLS_API_KEY",
    config=OwlsConfig, docs=["https://owlsinsight.com/docs", "https://owlsinsight.com/openapi.json"],
    limitations=[
        "Paid API access required; player props require an eligible tier.",
        "meta.availableBooks and returned metadata define actual book/sport coverage.",
        "Per-book feeder publication is not per-quote freshness; retained quote timestamps are preserved.",
        "Circa game-line coverage does not imply Circa player-prop coverage.",
    ],
)

OwlsPinnacleRealtimeRest.manifest = provider_manifest(
    "owls_pinnacle_realtime", "Owls Pinnacle realtime main lines", transports=["rest"],
    env="OWLS_API_KEY", config=OwlsConfig,
    docs=["https://owlsinsight.com/docs", "https://owlsinsight.com/openapi.json"],
    limitations=[
        "Requires an eligible Owls paid plan; no credential is bundled.",
        "Only the official Pinnacle realtime main-line shape is eligible for source-confirmed status.",
        "Market last_seen is observation; last_update is only a price-change clock.",
        "PS3838 fallback, stale feed, missing market confirmation, alternates and props are blocked.",
        "REST polling consumes quota; this plugin does not implement the realtime WebSocket sequence protocol.",
    ],
)
