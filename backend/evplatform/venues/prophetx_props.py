"""Exact ProphetX NBA player-prop vocabulary and structural parsing.

The subtype identifies the statistic.  Player and threshold still come from
the native market/selection payload; neither is guessed from sportsbook data.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


PROPHETX_NBA_PROP_SUBTYPES: dict[str, tuple[str, str, str]] = {
    # sub_type: (canonical statistic, exact native title suffix, native type)
    "player_total_points": ("points", " Total Points", "total"),
    "player_total_rebounds": ("rebounds", " Total Rebounds", "total"),
    "player_total_assists": ("assists", " Total Assists", "total"),
    "player_total_three_pointers_made": ("threes", " Total Three Pointers Made", "total"),
    "player_total_points_rebounds": ("points_rebounds", " Total Points Rebounds", "total"),
    "player_total_points_assists": ("points_assists", " Total Points Assists", "total"),
    "player_total_rebounds_assists": ("rebounds_assists", " Total Rebounds Assists", "total"),
    "player_total_points_rebounds_assists": (
        "points_rebounds_assists", " Total Points Rebounds Assists", "total"),
    "player_total_steals": ("steals", " Total Steals", "total"),
    "player_total_blocks": ("blocks", " Total Blocks", "total"),
    "player_total_turnovers": ("turnovers", " Total Turnovers", "total"),
    "player_to_record_a_double_double": (
        "double_double", " To Record A Double Double", "moneyline"),
    "player_to_record_a_triple_double": (
        "triple_double", " To Record A Triple Double", "moneyline"),
}

NBA_PROP_STATISTICS = frozenset(row[0] for row in PROPHETX_NBA_PROP_SUBTYPES.values())

PROPHETX_NFL_PROP_SUBTYPES: dict[str, tuple[str, str, str]] = {
    # Exact production V4 subtype/title shapes observed through the existing
    # authenticated read-only catalog. Source aliases are normalized
    # separately; a missing native subtype never inherits one of these rules.
    "player_total_passing_yards": ("passing_yards", " Passing Yards", "total"),
    "player_total_passing_touchdowns": (
        "passing_touchdowns", " Total Passing Touchdowns", "total"),
    "player_total_pass_completions": (
        "pass_completions", " Total Pass Completions", "total"),
    "player_total_passing_attempts": (
        "pass_attempts", " Total Passing Attempts", "total"),
    "player_total_rushing_yards": ("rushing_yards", " Rushing Yards", "total"),
    "player_total_rushing_attempts": (
        "rushing_attempts", " Total Rushing Attempts", "total"),
    "player_total_receiving_yards": ("receiving_yards", " Receiving Yards", "total"),
    "player_total_receptions": ("receptions", " Total Receptions", "total"),
    "player_total_rushing_receiving_yards": (
        "rushing_receiving_yards", " Total Rushing & Receiving Yards", "total"),
    "player_longest_reception": (
        "longest_reception", " Longest Reception", "total"),
    "player_total_kicking_points": (
        "kicking_points", " Total Kicking Points", "total"),
    "player_total_field_goals_made": (
        "field_goals_made", " Total Field Goals Made", "total"),
    "to_score_a_touchdown": (
        "anytime_touchdown", " To Score a Touchdown", "moneyline"),
    "player_to_score_first_touchdown": (
        "first_touchdown", " To Score First Touchdown", "moneyline"),
}

# Executable scope is the exact intersection of ProphetX's current native
# catalog and Parlay's documented NFL WebSocket keys. Keep the two extra
# native definitions above for honest catalog decoding/research, but do not
# manufacture sportsbook coverage for combined yards or longest reception.
NFL_PROP_STATISTICS = frozenset({
    "passing_yards", "passing_touchdowns", "pass_completions", "pass_attempts",
    "rushing_yards", "rushing_attempts", "receiving_yards", "receptions",
    "kicking_points", "field_goals_made", "anytime_touchdown", "first_touchdown",
})
PROPHETX_PROP_SUBTYPES = {
    **PROPHETX_NBA_PROP_SUBTYPES,
    **PROPHETX_NFL_PROP_SUBTYPES,
}
BOOLEAN_PROP_STATISTICS = frozenset({
    "double_double", "triple_double", "anytime_touchdown", "first_touchdown",
})


def prophetx_prop_subtypes(league: str) -> dict[str, tuple[str, str, str]]:
    if league == "NBA":
        return PROPHETX_NBA_PROP_SUBTYPES
    if league == "NFL":
        return PROPHETX_NFL_PROP_SUBTYPES
    return {}


def native_prop_identity(name: object, sub_type: object) -> tuple[str, str, str] | None:
    """Return exact (player, statistic, native type) from a reviewed subtype/title shape."""
    row = PROPHETX_PROP_SUBTYPES.get(str(sub_type))
    if row is None or not isinstance(name, str):
        return None
    statistic, suffix, native_type = row
    if not name.endswith(suffix):
        return None
    player = " ".join(name[:-len(suffix)].split())
    if not player or len(player) > 100:
        return None
    return player, statistic, native_type


_TOTAL_SELECTION = re.compile(r"^(over|under)\s+(-?\d+(?:\.\d+)?)$", re.IGNORECASE)


def native_prop_outcome(name: object, statistic: str) -> tuple[str, Decimal] | None:
    """Parse only the documented two-sided selection shapes.

    Milestone markets are represented canonically as over/under 0.5 so they
    can join a complete two-sided sportsbook pair without inventing a second
    instrument or a threshold.
    """
    if not isinstance(name, str):
        return None
    normalized = " ".join(name.split())
    if statistic in BOOLEAN_PROP_STATISTICS:
        if normalized.casefold() == "yes":
            return "over", Decimal("0.5")
        if normalized.casefold() == "no":
            return "under", Decimal("0.5")
        return None
    match = _TOTAL_SELECTION.fullmatch(normalized)
    if match is None:
        return None
    try:
        line = Decimal(match.group(2))
    except InvalidOperation:
        return None
    if not line.is_finite() or line == line.to_integral_value():
        return None
    return match.group(1).lower(), line


def native_prop_rule_token(sub_type: str, native_type: str, period: object, *,
                           league: str = "NBA") -> str:
    period_text = str(period) if period is not None else "default_full_game"
    return f"{league}|{native_type}|{sub_type}|{period_text}"


def reviewed_nba_prop_rule_tokens() -> tuple[str, ...]:
    return tuple(native_prop_rule_token(sub_type, row[2], None)
                 for sub_type, row in PROPHETX_NBA_PROP_SUBTYPES.items())


def reviewed_nfl_prop_rule_tokens() -> tuple[str, ...]:
    return tuple(native_prop_rule_token(sub_type, row[2], None, league="NFL")
                 for sub_type, row in PROPHETX_NFL_PROP_SUBTYPES.items()
                 if row[0] in NFL_PROP_STATISTICS)
