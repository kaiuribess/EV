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
