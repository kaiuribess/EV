"""Read-only Polymarket US catalog, full-book and moneyline research contracts.

One market slug is one instrument. LONG buys its offers; SHORT sells into its
bids and ties up $1 margin per contract, offset by the sale proceeds. Nothing
here creates an execution mapping or grants account authority.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo

from ..core.models import Market
from ..core.native_research import ResearchNativeMatch

D = Decimal


def _clock(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


_RULE_CLOCK = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?) "
    r"(?P<day>\d{1,2}), (?P<year>\d{4}) at (?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2}))?(?P<ampm>AM|PM) ET\b", re.IGNORECASE)


def _rule_start(value: object) -> datetime | None:
    """Read only an explicit Eastern rule clock; absence stays unreviewed."""
    match = _RULE_CLOCK.search(value) if isinstance(value, str) else None
    if match is None:
        return None
    try:
        month = datetime.strptime(match["month"][:3].title(), "%b").month
        hour = int(match["hour"]) % 12 + (12 if match["ampm"].upper() == "PM" else 0)
        return datetime(int(match["year"]), month, int(match["day"]), hour,
                        int(match["minute"] or "0"),
                        tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    except ValueError:
        return None


def catalog_record(league: str, event: dict, market: dict, *, observed_at: datetime) -> dict:
    """Keep only public identity, state, rule and fee evidence for one slug."""
    if (not isinstance(event, dict) or not isinstance(market, dict)
            or not isinstance(market.get("slug"), str) or not isinstance(event.get("slug"), str)
            or observed_at.tzinfo is None):
        raise ValueError("Invalid Polymarket US catalog identity")
    teams = []
    for team in event.get("teams") or []:
        if not isinstance(team, dict):
            continue
        city, nickname = team.get("safeName"), team.get("name")
        if (isinstance(city, str) and isinstance(nickname, str)
                and isinstance(team.get("id"), int)):
            teams.append({"id": team["id"], "name": f"{city} {nickname}".strip(),
                          "abbreviation": str(team.get("abbreviation") or "").upper()})
    sides = []
    for side in market.get("marketSides") or []:
        if isinstance(side, dict) and type(side.get("long")) is bool:
            sides.append({"id": str(side.get("id")), "long": side["long"],
                          "team_id": side.get("teamId"),
                          "description": str(side.get("description") or ""),
                          "tradable": side.get("tradable") is True})
    fee = market.get("feeCoefficient")
    fee = None if fee is None or isinstance(fee, bool) else str(fee)
    return {"slug": market["slug"], "event_slug": event["slug"],
            "league": league.upper(), "event_title": str(event.get("title") or "")[:160],
            "event_start": event.get("startTime"), "market_start": market.get("gameStartTime"),
            "market_id": str(market.get("id")), "market_type": market.get("sportsMarketTypeV2"),
            "question": str(market.get("question") or "")[:300],
            "rules": str(market.get("description") or "")[:2500],
            "active": market.get("active") is True, "closed": market.get("closed") is True,
            "hidden": market.get("hidden") is True, "archived": market.get("archived") is True,
            "status": market.get("status"), "teams": teams, "sides": sides,
            "fee_coefficient": fee, "minimum_quantity": str(market.get("minimumTradeQty")),
            "price_tick": str(market.get("orderPriceMinTickSize")),
            "catalog_at": observed_at.isoformat()}


def moneyline_matches(market: Market, rows: list[dict], *, observed_at: datetime) -> list[ResearchNativeMatch]:
    """Require identical event, two named teams, full-game winner and clock."""
    if (market.market_type != "moneyline" or market.status != "scheduled"
            or len(market.outcomes) != 2 or set(market.outcomes) != set(market.participants)
            or market.settlement.period.casefold() not in {"full", "full_game"}
            or market.start_time <= observed_at):
        return []
    matches = []
    for row in rows:
        if (row.get("league") != market.league or row.get("market_type") != "SPORTS_MARKET_TYPE_MONEYLINE"
                or row.get("status") != "MARKET_STATUS_OPEN" or not row.get("active")
                or row.get("closed") or row.get("hidden") or row.get("archived")
                or not row.get("rules") or not row.get("question")):
            continue
        event_start, native_start = _clock(row.get("event_start")), _clock(row.get("market_start"))
        if (event_start is None or native_start is None or event_start != native_start
                or abs((native_start-market.start_time.astimezone(timezone.utc)).total_seconds()) > 120):
            continue
        # A different catalog or written-rule time remains unresolved, even
        # for research; matching team names does not settle the question.
        rule_start = _rule_start(row.get("rules"))
        if rule_start is not None and rule_start != native_start:
            continue
        teams = row.get("teams", [])
        sides = row.get("sides", [])
        if (len(teams) != 2 or len(sides) != 2
                or {team["name"] for team in teams} != set(market.participants)
                or {side["long"] for side in sides} != {True, False}
                or not all(side["tradable"] and side["description"]
                           and side["description"].casefold() in row["question"].casefold()
                           and side["description"].casefold() in row["rules"].casefold()
                           for side in sides)):
            continue
        by_id = {team["id"]: team["name"] for team in teams}
        long, short = next(s for s in sides if s["long"]), next(s for s in sides if not s["long"])
        if (len(by_id) != 2 or by_id.get(long["team_id"]) not in market.outcomes
                or by_id.get(short["team_id"]) not in market.outcomes
                or by_id[long["team_id"]] == by_id[short["team_id"]]):
            continue
        matches.append((row, by_id[long["team_id"]], by_id[short["team_id"]], native_start))
    if len(matches) != 1:
        return []
    row, long_name, short_name, native_start = matches[0]
    digest = hashlib.sha256((row["question"] + "\n" + row["rules"]).encode()).hexdigest()
    return [ResearchNativeMatch(
        venue="polymarket_us", source_market_id=market.id, source_event_id=market.event_id,
        source_market_fingerprint=market.identity(), native_market_id=row["slug"],
        native_event_id=row["event_slug"], native_outcome_id="yes" if side == "long" else "no",
        side=side, outcome=outcome, opposing_outcome_id=None, exchange_index=None,
        source_start=market.start_time, native_start=native_start,
        rules_sha256=digest, rules_excerpt=row["rules"][:300],
        normal_win_condition=f"{outcome} wins the full game; exceptional settlement remains unreviewed",
        price_denomination_usd="1", catalog_observed_at=observed_at)
        for side, outcome in (("long", long_name), ("short", short_name))]


def parsed_book(record: dict, payload: dict, *, received_at: datetime, now: datetime,
                transport: str, max_age_seconds: int = 30) -> dict:
    """Validate a full replacement book; never promote BBO/trade/heartbeat."""
    data = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload.get("market_data") if isinstance(payload, dict) else None
    result = {"slug": record["slug"], "source": transport, "received_at": received_at.isoformat(),
              "source_time": None, "source_age_seconds": None, "state": None,
              "bids": [], "offers": [], "current": False, "rejection": None}
    if not isinstance(data, dict) or data.get("marketSlug", data.get("market_slug")) != record["slug"]:
        return {**result, "rejection": "Missing or mismatched full market book"}
    source = _clock(data.get("transactTime", data.get("transact_time")))
    result["source_time"] = source.isoformat() if source else None
    result["state"] = data.get("state")
    if source is None or received_at.tzinfo is None or now.tzinfo is None:
        return {**result, "rejection": "Unknown market-book source time"}
    age = (now-source).total_seconds()
    result["source_age_seconds"] = round(age, 3)
    if (age < 0 or age > max_age_seconds or (now-received_at).total_seconds() > max_age_seconds):
        return {**result, "rejection": "Market book is stale or future-dated"}
    if (data.get("state") != "MARKET_STATE_OPEN" or record.get("status") != "MARKET_STATUS_OPEN"
            or not record.get("active") or record.get("closed") or record.get("hidden")
            or record.get("archived")):
        return {**result, "rejection": "Native market is not affirmatively open"}
    try:
        for side in ("bids", "offers"):
            rows = data[side]
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError("Invalid depth collection")
            levels, prices = [], set()
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("px"), dict)
                        or row["px"].get("currency") != "USD"
                        or isinstance(row["px"].get("value"), (bool, float))
                        or isinstance(row.get("qty"), (bool, float))):
                    raise ValueError("Invalid exact USD level")
                price, qty = D(str(row["px"]["value"])), D(str(row["qty"]))
                if (not price.is_finite() or not qty.is_finite() or not D(0) < price < D(1)
                        or qty <= 0 or price in prices):
                    raise ValueError("Invalid price, fractional quantity or duplicate level")
                prices.add(price)
                levels.append((price, qty))
            levels.sort(key=lambda x: x[0], reverse=side == "bids")
            result[side] = [(str(price), str(qty)) for price, qty in levels]
        if result["bids"] and result["offers"] and D(result["bids"][0][0]) >= D(result["offers"][0][0]):
            raise ValueError("Crossed order book")
    except (KeyError, ValueError, TypeError, ArithmeticError):
        result["bids"], result["offers"] = [], []
        return {**result, "rejection": "Invalid or crossed native order book"}
    result["current"] = True
    return result


def direction_levels(book: dict, direction: str) -> list[tuple[Decimal, Decimal]]:
    if not book.get("current") or direction not in {"long", "short"}:
        return []
    if direction == "long":
        return [(D(p), D(q)) for p, q in book["offers"]]
    # This is a short sale of the same instrument. The effective NO cost is
    # $1 margin less the YES sale proceeds, not a separate NO offer book.
    return [(D(1)-D(p), D(q)) for p, q in book["bids"]]


def current_book(record: dict, client, *, now: datetime, stream_connected: bool) -> dict:
    """Prefer a current full WS book; a separately fetched REST book is labeled."""
    slug = record["slug"]
    if stream_connected and slug in client.books and slug in client.book_received_at:
        # A suspended/expired stream state vetoes an older REST snapshot.
        return parsed_book(record, {"marketData": client.books[slug]},
            received_at=datetime.fromtimestamp(client.book_received_at[slug], timezone.utc),
            now=now, transport="authenticated_full_websocket")
    if slug in client.rest_books and slug in client.rest_book_received_at:
        return parsed_book(record, {"marketData": client.rest_books[slug]},
            received_at=datetime.fromtimestamp(client.rest_book_received_at[slug], timezone.utc),
            now=now, transport="public_sdk_rest_snapshot")
    return {"slug": slug, "current": False, "bids": [], "offers": [],
            "rejection": "No full market book received", "source": None}


def taker_fee(record: dict, effective_price: Decimal, quantity: Decimal) -> Decimal:
    coefficient = record.get("fee_coefficient")
    if coefficient is None or record.get("status") != "MARKET_STATUS_OPEN":
        raise ValueError("Current Polymarket US fee metadata unavailable")
    theta, price, qty = D(coefficient), D(effective_price), D(quantity)
    if (not all(x.is_finite() for x in (theta, price, qty)) or not D(0) <= theta <= D(1)
            or not D(0) < price < D(1) or qty <= 0):
        raise ValueError("Invalid Polymarket US fee inputs")
    # The official standard taker curve is symmetric for long and short. One
    # comparison lot is one fill; live split fills need the order accumulator.
    return (theta*qty*price*(D(1)-price)).quantize(D("0.01"), rounding=ROUND_HALF_EVEN)
