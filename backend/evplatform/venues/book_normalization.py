"""Reviewed native book mappings with independent quote-level source clocks.

Consumes frames *after* the venue client has validated/applied them. Never
fetches metadata or a REST book on the pricing path. Stream invalidations must
also be delivered here. No source clock is inferred from local receipt time.

Official protocol references: Kalshi /websockets/orderbook-updates; Novig
/api/streaming/connection and /api/streaming/book; ProphetX /docs/websocket-events.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..core.models import Market, utcnow
from ..core.native_accounting import NativeContract, NativeInstrument
from ..core.native_pricing import NativeBook, NativeFeeBound, NativeLevel, instrument_fingerprint
from ..core.readiness import digest_json
from .common import VenueError, decimal_value
from .polymarket_us import alias
from .prophetx import executable_level


class NativeMarketMapping(NativeContract):
    market: Market
    instrument: NativeInstrument
    fee_bound: NativeFeeBound
    opposing_outcome: str | None = None
    source_event_id: str | None = None
    source_market_id: str | None = None
    prop_sub_type: str | None = None
    # ProphetX's public reference names updated_at but does not specify its unit.
    # Do not use magnitude guessing; retain unknown until verified independently.
    source_timestamp_unit: Literal["unknown", "seconds", "milliseconds", "microseconds", "nanoseconds"] = "unknown"
    timestamp_unit_evidence: str = ""

    @model_validator(mode="after")
    def exact_contract(self):
        i = self.instrument
        if (not i.verified or i.canonical_market_id != self.market.id or i.outcome not in self.market.outcomes
                or i.settlement_rules_fingerprint != self.market.identity()):
            raise ValueError("Native instrument must bind the reviewed full Market.identity()")
        if self.market.settlement.currency != "USD" or self.market.settlement.contract_type != "binary":
            raise ValueError("Unsupported native settlement type")
        if len(self.market.outcomes) != 2:
            raise ValueError("Opposing liquidity requires an exactly binary canonical market")
        if i.venue == "novig" and (not self.opposing_outcome or self.opposing_outcome == i.native_outcome):
            raise ValueError("Novig requires a distinct, reviewed opposing outcome ID")
        if i.venue in {"kalshi", "polymarket_us"} and i.native_outcome not in {"yes", "no"}:
            raise ValueError("Native outcome must be yes or no")
        if i.venue == "prophetx" and (not self.source_event_id or not self.source_market_id):
            raise ValueError("ProphetX mappings require native event and market scope")
        if self.source_timestamp_unit != "unknown" and not self.timestamp_unit_evidence.strip():
            raise ValueError("Numeric ProphetX clock units require verification evidence")
        if not self.fee_bound.verified or self.fee_bound.instrument_fingerprint != instrument_fingerprint(i):
            raise ValueError("Fee bound must be verified for this exact instrument")
        return self


class NativeMappingFile(NativeContract):
    schema_version: Literal[1] = 1
    mappings: tuple[NativeMarketMapping, ...] = ()


def _clock(value, unit: str) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result.astimezone(timezone.utc) if result.tzinfo else None
        except ValueError:
            return None
    if unit == "unknown":
        return None
    divisors = {"seconds": 1, "milliseconds": 1000, "microseconds": 1_000_000, "nanoseconds": 1_000_000_000}
    try:
        return datetime.fromtimestamp(float(decimal_value(value) / divisors[unit]), timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError, KeyError):
        return None


def _oldest(times) -> datetime | None:
    values = list(times)
    return min(values) if values and all(value is not None for value in values) else None


class NativeBookMapper:
    def __init__(self, mappings: list[NativeMarketMapping] | tuple[NativeMarketMapping, ...] = ()):
        self.mappings = tuple(NativeMarketMapping.model_validate(item) for item in mappings)
        self.mapping_digest = digest_json(NativeMappingFile(mappings=self.mappings).model_dump(mode="json"))
        self._index: dict[tuple[str, str], list[NativeMarketMapping]] = {}
        self._clocks: dict[tuple[str, str], dict] = {}
        self._sequences: dict[tuple[str, str, str], int] = {}
        self._last: dict[tuple[str, str, str], tuple[Market, NativeBook]] = {}
        self._revisions: dict[tuple[str, str, str], int] = {}
        self.errors = 0
        self.unmapped_updates = 0
        canonical, native = {}, set()
        for item in self.mappings:
            i = item.instrument
            key = (i.venue, i.canonical_market_id, i.outcome)
            native_key = (i.venue, i.instrument_id, i.native_outcome)
            if key in canonical or native_key in native:
                raise ValueError("Duplicate canonical/native mapping outcome")
            for prior in canonical.values():
                if prior.market.id == item.market.id and prior.market.identity() != item.market.identity():
                    raise ValueError("Canonical market ID has conflicting full identities")
            canonical[key] = item
            native.add(native_key)
            source_id = item.source_market_id if i.venue == "prophetx" else i.instrument_id
            self._index.setdefault((i.venue, source_id), []).append(item)

    @classmethod
    def from_file(cls, path: str | Path) -> "NativeBookMapper":
        path = Path(path)
        if not path.exists():
            return cls()
        return cls(NativeMappingFile.model_validate_json(path.read_text(encoding="utf-8")).mappings)

    def invalidate(self, venue: str) -> list[tuple[Market, NativeBook]]:
        result = []
        for key, (market, book) in list(self._last.items()):
            if key[0] == venue:
                self._revisions[key] = book.revision + 1
                invalid = book.model_copy(update={"synchronized": False, "status": "suspended", "asks": (),
                                                  "revision": self._revisions[key]})
                result.append((market, invalid))
                del self._last[key]
        self._clocks = {key: values for key, values in self._clocks.items() if key[0] != venue}
        self._sequences = {key: value for key, value in self._sequences.items() if key[0] != venue}
        return result

    def _book(self, mapping, levels, times, revision, received, publication=None, *, synchronized=True, status="active"):
        i = mapping.instrument
        key = (i.venue, i.canonical_market_id, i.outcome)
        revision = max(revision, self._revisions.get(key, -1) + 1)
        self._revisions[key] = revision
        # Best taker price first in the venue's own terms.
        levels = sorted(levels, key=lambda item: Fraction(1) / i.gross_ratio(item[0])
                        if i.price_format == "american" else item[0])
        book = NativeBook(instrument=i, market_fingerprint=mapping.market.identity(),
                          asks=tuple(NativeLevel(price=p, quantity=q) for p, q in levels), fee_bound=mapping.fee_bound,
                          source_time=_oldest(times), provider_time=publication, received_at=received,
                          revision=revision, synchronized=synchronized, status=status, synthetic=False)
        self._last[key] = (mapping.market, book)
        return mapping.market, book

    def process(self, venue: str, client, frame: dict, *, received_at: datetime | None = None) -> list[tuple[Market, NativeBook]]:
        received = received_at or utcnow()
        if received.tzinfo is None:
            raise ValueError("Native receipt time needs a timezone")
        if frame.get("type") in {"transport_invalidated", "snapshot_required"}:
            return self.invalidate(venue)
        try:
            if venue == "kalshi":
                return self._kalshi(client, frame, received)
            if venue == "novig":
                return self._novig(client, frame, received)
            if venue == "prophetx":
                return self._prophetx(frame, received)
            if venue == "polymarket_us":
                return self._polymarket_us(client, frame, received)
            return []
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError, VenueError):
            # A malformed update must invalidate already-published usable books.
            self.errors += 1
            return self.invalidate(venue)

    def _polymarket_us(self, client, frame, received):
        # A full MARKET_DATA frame is a replacement of the advertised top
        # levels. Lite/trade/heartbeat delivery is never executable depth.
        if (frame.get("type") != "polymarket_us_monitor" or frame.get("channel") != "markets"
                or frame.get("kind") != "data"):
            return []
        data = alias(frame["payload"], "marketData", "market_data")
        if data is None:
            return []
        slug = alias(data, "marketSlug", "market_slug")
        mappings = self._index.get(("polymarket_us", slug), [])
        if not mappings:
            self.unmapped_updates += 1
            return []
        if (not client._ready.get("markets") or slug not in client.market_slugs
                or client.books.get(slug) != data):
            return self.invalidate("polymarket_us")
        source = _clock(alias(data, "transactTime", "transact_time"), "unknown")
        key = ("polymarket_us", slug)
        prior = self._clocks.get(key, {}).get("snapshot")
        if source is None or source > received or (prior is not None and source < prior):
            return self.invalidate("polymarket_us")
        self._clocks[key] = {"snapshot": source}
        state = data.get("state")
        if state == "MARKET_STATE_OPEN":
            status = "active"
        elif state in {"MARKET_STATE_PREOPEN", "MARKET_STATE_SUSPENDED", "MARKET_STATE_HALTED",
                       "MARKET_STATE_MATCH_AND_CLOSE_AUCTION"}:
            status = "suspended"
        elif state in {"MARKET_STATE_EXPIRED", "MARKET_STATE_TERMINATED"}:
            status = "closed"
        else:
            raise ValueError("Unknown Polymarket US market state")
        sides = {}
        for side in ("bids", "offers"):
            rows = data[side]
            if not isinstance(rows, list):
                raise ValueError("Invalid Polymarket US depth")
            prices, levels = set(), []
            for row in rows:
                if not isinstance(row, dict) or row.get("px", {}).get("currency") != "USD":
                    raise ValueError("Invalid Polymarket US book currency")
                p, q = row["px"]["value"], row["qty"]
                if isinstance(p, (bool, float)) or isinstance(q, (bool, float)):
                    raise ValueError("Inexact Polymarket US book values")
                p, q = decimal_value(p), decimal_value(q)
                if not p.is_finite() or not q.is_finite() or not 0 < p < 1 or q <= 0 or p in prices:
                    raise ValueError("Invalid or duplicated Polymarket US depth level")
                prices.add(p)
                levels.append((p, q))
            sides[side] = levels
        if sides["bids"] and sides["offers"] and max(p for p, _ in sides["bids"]) >= min(p for p, _ in sides["offers"]):
            raise ValueError("Crossed Polymarket US advertised book")
        result = []
        for mapping in mappings:
            i = mapping.instrument
            if (i.quantity_unit != "contracts" or i.price_format != "probability"
                    or i.payout_per_unit_usd != 1 or i.instrument_id.startswith("caoc-")):
                raise ValueError("Unsupported Polymarket US executable instrument")
            levels = sides["offers"] if i.native_outcome == "yes" else [(Decimal(1)-p, q) for p, q in sides["bids"]]
            # No exchange sequence exists in this retail schema; _book supplies
            # only a monotonic LOCAL revision, never a sequence-gap claim.
            result.append(self._book(mapping, levels if status == "active" else [], [source], 0,
                                     received, status=status))
        return result

    def _kalshi(self, client, frame, received):
        kind = frame.get("type")
        if kind not in {"orderbook_snapshot", "orderbook_delta"}:
            return []
        msg = frame["msg"]
        ticker = msg["market_ticker"]
        mappings = self._index.get(("kalshi", ticker), [])
        if not mappings:
            self.unmapped_updates += 1
            return []
        book = client.book
        if ticker not in book.books or ticker in book.invalid:
            return self.invalidate("kalshi")
        sid, seq = int(frame["sid"]), int(frame["seq"])
        if book.market_sids.get(ticker) != sid or str(sid) in book.sequences.invalid:
            return self.invalidate("kalshi")
        seq_key = ("kalshi", ticker, str(sid))
        if seq <= self._sequences.get(seq_key, -1):
            return []
        if book.sequences.values.get(str(sid)) != seq:
            return self.invalidate("kalshi")
        self._sequences[seq_key] = seq
        publication = _clock(frame.get("sending_ts_ms"), "milliseconds")
        key = ("kalshi", ticker)
        if kind == "orderbook_snapshot":
            # A full native snapshot observes the server book at publication;
            # this is not a claim about the original maker's price-change time.
            clocks = {(side, p): publication for side, levels in book.books[ticker].items() for p in levels}
            self._clocks[key] = clocks
        else:
            clocks = self._clocks.get(key)
            if clocks is None:
                return self.invalidate("kalshi")
            side, price = msg["side"], decimal_value(msg["price_dollars"])
            source = _clock(msg.get("ts_ms"), "milliseconds") or _clock(msg.get("ts"), "unknown")
            clock_key = (side, price)
            prior = clocks.get(clock_key)
            if prior and source and source < prior:
                return self.invalidate("kalshi")
            if price in book.books[ticker][side]:
                clocks[clock_key] = source
            else:
                clocks.pop(clock_key, None)
        result = []
        for mapping in mappings:
            opposite = "no" if mapping.instrument.native_outcome == "yes" else "yes"
            levels = book.asks(ticker, mapping.instrument.native_outcome)
            times = [clocks.get((opposite, Decimal(1)-price)) for price, _ in levels]
            result.append(self._book(mapping, levels, times, seq, received, publication))
        return result

    def _novig(self, client, frame, received):
        snapshot = "snapshot" in frame
        changed = frame.get("snapshot" if snapshot else "delta", {})
        publication = _clock(frame.get("ts"), "milliseconds")
        result = []
        for market_id, channels in changed.items():
            mappings = self._index.get(("novig", market_id), [])
            if not mappings:
                self.unmapped_updates += 1
                continue
            book = client.book
            if f"{market_id}:book" in book.sequences.invalid or f"{market_id}:lifecycle" in book.sequences.invalid:
                return self.invalidate("novig")
            key = ("novig", market_id)
            rows = book.books.get(market_id, {})
            clocks = self._clocks.get(key)
            book_change = channels.get("book")
            lifecycle = channels.get("lifecycle")
            seen_new = False
            for name, channel in (("book", book_change), ("lifecycle", lifecycle)):
                if channel is not None:
                    seq_key = ("novig", market_id, name)
                    seq = int(channel["seq"])
                    if seq > self._sequences.get(seq_key, -1):
                        if book.sequences.values.get(f"{market_id}:{name}") != seq:
                            return self.invalidate("novig")
                        self._sequences[seq_key] = seq
                        seen_new = True
                    elif name == "book":
                        book_change = None
            if not seen_new:
                continue
            if snapshot and book_change is not None:
                clocks = {order_id: publication for order_id in rows}
                self._clocks[key] = clocks
            elif book_change is not None:
                if clocks is None:
                    # Newly opened event subscription markets start empty.
                    if not lifecycle or lifecycle.get("deltas") != ["OPEN"]:
                        return self.invalidate("novig")
                    clocks = self._clocks[key] = {}
                for delta in book_change["deltas"]:
                    order_id = book._identifier(delta, "order", "orderId")
                    if delta["kind"] == "remove":
                        clocks.pop(order_id, None)
                    elif delta["kind"] == "add":
                        clocks[order_id] = publication
            clocks = clocks or {}
            self._clocks[key] = {order_id: time for order_id, time in clocks.items() if order_id in rows}
            status = "active" if book.statuses.get(market_id) == "OPEN" else "closed"
            for mapping in mappings:
                levels = [(p, Decimal(q)) for p, q in book.asks(market_id, mapping.opposing_outcome)]
                times = [clocks.get(order_id) for order_id, row in rows.items() if row["outcome"] == mapping.opposing_outcome]
                seq = self._sequences.get(("novig", market_id, "book"), 0)
                result.append(self._book(mapping, levels, times, seq, received, publication,
                                         synchronized=key in self._clocks, status=status))
        return result

    def _prophetx(self, frame, received):
        if frame.get("event") in {"market", "v2.market", "market_strike", "v2.market_strike", "sport_event", "v2.sport_event"}:
            # Rule/status changes require fresh reviewed scope and book data.
            # Do not guess which native status strings mean active.
            return self.invalidate("prophetx")
        if frame.get("event") != "v2.market_selections":
            return []
        data, scope = frame["data"], frame.get("scope")
        native_market = str(data["market_id"])
        mappings = self._index.get(("prophetx", native_market), [])
        if not mappings:
            self.unmapped_updates += 1
            return []
        if not isinstance(scope, dict) or str(scope.get("event_id")) != str(data["sport_event_id"]):
            return self.invalidate("prophetx")
        info = data["info"]
        sequence = int(info["sequence_number"])
        subject = f"{data['sport_event_id']}:{native_market}:{info['id']}"
        key = ("prophetx", subject, "selections")
        if sequence <= self._sequences.get(key, -1):
            return []
        self._sequences[key] = sequence
        groups = info["selections"]
        if not isinstance(groups, list) or any(not isinstance(group, list) for group in groups):
            raise ValueError("Unknown ProphetX selection shape")
        result = []
        for mapping in mappings:
            if (mapping.source_event_id != str(data["sport_event_id"])
                    or (mapping.prop_sub_type is not None and str(scope.get("sub_type")) != mapping.prop_sub_type)
                    or (mapping.prop_sub_type is None and scope.get("sub_type") is not None)):
                continue
            selections = [row for group in groups for row in group
                          if str(row.get("strike_id")) == mapping.instrument.instrument_id
                          and str(row.get("outcome_id")) == mapping.instrument.native_outcome]
            if not selections:
                # This event can update another strike within the same market.
                # Do not clear or refresh an unrelated strike's snapshot.
                continue
            levels, times, seen_prices = [], [], set()
            for selection in selections:
                parsed = executable_level(selection)
                p, q = parsed["american_price"], parsed["executable_stake_usd"]
                if p in seen_prices:
                    raise ValueError("Duplicate ProphetX price level")
                seen_prices.add(p)
                if q:
                    levels.append((p, q))
                    times.append(_clock(parsed["source_observed_at"], mapping.source_timestamp_unit))
            # The official payload contains all displayed selections for a strike;
            # replacements may reduce displayed depth and never borrow old levels.
            if frame.get("envelope", {}).get("op") == "d":
                levels, times = [], []
            result.append(self._book(mapping, levels, times, sequence, received,
                                     status="active" if levels else "suspended"))
        return result
