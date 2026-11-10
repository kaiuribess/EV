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


def native_prop_identity(name: object, sub_type: object) -> tuple[str, str, str] | None:
    """Return exact (player, statistic, native type) from a reviewed subtype/title shape."""
    row = PROPHETX_NBA_PROP_SUBTYPES.get(str(sub_type))
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
    if statistic in {"double_double", "triple_double"}:
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


def native_prop_rule_token(sub_type: str, native_type: str, period: object) -> str:
    period_text = str(period) if period is not None else "default_full_game"
    return f"NBA|{native_type}|{sub_type}|{period_text}"


def reviewed_nba_prop_rule_tokens() -> tuple[str, ...]:
    return tuple(native_prop_rule_token(sub_type, row[2], None)
                 for sub_type, row in PROPHETX_NBA_PROP_SUBTYPES.items())
