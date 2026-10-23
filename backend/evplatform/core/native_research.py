"""Read-only, exact native catalog comparisons for provider-trusted markets.

Catalog leads never create a NativeMarketMapping or grant execution permission.
Only monitored, sequence-valid opposing bids supply executable offers. Unknown
native identities, fees, or source uncertainty remain explicit missing values.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .models import Market, utcnow
from ..integrations.kalshi_event_crosswalk import _fixture, _listed_winner
from ..integrations.team_aliases import NBA_TEAMS
from ..venues.kalshi_fees import kalshi_taker_fill_fee

D = Decimal
NOVIG_NBA = {**NBA_TEAMS, "BKN": "Brooklyn Nets", "CHA": "Charlotte Hornets",
             "PHX": "Phoenix Suns"}
NOVIG_PROP_TYPES = {
    "points": "POINTS", "rebounds": "REBOUNDS", "assists": "ASSISTS",
    "threes": "THREE_POINTERS_MADE", "points_rebounds_assists": "POINTS_REBOUNDS_ASSISTS",
    "passing_yards": "PASSING_YARDS", "rushing_yards": "RUSHING_YARDS",
    "receiving_yards": "RECEIVING_YARDS", "receptions": "RECEPTIONS",
    "pitcher_strikeouts": "PITCHER_STRIKEOUTS", "batter_hits": "HITS",
    "batter_total_bases": "TOTAL_BASES", "batter_rbi": "RBIS",
    "batter_runs": "RUNS", "shots_on_goal": "SHOTS_ON_GOAL",
}

# Only aliases checked against the current public MLB listings are admitted.
# A new team code needs its own observed listing and rule-label review.
_KALSHI_MLB_TEAMS = {
    "TB": ("Tampa Bay Rays", "Tampa Bay"),
    "MIL": ("Milwaukee Brewers", "Milwaukee"),
}
_MLB_EVENT = re.compile(
    r"^KXMLB(?P<family>GAME|TOTAL)-(?P<year>\d{2})(?P<month>[A-Z]{3})"
    r"(?P<day>\d{2})(?P<hour>\d{2})(?P<minute>\d{2})"
    r"(?P<away>TB|MIL)(?P<home>TB|MIL)$")
_MLB_WIN_RULE = re.compile(
    r"^If (?P<winner>.+?) wins the (?P<away>.+?) vs (?P<home>.+?) "
    r"professional baseball game originally scheduled for "
    r"(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}), (?P<year>\d{4}), "
    r"then the market resolves to Yes\.$")
_MLB_TOTAL_RULE = re.compile(
    r"^If (?P<away>.+?) and (?P<home>.+?) collectively score more "
    r"(?P<line>\d+(?:\.\d+)?) runs in the (?P=away) vs (?P=home) "
    r"professional baseball game originally scheduled for "
    r"(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}), (?P<year>\d{4}) "
    r"at (?P<hour>\d{1,2}):(?P<minute>\d{2}) PM E(?:D|S)T, "
    r"then the market resolves to Yes\.$")


@dataclass(frozen=True)
class ResearchNativeMatch:
    venue: str
    source_market_id: str
    source_event_id: str
    source_market_fingerprint: str
    native_market_id: str
    native_event_id: str
    native_outcome_id: str
    side: str
    outcome: str
    opposing_outcome_id: str | None
    exchange_index: int | None
    source_start: datetime
    native_start: datetime | None
    rules_sha256: str
    rules_excerpt: str
    normal_win_condition: str
    price_denomination_usd: str
    catalog_observed_at: datetime

    def public(self) -> dict:
        return {**self.__dict__, "source_start": self.source_start.isoformat(),
                "native_start": self.native_start.isoformat() if self.native_start else None,
                "catalog_observed_at": self.catalog_observed_at.isoformat(),
                "normal_condition_reviewed": False, "mapping_reviewed": False,
                "execution_eligible": False}


def _aware(value: object) -> datetime | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result.astimezone(timezone.utc) if result.utcoffset() is not None else None
        except ValueError:
            return None
    return None


def kalshi_moneyline_matches(market: Market, rows: list[dict], *, observed_at: datetime) -> list[ResearchNativeMatch]:
    """Use the existing reviewed NBA ticker/rule parser; require both winners."""
    if (market.league != "NBA" or market.market_type != "moneyline" or market.status != "scheduled"
            or len(market.outcomes) != 2 or market.start_time <= observed_at):
        return []
    by_event: dict[str, dict[str, dict]] = {}
    conflicts: set[str] = set()
    for row in rows:
        if (not isinstance(row, dict) or row.get("status") not in {"active", "open"}
                or row.get("market_type") != "binary"):
            continue
        event, ticker = row.get("event_ticker"), row.get("ticker")
        fixture = _fixture(event) if isinstance(event, str) else None
        winner = _listed_winner(row)
        if (not fixture or not isinstance(ticker, str) or winner is None
                or not isinstance(row.get("rules_primary"), str)
                or not isinstance(row.get("rules_secondary"), str) or not row["rules_secondary"].strip()
                or type(row.get("exchange_index")) is not int or row["exchange_index"] < 0
                or fixture[1:] != market.participants
                or market.start_time.astimezone(ZoneInfo("America/New_York")).date() != fixture[0]
                or winner not in market.outcomes or not ticker.startswith(event + "-")):
            continue
        pair = by_event.setdefault(event, {})
        if winner in pair:
            conflicts.add(event)
        pair[winner] = row
    complete = [(event, pair) for event, pair in by_event.items()
                if event not in conflicts and set(pair) == set(market.outcomes)
                and len({r.get("exchange_index") for r in pair.values()}) == 1]
    if len(complete) != 1:
        return []
    event, pair = complete[0]
    result = []
    for winner, row in pair.items():
        rule = row["rules_primary"] + "\n" + row["rules_secondary"]
        for side in ("yes", "no"):
            outcome = winner if side == "yes" else next(x for x in market.outcomes if x != winner)
            result.append(ResearchNativeMatch(
                venue="kalshi", source_market_id=market.id, source_event_id=market.event_id,
                source_market_fingerprint=market.identity(),
                native_market_id=row["ticker"], native_event_id=event,
                native_outcome_id=side, side=side, outcome=outcome,
                opposing_outcome_id=None, exchange_index=row.get("exchange_index"),
                source_start=market.start_time, native_start=_aware(row.get("occurrence_datetime")),
                rules_sha256=hashlib.sha256(rule.encode()).hexdigest(),
                rules_excerpt=row["rules_primary"],
                normal_win_condition=f"{outcome} wins; provider overtime scope requires review",
                price_denomination_usd="1", catalog_observed_at=observed_at))
    return result


def _mlb_listing_event(market: Market, event_ticker: str, family: str) -> tuple[datetime, str, str] | None:
    """Cross-check the listing ticker's Eastern game clock and reviewed team codes."""
    match = _MLB_EVENT.fullmatch(event_ticker)
    if match is None or match["family"] != family:
        return None
    away, home = _KALSHI_MLB_TEAMS.get(match["away"]), _KALSHI_MLB_TEAMS.get(match["home"])
    if away is None or home is None or away == home:
        return None
    try:
        local_start = datetime.strptime(
            f"20{match['year']}{match['month']}{match['day']}{match['hour']}{match['minute']}",
            "%Y%b%d%H%M").replace(tzinfo=ZoneInfo("America/New_York"))
    except ValueError:
        return None
    if (local_start.astimezone(timezone.utc) != market.start_time.astimezone(timezone.utc)
            or market.participants != (home[0], away[0])):
        return None
    return local_start.astimezone(timezone.utc), away[1], home[1]


def expected_kalshi_mlb_event(market: Market) -> str | None:
    """Diagnostic ticker lead for the narrowly reviewed MLB team aliases."""
    if market.league != "MLB" or market.market_type not in {"moneyline", "total"}:
        return None
    codes = {full: code for code, (full, _) in _KALSHI_MLB_TEAMS.items()}
    if len(market.participants) != 2 or any(team not in codes for team in market.participants):
        return None
    local = market.start_time.astimezone(ZoneInfo("America/New_York"))
    family = "GAME" if market.market_type == "moneyline" else "TOTAL"
    return f"KXMLB{family}-{local.strftime('%y%b%d%H%M').upper()}{codes[market.participants[1]]}{codes[market.participants[0]]}"


def kalshi_mlb_matches(market: Market, rows: list[dict], *, observed_at: datetime) -> list[ResearchNativeMatch]:
    """Read-only MLB game-winner or exact half-run total discovery leads.

    The observed rule text establishes the ordinary YES condition only. It does
    not approve source settlement, exceptional payouts, or an execution mapping.
    """
    if (market.sport != "baseball" or market.league != "MLB"
            or market.market_type not in {"moneyline", "total"}
            or market.status != "scheduled" or market.start_time <= observed_at
            or market.settlement.period != "FULL" or market.settlement.scope != "event"):
        return []
    family = "GAME" if market.market_type == "moneyline" else "TOTAL"
    if family == "GAME" and (market.line is not None or set(market.outcomes) != set(market.participants)):
        return []
    if family == "TOTAL" and (market.line is None or market.line % 1 != D("0.5")
                              or set(market.outcomes) != {"over", "under"}):
        return []
    grouped: dict[str, list[tuple[dict, str | None, datetime]]] = {}
    for row in rows:
        if (not isinstance(row, dict) or row.get("status") not in {"active", "open"}
                or row.get("market_type") != "binary"
                or type(row.get("exchange_index")) is not int or row["exchange_index"] < 0
                or not isinstance(row.get("rules_primary"), str)
                or not isinstance(row.get("rules_secondary"), str)
                or not row["rules_secondary"].strip()):
            continue
        event, ticker = row.get("event_ticker"), row.get("ticker")
        if not isinstance(event, str) or not isinstance(ticker, str) or not ticker.startswith(event + "-"):
            continue
        fixture = _mlb_listing_event(market, event, family)
        if fixture is None:
            continue
        game_start, away_label, home_label = fixture
        rule = (_MLB_WIN_RULE if family == "GAME" else _MLB_TOTAL_RULE).fullmatch(row["rules_primary"])
        if (rule is None or rule["away"] != away_label or rule["home"] != home_label
                or rule["month"].upper() != game_start.astimezone(ZoneInfo("America/New_York")).strftime("%b").upper()
                or int(rule["day"]) != game_start.astimezone(ZoneInfo("America/New_York")).day
                or int(rule["year"]) != game_start.astimezone(ZoneInfo("America/New_York")).year):
            continue
        winner = None
        if family == "GAME":
            code = ticker.removeprefix(event + "-")
            team = _KALSHI_MLB_TEAMS.get(code)
            if team is None or rule["winner"] != team[1] or team[0] not in market.outcomes:
                continue
            winner = team[0]
        else:
            try:
                strike = D(str(row.get("floor_strike")))
                rule_line = D(rule["line"])
            except (ValueError, TypeError):
                continue
            local_start = game_start.astimezone(ZoneInfo("America/New_York"))
            if (strike != market.line or rule_line != market.line
                    or int(rule["hour"]) != (local_start.hour % 12 or 12)
                    or int(rule["minute"]) != local_start.minute
                    or local_start.hour < 12):
                continue
        grouped.setdefault(event, []).append((row, winner, game_start))
    valid = []
    for event, found in grouped.items():
        if family == "GAME":
            if (len(found) != 2 or {winner for _, winner, _ in found} != set(market.outcomes)
                    or len({row["exchange_index"] for row, _, _ in found}) != 1):
                continue
            valid.append((event, found))
        elif len(found) == 1:
            valid.append((event, found))
    if len(valid) != 1:
        return []
    event, found = valid[0]
    output = []
    for row, winner, game_start in found:
        rule_text = row["rules_primary"] + "\n" + row["rules_secondary"]
        for side in ("yes", "no"):
            outcome = (winner if side == "yes" else next(x for x in market.outcomes if x != winner)) if family == "GAME" else ("over" if side == "yes" else "under")
            condition = (f"{outcome} wins the scheduled full game" if family == "GAME" else
                         f"full-game total {'over' if side == 'yes' else 'under'} {market.line} runs")
            output.append(ResearchNativeMatch(
                venue="kalshi", source_market_id=market.id, source_event_id=market.event_id,
                source_market_fingerprint=market.identity(), native_market_id=row["ticker"],
                native_event_id=event, native_outcome_id=side, side=side, outcome=outcome,
                opposing_outcome_id=None, exchange_index=row["exchange_index"],
                source_start=market.start_time, native_start=game_start,
                rules_sha256=hashlib.sha256(rule_text.encode()).hexdigest(),
                rules_excerpt=row["rules_primary"], normal_win_condition=condition,
                price_denomination_usd="1", catalog_observed_at=observed_at))
    return output


def novig_moneyline_matches(market: Market, rows: list[dict], events: dict[str, dict], *,
                            observed_at: datetime) -> list[ResearchNativeMatch]:
    """Match a catalog event and both outcomes, never infer a team from odds."""
    if (market.market_type != "moneyline" or market.status != "scheduled"
            or len(market.outcomes) != 2 or market.start_time <= observed_at):
        return []
    matching = []
    for row in rows:
        if (not isinstance(row, dict) or row.get("status") != "OPEN" or row.get("marketType") != "MONEY"
                or not isinstance(row.get("marketId"), str) or not row["marketId"]
                or row.get("voids") not in {"FMV", "REFUND"} or row.get("strike") not in (None, "0", 0)):
            continue
        event_id = row.get("eventId")
        event = events.get(event_id)
        if (not isinstance(event, dict) or event.get("eventId") != event_id
                or event.get("league") != market.league or event.get("status") != "OPEN_PREGAME"
                or not isinstance(event.get("description"), str)):
            continue
        starts, event_starts = _aware(row.get("startsTs")), _aware(event.get("startsTs"))
        if (starts is None or starts != event_starts
                or abs((starts - market.start_time).total_seconds()) > 120):
            continue
        names = [part.strip() for part in event["description"].split(" @ ")]
        if len(names) != 2:
            continue
        aliases = NOVIG_NBA if market.league == "NBA" else {}
        canonical = tuple(aliases.get(name, name) for name in names)
        if canonical != (market.participants[1], market.participants[0]):
            continue
        outcomes = row.get("outcomes")
        if not isinstance(outcomes, list) or len(outcomes) != 2:
            continue
        mapped = {}
        for native in outcomes:
            if not isinstance(native, dict) or native.get("status") not in {"TBD", "OPEN"}:
                break
            label = native.get("name")
            label = aliases.get(label, label)
            identifier = native.get("outcomeId")
            if label not in market.outcomes or not isinstance(identifier, str) or not identifier or label in mapped:
                break
            mapped[label] = identifier
        if set(mapped) != set(market.outcomes) or len(set(mapped.values())) != 2:
            continue
        matching.append((row, mapped, starts))
    if len(matching) != 1:
        return []
    row, mapped, starts = matching[0]
    rule = f"{row['marketType']}|{row['voids']}|{row.get('strike')}|{events[row['eventId']]['description']}"
    return [ResearchNativeMatch(
        venue="novig", source_market_id=market.id, source_event_id=market.event_id,
        source_market_fingerprint=market.identity(),
        native_market_id=row["marketId"], native_event_id=row["eventId"],
        native_outcome_id=mapped[outcome], side="buy", outcome=outcome,
        opposing_outcome_id=mapped[next(x for x in market.outcomes if x != outcome)],
        exchange_index=None, source_start=market.start_time, native_start=starts,
        rules_sha256=hashlib.sha256(rule.encode()).hexdigest(),
        rules_excerpt=f"Novig {row['marketType']} {row['voids']} / {events[row['eventId']]['description']}",
        normal_win_condition=f"{outcome} wins; provider overtime scope requires review",
        price_denomination_usd="0.01",
        catalog_observed_at=observed_at) for outcome in market.outcomes]


def novig_prop_matches(market: Market, rows: list[dict], events: dict[str, dict], *,
                       observed_at: datetime) -> list[ResearchNativeMatch]:
    """Require exact full-game player/stat/line and both native outcomes."""
    native_type = NOVIG_PROP_TYPES.get(market.statistic)
    if (market.market_type != "player_prop" or native_type is None or market.status != "scheduled"
            or market.start_time <= observed_at or market.settlement.period != "FULL"
            or market.line is None or not market.player or not market.player_id
            or set(market.outcomes) != {"over", "under"}):
        return []
    matching = []
    for row in rows:
        if (not isinstance(row, dict) or row.get("marketType") != native_type
                or not isinstance(row.get("marketId"), str) or not row["marketId"]
                or row.get("status") != "OPEN" or row.get("voids") not in {"FMV", "REFUND"}):
            continue
        event_id = row.get("eventId")
        event = events.get(event_id)
        if (not isinstance(event, dict) or event.get("eventId") != event_id
                or event.get("league") != market.league or event.get("status") != "OPEN_PREGAME"
                or not isinstance(event.get("description"), str)):
            continue
        starts, event_starts = _aware(row.get("startsTs")), _aware(event.get("startsTs"))
        if (starts is None or starts != event_starts
                or abs((starts-market.start_time).total_seconds()) > 120):
            continue
        aliases = NOVIG_NBA if market.league == "NBA" else {}
        event_names = [aliases.get(name.strip(), name.strip()) for name in event["description"].split(" @ ")]
        if len(event_names) != 2 or tuple(event_names) != (market.participants[1], market.participants[0]):
            continue
        try:
            strike = D(row.get("strike"))
        except (ValueError, TypeError):
            continue
        if (strike != market.line or strike == strike.to_integral_value()
                or row.get("description") != f"{market.player} {row['strike']} {native_type}"):
            continue
        outcomes = row.get("outcomes")
        if not isinstance(outcomes, list) or len(outcomes) != 2:
            continue
        mapped = {}
        for native in outcomes:
            if not isinstance(native, dict) or native.get("status") not in {"OPEN", "TBD"}:
                break
            name, identifier = native.get("name"), native.get("outcomeId")
            direction = next((side for side in market.outcomes
                              if name == f"{side.title()} {row['strike']}"), None)
            if direction is None or not isinstance(identifier, str) or not identifier or direction in mapped:
                break
            mapped[direction] = identifier
        if set(mapped) == set(market.outcomes) and len(set(mapped.values())) == 2:
            matching.append((row, mapped, starts))
    if len(matching) != 1:
        return []
    row, mapped, starts = matching[0]
    rule = f"{native_type}|{row['voids']}|{row['strike']}|{row['description']}|{events[row['eventId']]['description']}"
    return [ResearchNativeMatch(
        venue="novig", source_market_id=market.id, source_event_id=market.event_id,
        source_market_fingerprint=market.identity(), native_market_id=row["marketId"],
        native_event_id=row["eventId"], native_outcome_id=mapped[outcome],
        side="buy", outcome=outcome,
        opposing_outcome_id=mapped[next(side for side in market.outcomes if side != outcome)],
        exchange_index=None, source_start=market.start_time, native_start=starts,
        rules_sha256=hashlib.sha256(rule.encode()).hexdigest(),
        rules_excerpt=f"Novig {row['description']} / {row['voids']}",
        normal_win_condition=f"{market.player} {outcome} {market.line} {market.statistic}; participation and overtime scope require review",
        price_denomination_usd="0.01", catalog_observed_at=observed_at)
        for outcome in market.outcomes]


def compare_minimum_lot(match: ResearchNativeMatch, probability: Decimal, levels: list[tuple[Decimal, Decimal]],
                        *, fee_for, uncertainty_margin_probability: Decimal | None,
                        now: datetime | None = None) -> dict:
    """Conservative one-dollar-payout illustration, not an order or approval."""
    now = now or utcnow()
    result = {**match.public(), "calculated_at": now.isoformat(), "quality": "provisional_native_research",
              "positive_after_costs": False,
              "reasons": ["Native identity is a discovery lead; exact mapping is not reviewed",
                          "Account, quarter-Kelly, risk and owner approval are not established"]}
    if not levels:
        return {**result, "rejection": "No synchronized executable opposing bid depth"}
    quantity = D("1") if match.venue == "kalshi" else D("100")
    payout_per_contract = D("1") if match.venue == "kalshi" else D("0.01")
    if not D(0) <= probability <= D(1):
        return {**result, "rejection": "Invalid fair probability"}
    remaining, cost, fee = quantity, D(0), D(0)
    total_depth, worst_price = D(0), None
    first_depth = D(0)
    for index, (raw_price, raw_depth) in enumerate(levels):
        price, depth = D(raw_price), D(raw_depth)
        if not (price.is_finite() and depth.is_finite() and D(0) < price < D(1) and depth >= 0):
            return {**result, "rejection": "Invalid native price or depth"}
        if index == 0:
            first_depth = depth
        total_depth += depth
        take = min(remaining, depth)
        if take <= 0:
            continue
        try:
            fill_fee = D(fee_for(price, take))
        except (ValueError, TypeError, ArithmeticError):
            return {**result, "rejection": "Applicable native fee metadata is unavailable"}
        if not fill_fee.is_finite() or fill_fee < 0:
            return {**result, "rejection": "Applicable native fee metadata is invalid"}
        fee += fill_fee
        cost += price * take * payout_per_contract
        remaining -= take
        worst_price = price
        if remaining == 0:
            break
    if remaining:
        return {**result, "available_depth_contracts": str(total_depth),
                "rejection": "Insufficient executable depth for the comparison lot"}
    payout = quantity * payout_per_contract
    price = cost / payout
    gross = probability * payout - cost
    after_fee = gross - fee
    allowance = None
    adjusted = None
    if uncertainty_margin_probability is not None:
        margin = D(uncertainty_margin_probability)
        if not D(0) <= margin <= probability:
            return {**result, "rejection": "Uncertainty allowance exceeds fair win probability"}
        allowance = margin * payout
        adjusted = after_fee - allowance
    else:
        result["reasons"].append("Source-uncertainty allowance is not configured; net after allowance is unknown")
    result.update({"price": str(price), "limit_price": str(worst_price),
                   "available_depth_contracts": str(total_depth),
                   "available_at_best_price": str(first_depth),
                   "comparison_quantity": str(quantity), "normal_payout_usd": str(payout),
                   "cash_cost_usd": str(cost), "gross_estimated_profit_usd": str(gross),
                   "gross_edge_probability": str(probability-price), "taker_fee_usd": str(fee),
                   "fee_adjusted_estimated_profit_usd": str(after_fee),
                   "uncertainty_allowance_usd": str(allowance) if allowance is not None else None,
                   "net_estimated_profit_usd": str(adjusted) if adjusted is not None else None,
                   "net_estimated_roi": str(adjusted/(cost+fee)) if adjusted is not None else None,
                   "positive_after_costs": adjusted is not None and adjusted > 0,
                   "execution_eligible": False})
    return result


def novig_taker_fee(market: dict, event: dict, price: Decimal, quantity: Decimal) -> Decimal:
    if not isinstance(market, dict) or not isinstance(event, dict):
        raise ValueError("Novig market/event fee evidence is unavailable")
    fee = market.get("fee") if isinstance(market, dict) else None
    if not isinstance(fee, dict) or market.get("eventId") != event.get("eventId"):
        raise ValueError("Novig fee lineage unknown")
    coefficient = D(fee.get("coefficient"))
    charged = fee.get("charged")
    status = event.get("status")
    if not D(0) <= coefficient <= D(1) or charged not in {"WHEN_LIVE", "ALWAYS"} or status not in {
            "OPEN_PREGAME", "OPEN_INGAME"}:
        raise ValueError("Novig fee schedule or event phase unknown")
    if charged == "WHEN_LIVE" and status == "OPEN_PREGAME":
        return D(0)
    return (coefficient * price * (1-price) * quantity * D(".01")).quantize(D(".00001"), rounding=ROUND_HALF_UP)


def kalshi_research_taker_fee(match: ResearchNativeMatch, series_response: dict,
                              event_response: dict, series_changes: dict,
                              event_changes: dict, price: Decimal, quantity: Decimal,
                              *, now: datetime) -> Decimal:
    """Single-fill, non-direct-member debit for research; production uses its fee bound.

    The listing lineage and pending overrides are checked before applying the
    official quadratic fee. A split order may cost more; this result cannot be
    substituted for the native order's maximum debit reserve.
    """
    series = series_response.get("series") if isinstance(series_response, dict) else None
    event = event_response.get("event") if isinstance(event_response, dict) else None
    if not isinstance(series, dict) or not isinstance(event, dict):
        raise ValueError("Kalshi fee lineage missing")
    if (event.get("event_ticker") != match.native_event_id
            or event.get("series_ticker") != series.get("ticker")
            or event.get("exchange_index") != match.exchange_index
            or not match.native_market_id.startswith(series["ticker"] + "-")):
        raise ValueError("Kalshi fee event, series or shard differs")
    override_type, override_multiplier = event.get("fee_type_override"), event.get("fee_multiplier_override")
    if (override_type is None) != (override_multiplier is None):
        raise ValueError("Partial Kalshi event fee override")
    fee_type = override_type if override_type is not None else series.get("fee_type")
    if fee_type not in {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}:
        raise ValueError("Kalshi fee type unsupported")
    raw = override_multiplier if override_multiplier is not None else series.get("fee_multiplier")
    if isinstance(raw, (float, bool)):
        raise ValueError("Kalshi fee multiplier is not exact")
    multiplier = D(raw)
    if not multiplier.is_finite() or multiplier < 0:
        raise ValueError("Kalshi fee multiplier invalid")
    scheduled = series_changes.get("series_fee_change_arr") if isinstance(series_changes, dict) else None
    event_scheduled = event_changes.get("event_fee_changes") if isinstance(event_changes, dict) else None
    if (not isinstance(scheduled, list) or not isinstance(event_scheduled, list)
            or event_changes.get("cursor") != ""):
        raise ValueError("Kalshi fee changes incomplete")
    for change in [*scheduled, *event_scheduled]:
        if not isinstance(change, dict):
            raise ValueError("Kalshi fee change malformed")
        at = _aware(change.get("scheduled_ts"))
        if at is None or at <= now:
            raise ValueError("Kalshi fee schedule changed or lacks a valid future clock")
        if (change in scheduled and change.get("series_ticker") != series["ticker"]
                or change in event_scheduled and change.get("event_ticker") != match.native_event_id):
            raise ValueError("Kalshi fee change scope differs")
    return kalshi_taker_fill_fee(price, quantity, multiplier,
                                 balance_grid_usd=D(".01")).net_fee_usd
