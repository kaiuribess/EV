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
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evplatform.core.models import Market, Quote, Settlement

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
    # Identity and clocks are separate. A delivery or price-change timestamp
    # must never be promoted to evidence that the source re-observed a price.
    source_observed_at: datetime | None = None
    observation_verified: bool = False
    observation_complete: bool = False
    source_price_changed_at: datetime | None = None
    provider_reported_price_age_s: Decimal | None = None
    source_stored_at: datetime | None = None
    # Native selection IDs are evidence tied to this exact book/event/player/
    # market/period/line. They never define a new canonical market by themselves.
    source_selection_ids: dict[str, str] = field(default_factory=dict)
    source_status_historical_hint: bool = False
    verification_reason: str | None = None
    market_status_verified: bool = False
    # A provider's book/fixture poll is useful for health review but does not
    # bind this market, outcome, line, or price. It never enters identity or
    # quote-bound observation eligibility.
    source_scope_poll_at: datetime | None = None
    source_scope_is_current: bool | None = None
    # Transport receipt of this particular source row. A merged WebSocket
    # pair must retain the *older* side's delivery age, not the update frame's.
    received_at: datetime | None = None

    def identity(self) -> dict[str, Any]:
        context = dict(self.context)
        # Providers can spell the same instant as Z, .000Z or with a UTC
        # offset. A formatting-only change must not rotate a reviewed mapping.
        # A real schedule change still changes the signature. Keep invalid or
        # timezone-less values verbatim so they cannot gain false identity.
        raw_start = context.get("scheduled_start")
        if isinstance(raw_start, str) and (start := parse_timestamp(raw_start)) is not None:
            context["scheduled_start"] = start.isoformat()
        return {
            "provider": self.provider, "bookmaker": self.bookmaker,
            "event_id": self.event_id, "market_key": self.market_key,
            "player_key": self.player_key, "line": self.line, "period": self.period,
            "outcome_points": self.outcome_points, "context": context,
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
    # A reviewed team/market identity does not certify exceptional settlement.
    # Identity-only mappings may be displayed, but strict live references stop
    # at the contract-review gate until the source and target rules are bridged.
    settlement_equivalence_verified: bool = True
    normal_outcome_verified: bool = False
    normal_outcome_evidence: str | None = None

    @model_validator(mode="after")
    def complete_outcomes(self):
        if not self.verified:
            raise ValueError("Mapping must be explicitly verified")
        if not self.evidence.strip():
            raise ValueError("Mapping requires verification evidence")
        if self.normal_outcome_verified and not (self.normal_outcome_evidence and self.normal_outcome_evidence.strip()):
            raise ValueError("Normal WIN/LOSS review requires its own source-to-target evidence")
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


def _valid_fanduel_link(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        return (parsed.scheme == "https" and host is not None
                and (host == "fanduel.com" or host.endswith(".fanduel.com"))
                and parsed.port in (None, 443)
                and not parsed.username and not parsed.password)
    except ValueError:
        return False


_UNAVAILABLE_STATUSES = frozenset({
    "suspended", "closed", "complete", "completed", "final", "settled",
    "cancelled", "canceled", "void", "voided", "postponed", "withdrawn",
    "inactive", "unavailable",
})


def _explicitly_unavailable(scope: Any) -> bool:
    if not isinstance(scope, dict):
        return False
    if scope.get("suspended") is True or scope.get("available") is False:
        return True
    return any(isinstance(scope.get(key), str)
               and scope[key].strip().casefold() in _UNAVAILABLE_STATUSES
               for key in ("status", "phase"))


def _matches_scope(offer: SourceOffer, selectors: dict) -> bool:
    return all(getattr(offer, name) == value for name, value in selectors.items())


def _parlay_failure_scope(row: Any) -> dict[str, str]:
    """Only narrow a failed row using explicit, well-formed source identity.

    Missing/invalid identity broadens invalidation. Never guess an individual
    player or threshold after price parsing failed.
    """
    if not isinstance(row, dict):
        return {}
    selectors = {key: row[key] for key in ("bookmaker", "market_key")
                 if isinstance(row.get(key), str) and row[key].strip()}
    event = row.get("canonical_event_id") or row.get("event_id") or row.get("eventId")
    if isinstance(event, str) and event.strip():
        selectors["event_id"] = event
    return selectors


def _parlay_unavailability_reason(row: dict) -> str | None:
    """Negative evidence revokes quotes; it never verifies positive status.

    Nullable columns belonging to another transport shape must not withdraw a
    valid named-outcome price. An explicit suspended notice needs no prices.
    """
    if row.get("action") == "remove":
        return "Explicit source removal"
    if _explicitly_unavailable(row):
        return "Explicit source suspension"
    fields = {key: row[key] for key in (
        "over_price", "under_price", "home_ml", "away_ml", "price", "price_american", "price_decimal",
    ) if key in row}
    if any(fields.get(key) is not None for key in ("price", "price_american", "price_decimal")):
        return None
    # Inspect null sides only in a populated price family. Some containers also
    # carry unused null columns for a different family (props vs moneylines).
    families = [("over_price", "under_price"), ("home_ml", "away_ml")]
    populated = [family for family in families if any(fields.get(key) is not None for key in family)]
    missing_side = any(key in fields and fields[key] is None for family in populated for key in family)
    if fields and (missing_side or all(value is None for value in fields.values())):
        return "Explicit missing usable side price"
    return None


def _observation_problem(envelope: ProviderEnvelope, *scopes: dict) -> str | None:
    """Retain adverse completeness evidence without making it market identity."""
    body = envelope.payload if isinstance(envelope.payload, dict) else {}
    headers = envelope.metadata.get("headers", {})
    all_scopes = (body, *scopes)
    # Choose the strongest adverse fact across response, book, market and row.
    # A broad truncation must not hide a topped-up row, a failed collection,
    # or a stale/degraded response when research accepts a complete row.
    if any(scope.get("topped_up") is True for scope in all_scopes):
        return "retained_top_up"
    # Parlay can return a previously built props board when its scan is busy.
    # A fresh HTTP receipt and a row's old collector clock do not establish
    # that the cached board still represents currently offered outcomes.
    if headers.get("x-parlay-stale"):
        return "stale_cached_board"
    # X-Provider-State is per source, not per selection. Its adverse roles
    # still disqualify the corresponding book from time-sensitive pricing.
    try:
        provider_state = json.loads(headers.get("x-provider-state", "{}"))
        source_roles = provider_state.get("src", {}) if isinstance(provider_state, dict) else {}
    except (TypeError, ValueError):
        source_roles = {}
    if isinstance(source_roles, dict):
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            name = scope.get("bookmaker") or scope.get("key")
            state = source_roles.get(name)
            if isinstance(state, dict) and state.get("role") in {"degraded", "offline"}:
                return "degraded_source"
    # /props documents x-result-degraded as comma-separated *book keys*.
    # A failed Pinnacle read must not disqualify a complete FanDuel row from
    # the same response. Unknown/global header formats remain conservative.
    degraded = headers.get("x-result-degraded") or headers.get("x-result-degraded-sources")
    if degraded:
        affected = {part.strip().lower() for part in str(degraded).split(",") if part.strip()}
        names = {str(scope.get("bookmaker") or scope.get("key") or "").lower()
                 for scope in scopes if isinstance(scope, dict)}
        if not names or affected & names or affected & {"true", "all", "*"}:
            return "degraded_source"
    if any(scope.get("partial") is True or scope.get("missing_books")
           or scope.get("snapshot_partial_reasons") or scope.get("snapshot_in_progress") is True
           for scope in all_scopes):
        return "partial_response"
    if any(scope.get("truncated") is True or scope.get("truncated_books") for scope in all_scopes):
        return "truncated_response"
    if str(headers.get("x-result-truncated", "")).lower() == "true":
        return "truncated_response"
    if any(scope.get("snapshot_complete") is False for scope in all_scopes):
        return "incomplete_snapshot"
    if str(headers.get("x-result-has-more", "")).lower() == "true" or headers.get("x-next-offset"):
        return "paginated_response_not_atomic"
    return None


def _parlay_observation(envelope: ProviderEnvelope, row: dict, *, complete: bool,
                        scopes: tuple[dict, ...] = ()) -> dict[str, Any]:
    # /docs#market-aliases-offer-identity-and-completeness documents this exact
    # pair. last_update has varying legacy meanings; verified_at is a fixture
    # heartbeat. Neither establishes quote-bound collector verification.
    # The reference describes last_observed semantically but the published
    # AsyncAPI does not specify numeric units. ISO with an offset is unambiguous;
    # guessing seconds/milliseconds from magnitude would manufacture evidence.
    raw_observed = row.get("last_observed")
    observed = parse_timestamp(raw_observed) if isinstance(raw_observed, str) else None
    verified = ((envelope.provider_id != "parlay_rest" or envelope.kind == "props_snapshot")
                and observed is not None
                and row.get("last_update_type") == "collector_observation")
    # The separate Pinnacle current-state stream identifies its own rows with
    # source_ts_ms/last_observed_ms. A generic complete game-board frame never
    # inherits this observation from a heartbeat or nearby Pinnacle row.
    current_state = (envelope.provider_id != "parlay_rest" and envelope.kind == "odds_update"
                     and row.get("bookmaker") == "pinnacle"
                     and row.get("source_ts_ms") is not None
                     and row.get("last_observed_ms") is not None)
    if current_state:
        observed = parse_timestamp(row["last_observed_ms"], epoch_unit="milliseconds")
        verified = observed is not None and row.get("action") != "remove" and row.get("available") is True
    problem = _observation_problem(envelope, *scopes, row)
    if observed is not None and observed > envelope.received_at:
        problem, verified = "future_observation_clock", False
    try:
        age = (Decimal(str(row["price_age_s"])) if envelope.provider_id != "parlay_rest"
               and not isinstance(row.get("price_age_s"), bool) and row.get("price_age_s") is not None
               else None)
        if age is not None and (not age.is_finite() or age < 0):
            age = None
    except (InvalidOperation, ValueError):
        age = None
    return {
        "source_observed_at": observed,
        "observation_verified": verified,
        "observation_complete": verified and complete and problem is None,
        "source_price_changed_at": (None if envelope.provider_id == "parlay_rest" else
            parse_timestamp(row.get("line_changed_at_ms"), epoch_unit="milliseconds")),
        "provider_reported_price_age_s": age,
        "source_stored_at": (parse_timestamp(row.get("line_changed_at_ms"), epoch_unit="milliseconds")
            if envelope.provider_id == "parlay_rest" and envelope.kind == "snapshot" else
            parse_timestamp(row.get("last_update"), epoch_unit="milliseconds")
            if envelope.provider_id == "parlay_rest" and envelope.kind == "props_snapshot" else None),
        "verification_reason": problem or ("quote_bound_collector_observation" if verified else "unverified_observation_clock"),
    }


def _toa_offers(envelope: ProviderEnvelope, events: list, *, odds_format: str) -> list[SourceOffer]:
    result = []
    for event in events:
        if not isinstance(event, dict):
            continue
        # Preserve supplied discovery evidence. A quote's event identity is not
        # its scheduled time; neither field supplies period/settlement rules.
        event_context = {"event_sport": event.get("sport_key") or event.get("sport"),
                         "participants": [event.get("home_team"), event.get("away_team")]}
        if "commence_time" in event:
            event_context["scheduled_start"] = event["commence_time"]
        if "id" in event and "eventId" in event:
            event_context["provider_event_ids"] = {"id": event["id"], "eventId": event["eventId"]}
        for book in event.get("bookmakers", []):
            poll_at = parse_timestamp(book.get("verified_at")) if isinstance(book.get("verified_at"), str) else None
            poll_current = book.get("is_current") if type(book.get("is_current")) is bool else None
            for market in book.get("markets", []):
                outcomes = market.get("outcomes", [])
                prices, points = {}, {}
                for outcome in outcomes:
                    name = str(outcome["name"])
                    if name in prices:
                        raise ValueError("Duplicate outcome in market")
                    prices[name] = decimal_price(outcome["price"], odds_format)
                    points[name] = number_identity(outcome.get("point"))
                # Support confirmed book.line_changed_at_ms is the oldest
                # stored served row. market.last_update has a different and
                # ambiguous scope, so it cannot fill a missing stored clock.
                observed = parse_timestamp(market.get("last_update"))
                stored = parse_timestamp(book.get("line_changed_at_ms"), epoch_unit="milliseconds")
                status = ("suspended" if any(_explicitly_unavailable(scope)
                    for scope in (event, book, market, *outcomes)) else "active")
                # Links certify a stored OPEN/ACTIVE observation, potentially
                # five minutes old; they do not prove present market status.
                historical_link_active = (
                    envelope.provider_id == "parlay_rest"
                    and envelope.kind == "snapshot"
                    and book.get("key") == "fanduel"
                    and market.get("key") in {"h2h", "spreads", "totals"}
                    and len(prices) >= 2 and status == "active"
                    and all(_valid_fanduel_link(outcome.get("link")) for outcome in outcomes)
                    and _observation_problem(envelope, event, book, market) is None
                )
                result.append(SourceOffer(
                    envelope.provider_id, str(book["key"]), str(event.get("eventId") or event.get("id")),
                    str(market["key"]), None, None, market.get("period"), points, prices,
                    observed, envelope.published_at, status,
                    {**_row_context(market), **event_context},
                    **({**_parlay_observation(envelope, market, complete=len(prices) >= 2,
                                             scopes=(event, book)), "source_stored_at": stored}
                       if envelope.provider_id.startswith("parlay") else {}),
                    # A FanDuel link reports OPEN/ACTIVE at the stored
                    # observation, potentially minutes ago, not now.
                    market_status_verified=False,
                    source_status_historical_hint=historical_link_active,
                    source_scope_poll_at=poll_at,
                    source_scope_is_current=poll_current,
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
                        status, {**_row_context(market), **event_context, "alternate": True},
                        # An alternate rung has no documented quote-bound
                        # observation pair. It cannot inherit the main clock.
                        verification_reason="alternate_observation_unverified",
                        source_scope_poll_at=poll_at,
                        source_scope_is_current=poll_current,
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
        # Observed Parlay game/h2h and game/spreads transport variants: a named outcome's
        # documented American `price` is duplicated in `over_price` by the
        # source row container. It is NOT a separate over/under selection.
        # Accept only an exact alias in this narrow shape; preserve all other
        # conflicting-shape checks and never synthesize the opposing side.
        try:
            spread_line = (row.get("market_key") == "spreads" and row.get("line") is not None
                           and not isinstance(row["line"], bool) and Decimal(str(row["line"])).is_finite())
        except InvalidOperation:
            spread_line = False
        named_game_line = row.get("market_key") == "h2h" and row.get("line") is None or spread_line
        if (has_totals and has_outcome and not has_moneyline
                and row.get("kind") == "game" and named_game_line
                and isinstance(row.get("team"), str) and row["team"].strip()
                and row.get("price") is not None and row.get("over_price") is not None
                and row.get("under_price") is None
                and row.get("total") is None and row.get("player") is None
                and row.get("outcome", row["team"]) == row["team"]):
            if _parlay_american_price(row["over_price"]) != _parlay_outcome_price(row):
                raise ValueError("Conflicting Parlay named-outcome price alias")
            has_totals = False
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
            "suspended" if _explicitly_unavailable(row) else "active"
        )
        observation = _parlay_observation(envelope, row, complete=len(prices) >= 2)
        # Both FanDuel links are historical OPEN/ACTIVE hints, not proof of
        # current selection availability. A single link proves neither side.
        historical_link_active = (envelope.provider_id == "parlay_rest" and envelope.kind == "props_snapshot"
                           and row.get("bookmaker") == "fanduel" and has_totals and not has_outcome
                           and not has_moneyline and set(prices) == {"over", "under"}
                           and all(_valid_fanduel_link(row.get(f"{side}_link"))
                                   for side in ("over", "under"))
                           and status == "active")
        selection_ids = {side: row[f"{side}_sid"] for side in ("over", "under")
                         if side in prices and isinstance(row.get(f"{side}_sid"), str)
                         and row[f"{side}_sid"].strip()}
        result.append(SourceOffer(
            envelope.provider_id, str(row.get("bookmaker", "")),
            str(row.get("canonical_event_id") or row.get("event_id") or row.get("eventId") or ""),
            str(row.get("market_key", "")), row.get("player"),
            number_identity(row.get("line") if row.get("line") is not None else row.get("total")),
            row.get("period"), points, prices,
            parse_timestamp(row.get("last_update"), epoch_unit="milliseconds"),
            envelope.published_at, status,
            {**_row_context(row), "participants": [row.get("home_team"), row.get("away_team")],
             **({"scheduled_start": row["commence_time"]} if "commence_time" in row else {}),
             **({"event_sport": envelope.metadata["sport"]} if envelope.metadata.get("sport") else {})},
            **observation,
            market_status_verified=False,
            source_status_historical_hint=historical_link_active,
            source_selection_ids=selection_ids,
            received_at=envelope.received_at,
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


def _owls_pinnacle_realtime_offers(envelope: ProviderEnvelope) -> list[SourceOffer]:
    payload = envelope.payload
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise ValueError("Expected Owls realtime snapshot and metadata")
    meta = payload["meta"]
    if meta.get("source") != "pinnacle":
        return []
    events = payload.get("data")
    if not isinstance(events, list):
        raise ValueError("Expected Owls realtime event list")
    feed = meta.get("feed") if isinstance(meta.get("feed"), dict) else {}
    freshness = meta.get("freshness") if isinstance(meta.get("freshness"), dict) else {}
    feed_ready = (meta.get("available") is True and freshness.get("stale") is False
                  and (feed.get("resubscribing") is None or feed.get("resubscribing") is False))
    result = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Expected Owls realtime event")
        phase = event.get("phase")
        start = parse_timestamp(event.get("commence_time"))
        pregame = (phase == "scheduled" and event.get("status") in {None, "scheduled"}
                   and (event.get("isLive") is None or event.get("isLive") is False)
                   and start is not None
                   and start > envelope.received_at)
        context = {"event_sport": event.get("sport_key") or meta.get("sport"),
                   "participants": [event.get("home_team"), event.get("away_team")],
                   "scheduled_start": event.get("commence_time")}
        if event.get("market_type") == "outright":
            continue
        for book in event.get("bookmakers", []):
            if not isinstance(book, dict) or book.get("key") != "pinnacle":
                continue
            for market in book.get("markets", []):
                if not isinstance(market, dict) or market.get("key") not in {"h2h", "spreads", "totals"}:
                    continue
                outcomes = market.get("outcomes")
                if not isinstance(outcomes, list):
                    raise ValueError("Expected Owls realtime outcomes")
                prices, points = {}, {}
                for outcome in outcomes:
                    name = str(outcome["name"])
                    if name in prices:
                        raise ValueError("Duplicate Owls realtime outcome")
                    prices[name] = decimal_price(outcome["price"], "american")
                    points[name] = number_identity(outcome.get("point"))
                seen = parse_timestamp(market.get("last_seen"))
                changed = parse_timestamp(market.get("last_update"))
                # The documented last_seen confirms this individual main
                # market, even if its price did not change. Event-level seenAt,
                # sport-level freshness and REST arrival cannot replace it.
                seen_valid = (seen is not None and seen <= envelope.received_at
                              and (changed is None or changed <= seen))
                status_active = (feed_ready and pregame
                                 and (market.get("suspended") is None or market.get("suspended") is False)
                                 and seen_valid)
                verified = status_active
                result.append(SourceOffer(
                    envelope.provider_id, "pinnacle", str(event.get("eventId") or event.get("id") or ""),
                    str(market["key"]), None, None, market.get("period"), points, prices,
                    changed, envelope.published_at,
                    "active" if status_active else "suspended",
                    {**_row_context(market), **context},
                    source_observed_at=seen if seen_valid else None,
                    observation_verified=verified,
                    observation_complete=verified and len(prices) >= 2,
                    source_price_changed_at=changed,
                    market_status_verified=verified,
                    verification_reason=("owls_pinnacle_market_last_seen"
                                         if verified else "owls_pinnacle_feed_status_or_market_seen_unverified"),
                ))
    return result


def _oddspapi_offers(envelope: ProviderEnvelope) -> list[SourceOffer]:
    fixture = envelope.payload
    discovered = envelope.metadata.get("fixture_discovery", {})
    if not isinstance(discovered, dict) or (
        discovered.get("fixtureId") != fixture.get("fixtureId")
        or discovered.get("participant1Id") != fixture.get("participant1Id")
        or discovered.get("participant2Id") != fixture.get("participant2Id")
    ):
        discovered = {}
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
                # The v4 snapshot explicitly reports fixture, bookmaker, market
                # and selection availability. Require affirmative evidence at
                # every level; an omitted flag is not an active market. These
                # flags verify availability, not when the book last observed
                # unchanged prices (bookmakerChangedAt is a change clock).
                active = (fixture.get("statusId") in {0, 1} and fixture.get("hasOdds") is True
                          and book.get("bookmakerIsActive") is True
                          and book.get("suspended") is False and market.get("marketActive") is True
                          and bool(outcomes) and all(row.get("active") is True for row in outcomes.values()))
                # Provider recorded-change time and bookmaker clock are kept separate.
                clock_pairs = [(parse_timestamp(row.get("bookmakerChangedAt")),
                                parse_timestamp(row.get("changedAt"))) for row in outcomes.values()]
                source_time = _oldest([book_clock for book_clock, _ in clock_pairs])
                provider_time = _oldest([recorded_clock for _, recorded_clock in clock_pairs])
                # v4 documents both clocks as *changes*, not quote observations.
                # Availability flags do not prove an unchanged selection was
                # re-observed in this REST response. Retain the change clocks
                # separately and leave observation eligibility unknown.
                result.append(SourceOffer(
                    envelope.provider_id, book_key, str(fixture.get("fixtureId", "")), str(market_id),
                    None if player_id == "0" else str(player_id),
                    number_identity(catalog.get("handicap")), catalog.get("period"),
                    {key: None for key in outcomes}, prices, source_time, provider_time,
                    "active" if active else "suspended",
                    {"bookmaker_market_id": market.get("bookmakerMarketId"),
                     "native_outcomes": {key: row.get("bookmakerOutcomeId") for key, row in outcomes.items()},
                     "participants": [discovered.get("participant1Name") or fixture.get("participant1Name")
                                      or fixture.get("participant1Id"),
                                      discovered.get("participant2Name") or fixture.get("participant2Name")
                                      or fixture.get("participant2Id")],
                     "participant_ids": [fixture.get("participant1Id"), fixture.get("participant2Id")],
                     "scheduled_start": discovered.get("startTime") or fixture.get("startTime"),
                     "event_sport": fixture.get("sportId"),
                     "tournament_id": fixture.get("tournamentId"),
                     "event_league": discovered.get("tournamentName") or fixture.get("tournamentName"),
                     "market_catalog": {key: catalog[key] for key in (
                         "marketType", "marketName", "playerProp", "period", "handicap",
                     ) if key in catalog}},
                    source_observed_at=None,
                    observation_verified=False,
                    observation_complete=False,
                    source_price_changed_at=source_time,
                    market_status_verified=active,
                    verification_reason=("odds_papi_v4_change_clocks_not_quote_observation"
                                         if active else "odds_papi_availability_missing_or_inactive"),
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
        ref = envelope.metadata.get("event_ref")
        if ref is not None and (not isinstance(ref, dict)
                                or ref.get("event_id") != (payload.get("event_id") or envelope.metadata.get("event_id"))
                                or ref.get("sport") != (payload.get("sport") or envelope.metadata.get("sport"))
                                or any(not isinstance(ref.get(key), str) or not ref[key].strip()
                                       for key in ("team1", "team2", "start_date"))
                                or ref["team1"] == ref["team2"]
                                or parse_timestamp(ref["start_date"]) is None):
            raise ValueError("Oddize prop event reference does not match response identity")
        events = [{"event_id": payload.get("event_id") or envelope.metadata.get("event_id"),
                   "sport": payload.get("sport") or envelope.metadata.get("sport"),
                   **({key: ref[key] for key in ("team1", "team2", "start_date")}
                      if isinstance(ref, dict) else {}),
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
    result = []
    for group in grouped.values():
        observed = _oldest(group["times"])
        verified = observed is not None and all(stamp <= envelope.received_at for stamp in group["times"])
        problem = _observation_problem(envelope)
        if any(stamp is not None and stamp > envelope.received_at for stamp in group["times"]):
            problem = "future_observation_clock"
        # Per-side timestamps must identify the same observation before this
        # pair can be used by strict profiles. No guessed skew tolerance.
        synchronized = len(set(group["times"])) == 1
        complete = verified and len(group["prices"]) >= 2 and synchronized and problem is None
        result.append(SourceOffer(
            envelope.provider_id, group["bookmaker"], group["event_id"], group["market_key"],
            group["player_key"], group["line"], None, group["points"], group["prices"],
            # Oddize publishes an observation clock but no current
            # sportsbook-market status. Do not turn a listed price into an
            # affirmative ACTIVE claim.
            observed, None, "unknown", group["context"],
            source_observed_at=observed, observation_verified=verified,
            observation_complete=complete,
            verification_reason=problem or ("unsynchronized_outcomes" if not synchronized else
                "oddize_price_observation" if verified else "unverified_observation_clock"),
            # OpenAPI Price/PropRow has no current market-status declaration.
            # A timestamp alone never certifies that the quote is still offered.
            market_status_verified=False,
        ))
    return result


def extract_offers(envelope: ProviderEnvelope) -> list[SourceOffer]:
    if envelope.kind not in {"snapshot", "props_snapshot", "initial_state", "odds_update"}:
        return []
    if envelope.provider_id in {"parlay_ws", "parlay_game_ws"}:
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
    if envelope.provider_id == "owls_pinnacle_realtime":
        return _owls_pinnacle_realtime_offers(envelope)
    if envelope.provider_id == "oddspapi_rest":
        return _oddspapi_offers(envelope)
    if envelope.provider_id == "oddize_rest":
        return _oddize_offers(envelope)
    return []


class TrustedSourceProjector:
    """Source-identity-only markets for the existing pricing engine.

    These markets deliberately have a separate ID namespace and unreviewed
    payout rules. They can show a fair probability, never a native order.
    No team/player alias or missing opposing selection is guessed here.
    """
    SPORTS = {
        "basketball_nba": ("basketball", "NBA"),
        "americanfootball_nfl": ("football", "NFL"),
        "baseball_mlb": ("baseball", "MLB"),
        "icehockey_nhl": ("hockey", "NHL"),
        "americanfootball_ncaaf": ("football", "NCAAF"),
        "basketball_ncaab": ("basketball", "NCAABM"),
        "basketball_ncaabm": ("basketball", "NCAABM"),
        "tennis_atp": ("tennis", "ATP"),
        "tennis_wta": ("tennis", "WTA"),
        "soccer_epl": ("soccer", "EPL"),
        "soccer_spain_la_liga": ("soccer", "LA_LIGA"),
        "soccer_germany_bundesliga": ("soccer", "BUNDESLIGA"),
        "soccer_italy_serie_a": ("soccer", "SERIE_A"),
        "soccer_france_ligue_one": ("soccer", "LIGUE_1"),
        "soccer_usa_mls": ("soccer", "MLS"),
    }

    def __init__(self, pricing_getter):
        self.pricing_getter = pricing_getter
        self.active: dict[tuple, tuple[Market, Quote]] = {}
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self.ambiguous_events: set[tuple[str, str]] = set()

    @staticmethod
    def _key(offer: SourceOffer) -> tuple:
        return (offer.provider, offer.bookmaker, offer.event_id, offer.market_key,
                offer.player_key, offer.line, offer.period)

    def invalidate(self, provider: str, sport: str | None = None) -> list[tuple[Market, Quote]]:
        updates = []
        for key, (market, quote) in list(self.active.items()):
            if key[0] == provider and (sport is None or market.extensions.get("provider_sport") == sport):
                updates.append((market, quote.model_copy(update={"status": "withdrawn"})))
                del self.active[key]
        return updates

    def invalidate_scope(self, provider: str, selectors: dict[str, str],
                         sport: str | None = None) -> list[tuple[Market, Quote]]:
        """Withdraw research quotes when a named source row is removed or fails."""
        positions = {"bookmaker": 1, "event_id": 2, "market_key": 3}
        updates = []
        for key, (market, quote) in list(self.active.items()):
            if (key[0] != provider
                    or sport is not None and market.extensions.get("provider_sport") != sport
                    or any(key[positions[name]] != value for name, value in selectors.items()
                           if name in positions)):
                continue
            updates.append((market, quote.model_copy(update={"status": "withdrawn"})))
            del self.active[key]
        return updates

    @classmethod
    def _market_quote(cls, envelope: ProviderEnvelope, offer: SourceOffer):
        from evplatform.core.profile_import import STATISTICS
        sport_key = offer.context.get("event_sport") or envelope.metadata.get("sport")
        sport_league = cls.SPORTS.get(sport_key)
        names = offer.context.get("participants")
        start = parse_timestamp(offer.context.get("scheduled_start"))
        if (sport_league is None or not offer.event_id or not offer.bookmaker
                or not isinstance(names, list) or len(names) != 2
                or any(not isinstance(name, str) or not name.strip() for name in names)
                or names[0] == names[1] or start is None):
            return None
        inplay = start <= envelope.received_at
        if inplay and (envelope.provider_id not in {"parlay_ws", "parlay_game_ws"}
                or envelope.kind != "odds_update" or offer.observed_at is None
                or not start <= offer.observed_at <= envelope.received_at):
            # A delivery, heartbeat, or old prematch row does not establish an
            # in-play price. This is still research: price movement is not a
            # verified event-state or selection-availability observation.
            return None
        if offer.market_key in {"h2h", "spreads", "totals"} and offer.period not in {None, "FULL", "full_game"}:
            return None  # A period market is not the full-game research profile.
        if sport_league[0] == "soccer" and offer.market_key == "h2h":
            # A missing draw price does not establish a two-way winner market.
            return None
        market_type, statistic, player, line = None, None, None, None
        if offer.market_key == "h2h" and offer.line is None and offer.player_key is None:
            market_type = "moneyline"
            outcomes = {"home": names[0], "away": names[1], names[0]: names[0], names[1]: names[1]}
            if set(offer.prices) == {"home", "away"} or set(offer.prices) == set(names):
                odds = {outcomes[key]: price for key, price in offer.prices.items()}
            else:
                return None  # Three-way and ambiguous winner markets need their own profile.
        elif offer.market_key in {"spreads", "totals"} and len(offer.prices) == 2:
            market_type = "spread" if offer.market_key == "spreads" else "total"
            points = [number_identity(value) for value in offer.outcome_points.values()]
            if any(value is None for value in points):
                return None
            if market_type == "total":
                if set(offer.prices) != {"Over", "Under"} and set(offer.prices) != {"over", "under"}:
                    return None
                if len(set(points)) != 1:
                    return None
                line = Decimal(points[0])
                odds = {key.lower(): price for key, price in offer.prices.items()}
            else:
                if set(offer.prices) != set(names) or (Decimal(offer.outcome_points[names[0]])
                        != -Decimal(offer.outcome_points[names[1]])):
                    return None
                line = abs(Decimal(offer.outcome_points[names[0]]))
                # A home -3.5 spread is a different selection from home +3.5.
                # Keep the signed side in the normalized outcome identity.
                odds = {f"{name} {Decimal(offer.outcome_points[name]):+g}": offer.prices[name]
                        for name in names}
        elif offer.market_key in STATISTICS and offer.player_key and offer.line is not None:
            # The REST contract distinguishes FULL from Q1/1H/P1/F5 and legacy
            # UNKNOWN. A full-game research profile must not price the latter.
            if offer.period not in {"FULL", "full_game"}:
                return None
            market_type, statistic, player = "player_prop", STATISTICS[offer.market_key], offer.player_key
            if {key.removeprefix(player + " ").lower() for key in offer.prices} != {"over", "under"}:
                return None
            if set(offer.outcome_points.values()) != {offer.line}:
                return None
            line = Decimal(offer.line)
            odds = {key.removeprefix(player + " ").lower(): price for key, price in offer.prices.items()}
        else:
            return None
        if len(odds) != 2 or any(not price.is_finite() or price <= 1 for price in odds.values()):
            return None
        period = ("FULL" if offer.market_key in {"h2h", "spreads", "totals"}
                  or offer.period in {"FULL", "full_game"} else offer.period)
        identity = {"source": "parlay", "sport": sport_key, "event": offer.event_id,
                    "start": start.isoformat(), "participants": names, "market": offer.market_key,
                    "player": player, "line": str(line) if line is not None else None,
                    "period": period, "outcomes": sorted(odds)}
        if inplay:
            # Preserve every existing pregame research ID and give post-start
            # rows a separate namespace so a prematch quote cannot join them.
            identity["phase"] = "inplay"
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
        push_possible = line is not None and line == line.to_integral_value()
        if push_possible:
            return None  # Conditional no-push odds need a separate probability model.
        market = Market(id=f"research:parlay:{digest}", event_id=f"research:parlay:{offer.event_id}",
            event_name=f"{names[1]} at {names[0]}", sport=sport_league[0], league=sport_league[1],
            participants=tuple(names), start_time=start,
            status="live" if inplay else "scheduled", market_type=market_type,
            player=player, player_id=player, statistic=statistic, line=line,
            outcomes=tuple(sorted(odds)), settlement=Settlement(
                period=period, overtime="unverified", void_rule="unreviewed_source_rules",
                push_rule="possible" if push_possible else "unverified",
                payout=Decimal(1)),
            original_ids={offer.provider: offer.event_id}, extensions={
                "research_only": True, "provider_sport": sport_key,
                "source_market_key": offer.market_key, "native_mapping_reviewed": False,
                "normal_outcome_reviewed": False, "source_period": offer.period,
                "inplay_price_after_start": inplay,
                "event_state_verified": False})
        status = offer.status if offer.status in {"suspended", "withdrawn"} else (
            "active" if offer.market_status_verified else "unknown")
        quote = Quote(market_id=market.id, provider=offer.provider, bookmaker=offer.bookmaker,
            market_fingerprint=market.identity(), decimal_odds=odds, status=status,
            source_time=offer.observed_at, source_observed_at=offer.source_observed_at,
            source_price_changed_at=offer.source_price_changed_at,
            provider_reported_price_age_s=offer.provider_reported_price_age_s,
            source_stored_at=offer.source_stored_at,
            source_selection_ids=offer.source_selection_ids,
            source_status_historical_hint=offer.source_status_historical_hint,
            observation_verified=offer.observation_verified,
            observation_complete=offer.observation_complete,
            market_status_verified=offer.market_status_verified,
            source_scope_poll_at=offer.source_scope_poll_at,
            source_scope_is_current=offer.source_scope_is_current,
            verification_reason=offer.verification_reason,
            settlement_equivalence_verified=False, normal_outcome_verified=False,
            received_at=offer.received_at or envelope.received_at, provider_time=offer.provider_time,
            synthetic=envelope.synthetic, original_market_id=offer.market_key)
        return market, quote

    def apply(self, envelope: ProviderEnvelope, offers: list[SourceOffer]) -> list[tuple[Market, Quote]]:
        from evplatform.core.pricing import market_config
        from evplatform.core.reference_profiles import profile_for_market
        updates = []
        # Focused REST prop rows may omit fixture names and start. The exact
        # /events lookup supplies identity context only; it never supplies a
        # quote observation clock or active-selection attestation.
        focused_event = envelope.metadata.get("focused_event")
        if (envelope.provider_id == "parlay_rest" and envelope.kind == "props_snapshot"
                and isinstance(focused_event, dict)):
            event_id = focused_event.get("id")
            names = [focused_event.get("home_team"), focused_event.get("away_team")]
            start = parse_timestamp(focused_event.get("commence_time"))
            sport = envelope.metadata.get("sport")
            if (offers and isinstance(event_id, str) and isinstance(sport, str)
                    and all(isinstance(name, str) and name.strip() for name in names)
                    and names[0] != names[1] and start is not None
                    and all(offer.event_id == event_id for offer in offers)):
                event_key = sport, event_id
                evidence = {"scheduled_start": start.isoformat(),
                            "participants": names, "event_sport": sport}
                existing = self.events.get(event_key)
                if event_key not in self.ambiguous_events:
                    if existing is None or existing == evidence:
                        self.events[event_key] = evidence
                    else:
                        self.events.pop(event_key, None)
                        self.ambiguous_events.add(event_key)
        if envelope.kind in {"feed_invalidated", "disconnected", "reconnect", "initial_state"}:
            sport = envelope.metadata.get("sport") or (
                envelope.payload.get("sport") if isinstance(envelope.payload, dict) else None)
            updates.extend(self.invalidate(envelope.provider_id, sport if isinstance(sport, str) else None))
        for offer in offers:
            context = offer.context
            if (offer.provider.startswith("parlay") and offer.market_key in {"h2h", "spreads", "totals"}
                    and offer.event_id and parse_timestamp(context.get("scheduled_start")) is not None
                    and isinstance(context.get("participants"), list)
                    and len(context["participants"]) == 2):
                event_key = str(context.get("event_sport") or envelope.metadata.get("sport")), offer.event_id
                existing = self.events.get(event_key)
                evidence = {"scheduled_start": parse_timestamp(context["scheduled_start"]).isoformat(),
                            "participants": context["participants"], "event_sport": event_key[0]}
                if event_key in self.ambiguous_events:
                    continue
                if existing is None or existing == evidence:
                    self.events[event_key] = evidence
                else:
                    self.events.pop(event_key, None)
                    self.ambiguous_events.add(event_key)
        for offer in offers:
            if not offer.provider.startswith("parlay"):
                continue
            event_key = str(offer.context.get("event_sport") or envelope.metadata.get("sport")), offer.event_id
            if offer.player_key and event_key in self.events:
                context = dict(offer.context)
                for field_name in ("scheduled_start", "participants", "event_sport"):
                    if (field_name not in context or context[field_name] is None
                            or field_name == "participants" and not all(context[field_name])):
                        context[field_name] = self.events[event_key][field_name]
                offer = replace(offer, context=context)
            key = self._key(offer)
            try:
                projected = self._market_quote(envelope, offer)
            except (InvalidOperation, ValueError, TypeError):
                # A malformed line invalidates this one displayed research
                # quote, not the entire provider stream or its other markets.
                projected = None
            if projected is None or offer.status in {"suspended", "withdrawn"}:
                prior = self.active.pop(key, None)
                if prior is not None:
                    updates.append((prior[0], prior[1].model_copy(update={"status": "withdrawn"})))
                continue
            market, quote = projected
            config = market_config(self.pricing_getter(), market)
            policy = config.trusted_pilot_policy
            scope = policy.market_scope
            full_game_winner = (offer.market_key == "h2h"
                and (offer.period in {"FULL", "full_game"}
                    or offer.provider == "parlay_game_ws" and offer.period is None)
                and market.market_type == scope.market_type == "moneyline"
                and len(market.participants) == 2
                and set(market.outcomes) == set(market.participants)) if scope is not None else False
            full_game_prop = (offer.period in {"FULL", "full_game"}
                and market.market_type == scope.market_type == "player_prop"
                and market.player and market.player_id
                and market.statistic in scope.statistics
                and market.line is not None
                and market.line != market.line.to_integral_value()
                and set(market.outcomes) == {"over", "under"}) if scope is not None else False
            if (market.status == "scheduled" and policy.enabled and policy.owner_approved and scope is not None
                    and scope.source_normal_rule_evidence
                    and config.normal_outcome_policy == "winner_only_binary"
                    # Unsuffixed game h2h is documented as full-game. Props
                    # require an explicit full-game period and exact half line.
                    # The reviewed rule attests ordinary overtime/participation
                    # scope; it never supplies quote freshness or active status.
                    and (full_game_winner or full_game_prop)
                    and offer.provider in {policy.provider_id, policy.crosscheck_provider_id}
                    and offer.bookmaker in policy.trusted_books
                    and market.sport == scope.sport and market.league == scope.league
                    and market.settlement.period == "FULL"):
                # Approval establishes only the ordinary full-game question.
                # It never converts a heartbeat to quote observation or an
                # unknown market status to affirmative active evidence.
                settlement = market.settlement.model_copy(update={
                    "overtime": "included", "push_rule": "not_applicable"})
                market = market.model_copy(update={"id": market.id.replace("research:", "reviewed:", 1),
                    "settlement": settlement, "extensions": {**market.extensions,
                        "research_only": False, "normal_outcome_reviewed": True,
                        "reviewed_normal_rule_id": scope.reviewed_normal_rule_id,
                        "source_normal_rule_evidence": scope.source_normal_rule_evidence}})
                quote = quote.model_copy(update={"market_id": market.id,
                    "market_fingerprint": market.identity(), "normal_outcome_verified": True,
                    "normal_outcome_evidence": (scope.reviewed_normal_rule_id + ": "
                        + scope.source_normal_rule_evidence)})
            if config.reference_profile_mode != "strict" or not (
                    config.reference_quality_mode == "trusted_provider"
                    or config.multi_book_consensus_enabled):
                continue
            if not config.multi_book_consensus_enabled:
                try:
                    if profile_for_market(config, market, envelope.received_at) is None:
                        continue
                except ValueError:
                    continue
            prior = self.active.get(key)
            if (prior is not None and prior[0].id == market.id
                    and prior[1].source_time is not None and quote.source_time is not None
                    and quote.source_time < prior[1].source_time):
                # A delayed stream frame must never replace a newer price.
                continue
            if prior is not None and prior[0].id != market.id:
                updates.append((prior[0], prior[1].model_copy(update={"status": "withdrawn"})))
            self.active[key] = market, quote
            updates.append((market, quote))
        return updates


class VerifiedProviderMapper:
    def __init__(self, mappings: list[VerifiedOfferMapping] | None = None, *, quarantine_limit: int = 1000):
        self.mappings: dict[str, VerifiedOfferMapping] = {}
        self.quarantine: OrderedDict[str, dict] = OrderedDict()
        self.quarantine_limit = quarantine_limit
        self.mapped_count = 0
        self.rejected_count = 0
        self._last: dict[tuple[str, str], tuple[SourceOffer, Market, Quote, str | None]] = {}
        self._partial: dict[str, dict[str, SourceOffer]] = {}
        self._verified_watermarks: dict[tuple[str, str], datetime] = {}
        self._verification_latches: set[tuple[str, str]] = set()
        self.research_projector: TrustedSourceProjector | None = None
        self.research_updates: list[tuple[Market, Quote]] = []
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

    def _quarantine(self, offer: SourceOffer | None, reason: str, provider: str,
                    *, failed_scope: dict | None = None) -> None:
        self.rejected_count += 1
        key = offer.signature if offer else f"invalid:{provider}"
        self.quarantine[key] = {
            "signature": key, "provider": provider, "reason": reason,
            "identity": offer.identity() if offer else None,
            "observation": ({
                "source_time": offer.observed_at.isoformat() if offer.observed_at else None,
                "provider_time": offer.provider_time.isoformat() if offer.provider_time else None,
                "source_observed_at": offer.source_observed_at.isoformat() if offer.source_observed_at else None,
                "source_price_changed_at": offer.source_price_changed_at.isoformat() if offer.source_price_changed_at else None,
                "observation_verified": offer.observation_verified,
                "observation_complete": offer.observation_complete,
                "market_status_verified": offer.market_status_verified,
                "source_scope_poll_at": offer.source_scope_poll_at.isoformat() if offer.source_scope_poll_at else None,
                "source_scope_is_current": offer.source_scope_is_current,
                "verification_reason": offer.verification_reason,
                "status": offer.status,
                "decimal_odds": {name: str(value) for name, value in offer.prices.items()},
            } if offer else None),
            **({"affected_source_scope": failed_scope} if failed_scope is not None else {}),
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
            suspended = quote.model_copy(update={"status": "suspended", "received_at": envelope.received_at,
                "observation_complete": False, "market_status_verified": False,
                "verification_reason": "feed_or_mapping_invalidated"})
            if key in self._verified_watermarks:
                self._verification_latches.add(key)
            self._last[key] = (offer, market, suspended, cached_scope)
            result.append((market, suspended))
        return result

    def _combine_partial_outcomes(self, offers: list[SourceOffer]) -> list[SourceOffer]:
        complete = []
        changed = {}
        current_sides: dict[str, set[str]] = {}
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
            streaming_pair = offer.provider in {"parlay_ws", "parlay_game_ws"} and (moneyline or paired_prop)
            snapshot_pair = offer.provider == "parlay_rest" and paired_prop
            if len(offer.prices) != 1 or not (streaming_pair or snapshot_pair):
                # A complete current game-board baseline may use home/away
                # keys while later change frames use named teams. Seed the
                # exact market identity with both sides so a one-sided delta
                # can update the board without inventing a new receipt time
                # for the unchanged opposing price. The stored side clocks
                # remain separate and the merge is research-only.
                participants = offer.context.get("participants")
                if (offer.provider == "parlay_game_ws" and offer.market_key == "h2h"
                        and moneyline and len(offer.prices) == 2
                        and isinstance(participants, list) and len(participants) == 2):
                    if set(offer.prices) == {"home", "away"}:
                        names = {"home": participants[0], "away": participants[1]}
                    elif set(offer.prices) == set(participants):
                        names = {name: name for name in participants}
                    else:
                        names = {}
                    if len(names) == 2:
                        identity = offer.identity()
                        identity.pop("outcome_points")
                        key = json.dumps(identity, sort_keys=True, default=str)
                        self._partial[key] = {names[side]: replace(offer,
                            prices={names[side]: price},
                            outcome_points={names[side]: offer.outcome_points.get(side)})
                            for side, price in offer.prices.items()}
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
            previous_clock = (previous.source_observed_at if previous.observation_verified else previous.observed_at) if previous else None
            current_clock = offer.source_observed_at if offer.observation_verified else offer.observed_at
            degraded = previous and any(getattr(previous, name) and not getattr(offer, name) for name in (
                "observation_verified", "observation_complete", "market_status_verified"))
            if previous_clock and current_clock and current_clock < previous_clock and not degraded:
                continue
            sides[side] = offer
            changed[key] = (offer, sides)
            current_sides.setdefault(key, set()).add(side)
        for key, (offer, sides) in changed.items():
            timestamps = [part.source_observed_at for part in sides.values()]
            same_observation = len(set(timestamps)) == 1 and all(stamp is not None for stamp in timestamps)
            current_pair = len(sides) >= 2 and set(sides) == current_sides[key]
            verified = all(part.observation_verified for part in sides.values())
            acceptable = all(part.verification_reason == "quote_bound_collector_observation" for part in sides.values())
            adverse_reason = next((part.verification_reason for part in sides.values()
                                   if part.verification_reason != "quote_bound_collector_observation"), None)
            # The documented complete game board is a replacement baseline;
            # later odds_update frames contain only changed rows. Preserve the
            # oldest side's receipt for a research-only cross-frame pair. It
            # is not a current observation or an active-status attestation.
            research_game_pair = (offer.provider == "parlay_game_ws" and offer.market_key == "h2h"
                                  and len(sides) == 2 and not current_pair)
            complete.append(replace(
                offer,
                prices={side: part.prices[side] for side, part in sides.items()},
                outcome_points={side: part.outcome_points[side] for side, part in sides.items()},
                observed_at=_oldest([part.observed_at for part in sides.values()]),
                source_observed_at=_oldest(timestamps),
                source_price_changed_at=_oldest([part.source_price_changed_at for part in sides.values()]),
                provider_reported_price_age_s=max(
                    (part.provider_reported_price_age_s for part in sides.values()
                     if part.provider_reported_price_age_s is not None), default=None),
                source_stored_at=_oldest([part.source_stored_at for part in sides.values()]),
                source_status_historical_hint=all(part.source_status_historical_hint for part in sides.values()),
                source_selection_ids={name: sid for part in sides.values()
                    for name, sid in part.source_selection_ids.items()},
                received_at=_oldest([part.received_at for part in sides.values()]),
                observation_verified=verified,
                observation_complete=verified and current_pair and same_observation and acceptable,
                market_status_verified=all(part.market_status_verified for part in sides.values()),
                verification_reason=("trusted_game_cross_frame_pair" if research_game_pair else
                                     "incomplete_or_cross_frame_outcomes" if not current_pair else
                                     "unsynchronized_outcomes" if verified and not same_observation else
                                     adverse_reason or offer.verification_reason),
                status="active" if all(part.status == "active" for part in sides.values()) else "suspended",
            ))
        return complete

    def _clear_partial(self, provider: str, selectors: dict | None = None) -> None:
        for key in list(self._partial):
            offer = next(iter(self._partial[key].values()))
            if offer.provider == provider and (not selectors or _matches_scope(offer, selectors)):
                del self._partial[key]

    def _extract_isolated(self, envelope: ProviderEnvelope):
        """Flat Parlay rows fail independently; other shapes remain atomic.

        Reject the entire affected event/book/market scope in this delivery,
        including otherwise valid rows. A good opposing row cannot resurrect a
        price whose companion was malformed in the same batch.
        """
        flat = (envelope.provider_id == "parlay_ws" and envelope.kind in {"initial_state", "odds_update"}
                or envelope.provider_id == "parlay_rest" and envelope.kind == "props_snapshot")
        if not flat:
            return extract_offers(envelope), []
        rows = envelope.payload.get("data") if isinstance(envelope.payload, dict) else envelope.payload
        if not isinstance(rows, list):
            raise ValueError("Expected a Parlay row array")
        offers, failures = [], []
        for row in rows:
            try:
                if not isinstance(row, dict):
                    raise ValueError("Expected a Parlay row")
                adverse = _parlay_unavailability_reason(row)
                if adverse:
                    scope = _parlay_failure_scope(row)
                    self._quarantine(None, adverse, envelope.provider_id, failed_scope=scope)
                    if scope not in failures:
                        failures.append(scope)
                    continue
                payload = {**envelope.payload, "data": [row]} if isinstance(envelope.payload, dict) else [row]
                offers.extend(extract_offers(replace(envelope, payload=payload)))
            except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
                scope = _parlay_failure_scope(row)
                self._quarantine(None, "Invalid provider payload or price",
                                 envelope.provider_id, failed_scope=scope)
                if scope not in failures:
                    failures.append(scope)
        return [offer for offer in offers if not any(_matches_scope(offer, scope) for scope in failures)], failures

    def __call__(self, envelope: ProviderEnvelope) -> list[tuple[Market, Quote]]:
        self.research_updates = []
        if envelope.kind == "feed_invalidated":
            self._clear_partial(envelope.provider_id)
            if self.research_projector is not None:
                self.research_updates = self.research_projector.apply(envelope, [])
            return self._invalidate(envelope)
        result = []
        if envelope.kind == "initial_state" or envelope.snapshot_complete:
            self._clear_partial(envelope.provider_id)
            # Even an incomplete reconnect baseline cannot retain old live quotes.
            result.extend(self._invalidate(envelope))
        try:
            extracted, failed_scopes = self._extract_isolated(envelope)
            for scope in failed_scopes:
                self._clear_partial(envelope.provider_id, scope)
                result.extend(self._invalidate(envelope, scope))
                if self.research_projector is not None:
                    self.research_updates.extend(self.research_projector.invalidate_scope(
                        envelope.provider_id, scope, envelope.metadata.get("sport")))
            offers = self._combine_partial_outcomes(extracted)
            if self.research_projector is not None:
                self.research_updates.extend(self.research_projector.apply(envelope, offers))
        except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
            self._quarantine(None, "Invalid provider payload or price", envelope.provider_id)
            self._clear_partial(envelope.provider_id)
            if self.research_projector is not None:
                self.research_updates = self.research_projector.invalidate(envelope.provider_id)
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
                market_fingerprint=mapping.market.identity(),
                decimal_odds={mapping.outcome_map[key]: price for key, price in offer.prices.items()},
                source_time=offer.observed_at, provider_time=offer.provider_time,
                source_observed_at=offer.source_observed_at,
                observation_verified=offer.observation_verified,
                observation_complete=offer.observation_complete,
                source_price_changed_at=offer.source_price_changed_at,
                provider_reported_price_age_s=offer.provider_reported_price_age_s,
                source_stored_at=offer.source_stored_at,
                source_selection_ids=offer.source_selection_ids,
                source_status_historical_hint=offer.source_status_historical_hint,
                verification_reason=offer.verification_reason,
                source_scope_poll_at=offer.source_scope_poll_at,
                source_scope_is_current=offer.source_scope_is_current,
                market_status_verified=offer.market_status_verified,
                settlement_equivalence_verified=mapping.settlement_equivalence_verified,
                normal_outcome_verified=mapping.normal_outcome_verified,
                normal_outcome_evidence=mapping.normal_outcome_evidence,
                received_at=envelope.received_at, synthetic=envelope.synthetic,
                status=offer.status, original_market_id=offer.market_key,
            )
            cache_key = (envelope.provider_id, offer.signature)
            previous = self._last.get(cache_key)
            previous_clock = (previous[2].source_observed_at if previous[2].observation_verified else previous[2].source_time) if previous else None
            current_clock = quote.source_observed_at if quote.observation_verified else quote.source_time
            degraded = previous and any(getattr(previous[2], name) and not getattr(quote, name) for name in (
                "observation_verified", "observation_complete", "market_status_verified"))
            watermark = self._verified_watermarks.get(cache_key)
            if degraded and watermark is not None:
                self._verification_latches.add(cache_key)
            if cache_key in self._verification_latches and not degraded:
                complete_evidence = (quote.observation_verified and quote.observation_complete
                                     and quote.market_status_verified and quote.source_observed_at is not None)
                if complete_evidence and (watermark is None or quote.source_observed_at > watermark):
                    self._verification_latches.discard(cache_key)
                elif quote.observation_complete:
                    # A delayed duplicate cannot resurrect eligibility after
                    # evidence was lost. Keep its research clocks/prices, while
                    # preserving the pre-downgrade observation watermark.
                    quote = quote.model_copy(update={"observation_complete": False,
                        "verification_reason": "new_complete_verified_observation_required"})
            if previous_clock and current_clock and current_clock < previous_clock and quote.status == "active" and not degraded:
                # An out-of-order observation cannot overwrite a newer price.
                continue
            if (cache_key not in self._verification_latches and quote.observation_verified
                    and quote.source_observed_at is not None):
                if watermark is None or quote.source_observed_at > watermark:
                    self._verified_watermarks[cache_key] = quote.source_observed_at
            self.mapped_count += 1
            self.quarantine.pop(offer.signature, None)
            self._last[cache_key] = (
                offer, mapping.market, quote, envelope.metadata.get("sport"),
            )
            result.append((mapping.market, quote))
        return result
