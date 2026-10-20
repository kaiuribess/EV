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


def _parlay_rows(envelope: ProviderEnvelope, rows: list) -> list[SourceOffer]:
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        prices: dict[str, Decimal] = {}
        points: dict[str, str | None] = {}
        if row.get("over_price") is not None or row.get("under_price") is not None:
            for side in ("over", "under"):
                if row.get(f"{side}_price") is not None:
                    prices[side] = decimal_price(row[f"{side}_price"], "american")
                    points[side] = number_identity(row.get("line") if row.get("line") is not None else row.get("total"))
        elif row.get("home_ml") is not None or row.get("away_ml") is not None:
            for side in ("home", "away", "draw"):
                if row.get(f"{side}_ml") is not None:
                    # Canonical mapping decides whether this is truly a two-way market.
                    prices[side] = decimal_price(row[f"{side}_ml"], "american")
                    points[side] = None
        elif row.get("price") is not None:
            name = str(row.get("team") or row.get("side") or "unknown")
            prices[name] = decimal_price(row["price"], "american")
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

    def _combine_partial_moneylines(self, offers: list[SourceOffer]) -> list[SourceOffer]:
        complete = []
        changed = {}
        for offer in offers:
            # Only line-free, non-player outcome rows can be grouped structurally.
            # Spreads with side-dependent points require a dedicated verified mapper.
            if offer.provider != "parlay_ws" or len(offer.prices) != 1 or offer.player_key or offer.line is not None:
                complete.append(offer)
                continue
            identity = offer.identity()
            identity.pop("outcome_points")
            key = json.dumps(identity, sort_keys=True, default=str)
            sides = self._partial.setdefault(key, {})
            if len(self._partial) > max(1000, self.quarantine_limit):
                self._partial.pop(next(iter(self._partial)))
            side = next(iter(offer.prices))
            previous = sides.get(side)
            if previous and previous.observed_at and offer.observed_at and offer.observed_at < previous.observed_at:
                continue
            sides[side] = offer
            changed[key] = offer
        for key, offer in changed.items():
            sides = self._partial[key]
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
            offers = self._combine_partial_moneylines(extract_offers(envelope))
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
