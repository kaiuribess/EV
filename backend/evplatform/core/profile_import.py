"""Compile the owner's versioned research specification without granting execution.

Vocabulary translation is explicit. It does not verify external identities,
settlement equivalence, actual coverage, observation semantics or freshness TTLs.
"""
from __future__ import annotations

from .config import PricingConfig
from .reference_profiles import ProfileSelector, ReferenceProfile

INITIAL_PROFILES = ("nba_core_props", "nfl_volume_props", "mlb_pitcher_strikeouts", "mlb_hitter_volume")
SPORTS = {
    "nba": ("basketball", "NBA"), "nfl": ("football", "NFL"),
    "mlb": ("baseball", "MLB"), "nhl": ("hockey", "NHL"),
    "ncaaf": ("football", "NCAAF"), "ncaabm": ("basketball", "NCAABM"),
}
STATISTICS = {
    "player_points": "points", "player_rebounds": "rebounds", "player_assists": "assists",
    "player_threes": "threes", "player_points_rebounds_assists": "points_rebounds_assists",
    "player_passing_yards": "passing_yards", "player_rushing_yards": "rushing_yards",
    "player_receiving_yards": "receiving_yards", "player_receptions": "receptions",
    "pitcher_strikeouts": "pitcher_strikeouts", "batter_hits": "batter_hits",
    "batter_total_bases": "batter_total_bases", "batter_rbi": "batter_rbi",
    "batter_runs": "batter_runs", "batter_home_runs": "batter_home_runs",
    "player_shots_on_goal": "shots_on_goal",
}
BASKETBALL_STATS = {"points", "rebounds", "assists", "threes", "points_rebounds_assists"}
FOOTBALL_STATS = {"passing_yards", "rushing_yards", "receiving_yards", "receptions"}


def compile_priors(spec: dict, base: PricingConfig | None = None) -> PricingConfig:
    if (spec.get("schema_version") != "1.0" or spec.get("execution_enabled") is not False
            or spec.get("all_profiles_pregame_only") is not True or spec.get("default_mode") != "paper"):
        raise ValueError("Expected the version 1.0 pregame, execution-disabled research specification")
    if spec.get("freshness_contract", {}).get("ttl_seconds_by_provider_book_sport_market_phase") != {}:
        raise ValueError("Import TTL policies separately with exact source selectors and verification evidence")
    if spec.get("optional_bookmaker_override", {}).get("enabled") is not False:
        raise ValueError("Optional BookMaker experiment must be separately reviewed")
    profiles = {}
    for name, raw in spec["profiles"].items():
        if raw.get("live_execution_allowed") is not False or raw.get("phase") != "pregame":
            raise ValueError("Research import cannot authorize live execution")
        selectors = []
        for league_key in raw["sports"]:
            if league_key not in SPORTS:
                raise ValueError(f"Unrecognized league vocabulary: {league_key}")
            sport, league = SPORTS[league_key]
            for category in raw["canonical_markets"]:
                if category in {"moneyline", "spread", "game_total"}:
                    market_type, statistic = ("total" if category == "game_total" else category), None
                elif category in STATISTICS:
                    market_type, statistic = "player_prop", STATISTICS[category]
                    # The college source has a shared union of football and
                    # basketball statistics, not a promise of cross-sport coverage.
                    if league_key == "ncaaf" and statistic not in FOOTBALL_STATS:
                        continue
                    if league_key == "ncaabm" and statistic not in BASKETBALL_STATS:
                        continue
                else:
                    raise ValueError(f"Unrecognized market vocabulary: {category}")
                selectors.append(ProfileSelector(sport=sport, league=league,
                    market_type=market_type, statistic=statistic))
        profiles[name] = ReferenceProfile(selectors=tuple(selectors),
            weights_percent=raw["weights_percent"], required_any_anchor=tuple(raw["required_any_anchor"]),
            min_distinct_books=raw["min_distinct_books"], status=raw["status"],
            evidence_sources=tuple(spec["sources"][key] for key in raw["evidence_sources"]),
            note=raw.get("note", ""), live_execution_allowed=False)
    return PricingConfig.model_validate({**(base or PricingConfig()).model_dump(),
        "reference_profile_mode": "strict", "reference_profiles": profiles,
        "enabled_reference_profiles": list(INITIAL_PROFILES), "profile_freshness_rules": [],
        "market_overrides": {}, "devig_method": "power"})
