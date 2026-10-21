"""Explicit, reviewable source-to-canonical mappings; no fuzzy identity matching.

The extraction layer understands transport shapes. Only a verified mapping can
assert event identity, outcome arity, player identity, and settlement equivalence.
Unknown observations remain quarantined and cannot become executable candidates.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evplatform.core.models import Market, Quote

from .base import ProviderEnvelope, parse_timestamp


def decimal_price(value: Any, odds_format: str) -> Decimal:
    price = Decimal(str(value))
    if not price.is_finite():
        raise ValueError("Non-finite price")
    if odds_format == "american":
        if price == 0:
            raise ValueError("Zero American price")
        price = 1 + (price / 100 if price > 0 else 100 / abs(price))
    if price <= 1:
        raise ValueError("Invalid decimal price")
    return price


def number_identity(value: Any) -> str | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        return str(result.normalize()) if result.is_finite() else None
    except InvalidOperation:
        return str(value)


@dataclass(frozen=True)
class SourceOffer:
    provider: str
    bookmaker: str
    event_id: str
    market_key: str
    player_key: str | None
    line: str | None
    period: str | None
    outcome_points: dict[str, str | None]
    prices: dict[str, Decimal]
    observed_at: datetime | None
    provider_time: datetime | None
    status: str = "active"
    context: dict[str, Any] = field(default_factory=dict)

    def identity(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "bookmaker": self.bookmaker,
            "event_id": self.event_id, "market_key": self.market_key,
            "player_key": self.player_key, "line": self.line, "period": self.period,
            "outcome_points": self.outcome_points, "context": self.context,
        }

    @property
    def signature(self) -> str:
        text = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(text.encode()).hexdigest()


class VerifiedOfferMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    market: Market
    outcome_map: dict[str, str]
    bookmaker: str = Field(min_length=1)
    verified: bool = False
    evidence: str = Field(min_length=8)

    @model_validator(mode="after")
    def complete_outcomes(self):
        if not self.verified:
            raise ValueError("Mapping must be explicitly verified")
        if not self.evidence.strip():
            raise ValueError("Mapping requires verification evidence")
        if set(self.outcome_map.values()) != set(self.market.outcomes):
            raise ValueError("Mapping must cover every canonical outcome")
        if len(self.outcome_map) != len(self.market.outcomes):
            raise ValueError("Outcome mapping must be one-to-one")
        return self


class MappingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    mappings: list[VerifiedOfferMapping] = Field(default_factory=list)


def _row_context(row: dict) -> dict:
    return {key: row[key] for key in (
        "marketStructure", "market_structure", "units", "side", "description",
        "settlement", "settlement_rules", "overtime", "participation",
    ) if key in row}


def _oldest(values: list[datetime | None]) -> datetime | None:
    # Both opposing sides need valid time evidence, not just the freshest side.
    return min(values) if values and all(value is not None for value in values) else None


def _toa_offers(envelope: ProviderEnvelope, events: list, *, odds_format: str) -> list[SourceOffer]:
    result = []
    for event in events:
        if not isinstance(event, dict):
            continue
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                outcomes = market.get("outcomes", [])
                prices, points = {}, {}
                for outcome in outcomes:
                    name = str(outcome["name"])
                    if name in prices:
                        raise ValueError("Duplicate outcome in market")
                    prices[name] = decimal_price(outcome["price"], odds_format)
                    points[name] = number_identity(outcome.get("point"))
                # Whole-book liveness is not per-market verification.
                observed = parse_timestamp(market.get("last_update"))
                if envelope.provider_id.startswith("parlay"):
                    observed = observed or parse_timestamp(book.get("last_update_ms"), epoch_unit="milliseconds")
                status = "suspended" if market.get("suspended") or book.get("suspended") else "active"
                if event.get("phase") in {"suspended", "closed"} or event.get("status") in {"suspended", "closed"}:
                    status = "suspended"
                result.append(SourceOffer(
                    envelope.provider_id, str(book["key"]), str(event.get("eventId") or event.get("id")),
                    str(market["key"]), None, None, market.get("period"), points, prices,
                    observed, envelope.published_at, status,
                    {**_row_context(market), "event_sport": event.get("sport_key") or event.get("sport"),
                     "participants": [event.get("home_team"), event.get("away_team")]},
                ))
                # Alternate lines are separate source identities. Missing sides never
                # inherit prices or timestamps from a primary rung.
                alternates: dict[str, list[dict]] = {}
                for outcome in outcomes:
                    for rung in outcome.get("alternateLines", []):
                        key = number_identity(abs(Decimal(str(rung["point"]))))
                        alternates.setdefault(key, []).append({**rung, "name": outcome["name"]})
                for group in alternates.values():
                    alt_prices = {str(o["name"]): decimal_price(o["price"], odds_format) for o in group}
                    alt_points = {str(o["name"]): number_identity(o["point"]) for o in group}
                    result.append(SourceOffer(
                        envelope.provider_id, str(book["key"]), str(event.get("eventId") or event.get("id")),
                        str(market["key"]), None, None, market.get("period"), alt_points, alt_prices,
                        _oldest([parse_timestamp(o.get("last_update")) for o in group]), envelope.published_at,
                        "active", {**_row_context(market), "alternate": True,
                                   "participants": [event.get("home_team"), event.get("away_team")]},
                    ))
    return result


def _parlay_american_price(value: Any) -> Decimal:
    price = Decimal(str(value))
    if not price.is_finite() or abs(price) < 100 or price != price.to_integral_value():
        raise ValueError("Invalid Parlay American price")
    return decimal_price(price, "american")


def _parlay_outcome_price(row: dict) -> Decimal | None:
    # The AsyncAPI schema calls the American price `price`; the player-props
    # guide also documents explicit American and decimal representations.
    american = [_parlay_american_price(row[key]) for key in ("price", "price_american")
                if row.get(key) is not None]
    if american and any(price != american[0] for price in american[1:]):
        raise ValueError("Conflicting Parlay American prices")
    decimal = decimal_price(row["price_decimal"], "decimal") if row.get("price_decimal") is not None else None
    # The published example pairs -118 with 1.847, rounded to three decimals.
    # A larger discrepancy is not an alternate representation of that quote.
    if american and decimal is not None and abs(american[0] - decimal) > Decimal("0.0005"):
        raise ValueError("Conflicting Parlay price formats")
    return american[0] if american else decimal


def _parlay_rows(envelope: ProviderEnvelope, rows: list) -> list[SourceOffer]:
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        prices: dict[str, Decimal] = {}
        points: dict[str, str | None] = {}
        has_totals = any(row.get(f"{side}_price") is not None for side in ("over", "under"))
        has_moneyline = any(row.get(f"{side}_ml") is not None for side in ("home", "away", "draw"))
        has_outcome = any(row.get(key) is not None for key in ("price", "price_american", "price_decimal"))
        if sum((has_totals, has_moneyline, has_outcome)) > 1:
            raise ValueError("Conflicting Parlay row shapes")
        if has_totals:
            for side in ("over", "under"):
                if row.get(f"{side}_price") is not None:
                    prices[side] = _parlay_american_price(row[f"{side}_price"])
                    points[side] = number_identity(row.get("line") if row.get("line") is not None else row.get("total"))
        elif has_moneyline:
            for side in ("home", "away", "draw"):
                if row.get(f"{side}_ml") is not None:
                    # Canonical mapping decides whether this is truly a two-way market.
                    prices[side] = _parlay_american_price(row[f"{side}_ml"])
                    points[side] = None
        elif has_outcome:
            name = row.get("outcome") if "outcome" in row else row.get("team") or row.get("side")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Missing Parlay outcome label")
            prices[name] = _parlay_outcome_price(row)
            points[name] = number_identity(row.get("line"))
        if not prices:
            continue
        status = "withdrawn" if row.get("action") == "remove" else (
            "suspended" if row.get("suspended") is True or row.get("status") == "suspended" or row.get("available") is False else "active"
        )
        result.append(SourceOffer(
            envelope.provider_id, str(row.get("bookmaker", "")),
            str(row.get("canonical_event_id") or row.get("event_id") or row.get("eventId") or ""),
            str(row.get("market_key", "")), row.get("player"),
            number_identity(row.get("line") if row.get("line") is not None else row.get("total")),
            row.get("period"), points, prices,
            parse_timestamp(row.get("last_update"), epoch_unit="milliseconds"),
            envelope.published_at, status,
            {**_row_context(row), "participants": [row.get("home_team"), row.get("away_team")]},
        ))
    return result


def _owls_props(envelope: ProviderEnvelope) -> list[SourceOffer]:
    result = []
    for game in envelope.payload.get("data", []):
        for book in game.get("books", []):
            for prop in book.get("props", []):
                prices = {side: decimal_price(prop[f"{side}Price"], "american")
                          for side in ("over", "under") if prop.get(f"{side}Price") is not None}
                result.append(SourceOffer(
                    envelope.provider_id, str(book.get("book") or book.get("key") or ""),
                    str(game.get("gameId", "")), str(prop.get("category", "")), prop.get("playerName"),
                    number_identity(prop.get("line")), prop.get("period"),
                    {side: number_identity(prop.get("line")) for side in prices}, prices,
                    parse_timestamp(prop.get("lastUpdate")), envelope.published_at,
                    "suspended" if prop.get("suspended") else "active",
                    {**_row_context(prop), "price_basis": {
                        side: prop.get(f"{side}PriceBasis") for side in prices
                    }, "participants": [game.get("homeTeam"), game.get("awayTeam")]},
                ))
    return result


def _oddspapi_offers(envelope: ProviderEnvelope) -> list[SourceOffer]:
    fixture = envelope.payload
    result = []
    for book_key, book in fixture.get("bookmakerOdds", {}).items():
        for market_id, market in book.get("markets", {}).items():
            catalog = envelope.metadata.get("market_catalog", {}).get(str(market_id), {})
            players: dict[str, dict[str, dict]] = {}
            for outcome_id, outcome in market.get("outcomes", {}).items():
                for player_id, selection in outcome.get("players", {}).items():
                    players.setdefault(player_id, {})[outcome_id] = selection
            for player_id, outcomes in players.items():
                if player_id != "0" and (
                    not envelope.metadata.get("allow_props", False)
                    or book_key not in envelope.metadata.get("props_books", [])
                ):
                    continue
                prices = {key: decimal_price(row["price"], "decimal") for key, row in outcomes.items()}
                active = (book.get("suspended") is not True and market.get("marketActive") is True
                          and all(row.get("active") is True for row in outcomes.values()))
                # Provider recorded-change time and bookmaker clock are kept separate.
                source_time = _oldest([parse_timestamp(row.get("bookmakerChangedAt")) for row in outcomes.values()])
                provider_time = _oldest([parse_timestamp(row.get("changedAt")) for row in outcomes.values()])
                result.append(SourceOffer(
                    envelope.provider_id, book_key, str(fixture.get("fixtureId", "")), str(market_id),
                    None if player_id == "0" else str(player_id),
                    number_identity(catalog.get("handicap")), catalog.get("period"),
                    {key: None for key in outcomes}, prices, source_time, provider_time,
                    "active" if active and fixture.get("statusId") not in {2, 3} else "suspended",
                    {"bookmaker_market_id": market.get("bookmakerMarketId"),
                     "native_outcomes": {key: row.get("bookmakerOutcomeId") for key, row in outcomes.items()},
                     "participants": [fixture.get("participant1Id"), fixture.get("participant2Id")],
                     "market_catalog": {key: catalog[key] for key in (
                         "marketType", "marketName", "playerProp", "period", "handicap",
                     ) if key in catalog}},
                ))
    return result


def _oddize_quote_time(row: dict) -> datetime | None:
    epoch = parse_timestamp(row.get("ts"))
    iso = parse_timestamp(row.get("timestamp"))
    # The two documented fields represent the same observation clock. Conflicts
    # cannot be made trustworthy by using whichever happens to look fresher.
    if epoch is not None and iso is not None and epoch != iso:
        return None
    return epoch or iso


def _oddize_offers(envelope: ProviderEnvelope) -> list[SourceOffer]:
    payload = envelope.payload
    if not isinstance(payload, dict):
        raise ValueError("Oddize payload must be an object")
    props = envelope.kind == "props_snapshot"
    if props:
        events = [{"event_id": payload.get("event_id") or envelope.metadata.get("event_id"),
                   "sport": payload.get("sport") or envelope.metadata.get("sport"),
                   "props": payload.get("props", [])}]
    elif "event" in payload:
        events = [payload["event"]]
    else:
        events = payload.get("events", [])
    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict) or not event.get("event_id"):
            raise ValueError("Oddize event identity missing")
        rows = event.get("props" if props else "odds", [])
        if not isinstance(rows, list):
            raise ValueError("Oddize prices must be a list")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Oddize price must be an object")
            bookmaker, market = row.get("book"), row.get("offer_type") or row.get("market")
            side = row.get("side") if props else row.get("team")
            if not all(isinstance(value, str) and value.strip() for value in (bookmaker, market, side)):
                raise ValueError("Oddize book, market or outcome missing")
            player = row.get("subject") if props and row.get("subject_type") == "player" else row.get("player_name")
            if props and (row.get("subject_type") not in {"player", "team"} or not row.get("subject")):
                raise ValueError("Oddize prop subject identity missing")
            if props and side not in {"Over", "Under"}:
                raise ValueError("Oddize prop side is not a documented Over/Under outcome")
            line = number_identity(row.get("line"))
            if line is not None:
                numeric = Decimal(line)
                if not numeric.is_finite():
                    raise ValueError("Invalid Oddize threshold")
                if not props and market in {"s", "rl", "alt_s"}:
                    line = number_identity(abs(numeric))
            if props and line is None:
                raise ValueError("Oddize prop threshold missing")
            raw_price = Decimal(str(row.get("american_odds")))
            if not raw_price.is_finite() or abs(raw_price) < 100 or raw_price != raw_price.to_integral_value():
                raise ValueError("Invalid Oddize American price")
            context = {
                "event_sport": event.get("sport") or envelope.metadata.get("sport"),
                # OpenAPI defines team1 as away and team2 as home.
                "participants": [event.get("team2"), event.get("team1")],
                "scheduled_start": event.get("start_date"),
                "subject": row.get("subject") if props else None,
                "subject_type": row.get("subject_type") if props else None,
                "observation_clock": "oddize_observation",
                "settlement_rules_provided": False,
            }
            identity = {"bookmaker": bookmaker, "event_id": str(event["event_id"]), "market_key": market,
                        "player_key": player, "line": line, "context": context}
            key = json.dumps(identity, sort_keys=True, default=str)
            group = grouped.setdefault(key, {**identity, "prices": {}, "points": {}, "times": []})
            if side in group["prices"]:
                raise ValueError("Duplicate Oddize outcome in a source market")
            group["prices"][side] = decimal_price(raw_price, "american")
            group["points"][side] = number_identity(row.get("line"))
            group["times"].append(_oddize_quote_time(row))
    return [SourceOffer(
        envelope.provider_id, group["bookmaker"], group["event_id"], group["market_key"],
        group["player_key"], group["line"], None, group["points"], group["prices"],
        _oldest(group["times"]), None, "active", group["context"],
    ) for group in grouped.values()]


def extract_offers(envelope: ProviderEnvelope) -> list[SourceOffer]:
    if envelope.kind not in {"snapshot", "props_snapshot", "initial_state", "odds_update"}:
        return []
    if envelope.provider_id == "parlay_ws":
        return _parlay_rows(envelope, envelope.payload.get("data", []))
    if envelope.provider_id == "parlay_rest":
        if envelope.kind == "props_snapshot":
            return _parlay_rows(envelope, envelope.payload)
        return _toa_offers(envelope, envelope.payload, odds_format="decimal")
    if envelope.provider_id == "owls_rest":
        if envelope.kind == "props_snapshot":
            return _owls_props(envelope)
        # A game may occur under several book keys; deduplicate exact source identity.
        events = [event for group in envelope.payload.get("data", {}).values() for event in group]
        return list({offer.signature: offer for offer in _toa_offers(envelope, events, odds_format="american")}.values())
    if envelope.provider_id == "oddspapi_rest":
        return _oddspapi_offers(envelope)
    if envelope.provider_id == "oddize_rest":
        return _oddize_offers(envelope)
    return []


class VerifiedProviderMapper:
    def __init__(self, mappings: list[VerifiedOfferMapping] | None = None, *, quarantine_limit: int = 1000):
        self.mappings: dict[str, VerifiedOfferMapping] = {}
        self.quarantine: OrderedDict[str, dict] = OrderedDict()
        self.quarantine_limit = quarantine_limit
        self.mapped_count = 0
        self.rejected_count = 0
        self._last: dict[tuple[str, str], tuple[SourceOffer, Market, Quote, str | None]] = {}
        self._partial: dict[str, dict[str, SourceOffer]] = {}
        for mapping in mappings or []:
            self.register(mapping)

    @classmethod
    def from_file(cls, path: str | Path) -> "VerifiedProviderMapper":
        path = Path(path)
        if not path.exists():
            return cls()
        contents = MappingFile.model_validate_json(path.read_text(encoding="utf-8"))
        return cls(contents.mappings)

    def load(self, path: str | Path) -> None:
        replacement = self.from_file(path)
        self.mappings = replacement.mappings

    def register(self, mapping: VerifiedOfferMapping | dict) -> None:
        mapping = VerifiedOfferMapping.model_validate(mapping)
        if mapping.signature in self.mappings and self.mappings[mapping.signature] != mapping:
            raise ValueError("Conflicting verified mapping")
        self.mappings[mapping.signature] = mapping

    def _quarantine(self, offer: SourceOffer | None, reason: str, provider: str) -> None:
        self.rejected_count += 1
        key = offer.signature if offer else f"invalid:{provider}"
        self.quarantine[key] = {
            "signature": key, "provider": provider, "reason": reason,
            "identity": offer.identity() if offer else None,
        }
        self.quarantine.move_to_end(key)
        while len(self.quarantine) > self.quarantine_limit:
            self.quarantine.popitem(last=False)

    def _invalidate(self, envelope: ProviderEnvelope, selectors: dict | None = None) -> list[tuple[Market, Quote]]:
        result = []
        scope = envelope.metadata.get("sport")
        if envelope.kind == "feed_invalidated" and isinstance(envelope.payload, dict):
            scope = envelope.payload.get("sport") or scope
        for key, (offer, market, quote, cached_scope) in list(self._last.items()):
            if key[0] != envelope.provider_id or (scope and cached_scope and scope != cached_scope):
                continue
            if quote.status != "active":
                continue
            if selectors and any(value and getattr(offer, name) != value for name, value in selectors.items()):
                continue
            suspended = quote.model_copy(update={"status": "suspended", "received_at": envelope.received_at})
            self._last[key] = (offer, market, suspended, cached_scope)
            result.append((market, suspended))
        return result

    def _combine_partial_outcomes(self, offers: list[SourceOffer]) -> list[SourceOffer]:
        complete = []
        changed = {}
        # REST prop rows may be separate outcomes within one response. They must
        # never inherit an opposing side from an earlier polling snapshot.
        snapshot_partial: dict[str, dict[str, SourceOffer]] = {}
        for offer in offers:
            try:
                numeric_line = offer.line is not None and Decimal(offer.line).is_finite()
            except InvalidOperation:
                numeric_line = False
            # Only the documented exact player/Over/Under threshold structure can
            # join prop rows. Names, periods, books and lines remain source identity;
            # the resulting complete offer still needs its own verified mapping.
            paired_prop = (
                isinstance(offer.player_key, str) and bool(offer.player_key.strip())
                and numeric_line
                and offer.market_key.removesuffix("_alternate") not in {
                    "player_first_basket", "player_first_team_basket", "player_double_double",
                    "player_triple_double", "player_anytime_td", "player_1st_td", "player_last_td",
                    "pitcher_record_a_win",
                }
                and set(offer.prices) <= {f"{offer.player_key} Over", f"{offer.player_key} Under"}
                and set(offer.outcome_points.values()) == {offer.line}
            )
            # Line-free game rows retain their actual outcome set. Spreads and
            # other prop structures require a dedicated verified mapper.
            moneyline = not offer.player_key and offer.line is None
            streaming_pair = offer.provider == "parlay_ws" and (moneyline or paired_prop)
            snapshot_pair = offer.provider == "parlay_rest" and paired_prop
            if len(offer.prices) != 1 or not (streaming_pair or snapshot_pair):
                complete.append(offer)
                continue
            identity = offer.identity()
            identity.pop("outcome_points")
            key = json.dumps(identity, sort_keys=True, default=str)
            partial = self._partial if streaming_pair else snapshot_partial
            sides = partial.setdefault(key, {})
            if streaming_pair and len(self._partial) > max(1000, self.quarantine_limit):
                self._partial.pop(next(iter(self._partial)))
            side = next(iter(offer.prices))
            previous = sides.get(side)
            if previous and previous.observed_at and offer.observed_at and offer.observed_at < previous.observed_at:
                continue
            sides[side] = offer
            changed[key] = (offer, sides)
        for offer, sides in changed.values():
            complete.append(replace(
                offer,
                prices={side: part.prices[side] for side, part in sides.items()},
                outcome_points={side: part.outcome_points[side] for side, part in sides.items()},
                observed_at=_oldest([part.observed_at for part in sides.values()]),
                status="active" if all(part.status == "active" for part in sides.values()) else "suspended",
            ))
        return complete

    def _clear_partial(self, provider: str) -> None:
        for key in list(self._partial):
            if next(iter(self._partial[key].values())).provider == provider:
                del self._partial[key]

    def __call__(self, envelope: ProviderEnvelope) -> list[tuple[Market, Quote]]:
        if envelope.kind == "feed_invalidated":
            self._clear_partial(envelope.provider_id)
            return self._invalidate(envelope)
        result = []
        if envelope.kind == "initial_state" or envelope.snapshot_complete:
            self._clear_partial(envelope.provider_id)
            # Even an incomplete reconnect baseline cannot retain old live quotes.
            result.extend(self._invalidate(envelope))
        if envelope.provider_id.startswith("parlay"):
            rows = envelope.payload.get("data", []) if isinstance(envelope.payload, dict) else envelope.payload
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and (row.get("action") == "remove" or row.get("suspended") is True or row.get("available") is False):
                        self._clear_partial(envelope.provider_id)
                        result.extend(self._invalidate(envelope, {
                            "event_id": str(row.get("canonical_event_id") or row.get("event_id") or row.get("eventId") or ""),
                            "bookmaker": row.get("bookmaker"), "market_key": row.get("market_key"),
                        }))
        try:
            offers = self._combine_partial_outcomes(extract_offers(envelope))
        except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
            self._quarantine(None, "Invalid provider payload or price", envelope.provider_id)
            self._clear_partial(envelope.provider_id)
            return result + self._invalidate(envelope)
        for offer in offers:
            mapping = self.mappings.get(offer.signature)
            if mapping is None:
                self._quarantine(offer, "Verified event/player/line/settlement mapping required", envelope.provider_id)
                result.extend(self._invalidate(envelope, {
                    "event_id": offer.event_id, "bookmaker": offer.bookmaker, "market_key": offer.market_key,
                }))
                continue
            if set(offer.prices) != set(mapping.outcome_map):
                self._quarantine(offer, "Incomplete or changed outcome set", envelope.provider_id)
                continue
            if mapping.market.market_type == "player_prop" and (not offer.player_key or offer.line is None):
                self._quarantine(offer, "Player-prop identity or threshold missing", envelope.provider_id)
                continue
            if mapping.market.line is not None and offer.line is not None:
                if number_identity(mapping.market.line) != number_identity(offer.line):
                    self._quarantine(offer, "Source and canonical thresholds differ", envelope.provider_id)
                    continue
            if offer.context.get("price_basis") and "midpoint" in offer.context["price_basis"].values():
                self._quarantine(offer, "Midpoint is not a sportsbook quote", envelope.provider_id)
                continue
            quote = Quote(
                market_id=mapping.market.id, provider=envelope.provider_id, bookmaker=mapping.bookmaker,
                decimal_odds={mapping.outcome_map[key]: price for key, price in offer.prices.items()},
                source_time=offer.observed_at, provider_time=offer.provider_time,
                received_at=envelope.received_at, synthetic=envelope.synthetic,
                status=offer.status, original_market_id=offer.market_key,
            )
            previous = self._last.get((envelope.provider_id, offer.signature))
            if (previous and previous[2].source_time and quote.source_time
                    and quote.source_time < previous[2].source_time and quote.status == "active"):
                # An out-of-order observation cannot overwrite a newer price.
                continue
            self.mapped_count += 1
            self.quarantine.pop(offer.signature, None)
            self._last[(envelope.provider_id, offer.signature)] = (
                offer, mapping.market, quote, envelope.metadata.get("sport"),
            )
            result.append((mapping.market, quote))
        return result
