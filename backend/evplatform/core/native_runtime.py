"""Native execution coordinator and live facts for the release gate.

All saved artifacts describe intent/evidence. Connection, account identity,
inventory reconciliation and freshness facts come from the running adapters.
No credential presence or saved certificate automatically arms trading.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from pydantic import Field

from .conflicts import UNRESOLVED, reviewed_correlations, risk_family
from .models import Contract, Envelope, OrderStatus, utcnow
from .native_oms import NativeOrder, NativeOrderManager
from .pricing import NoSignal, fair_value, market_config
from .readiness import (
    FeedReadiness, LaunchProfile, LiveLimits, MarketReadiness,
    ReadinessBlocker, ReadinessReport, RuntimeReadiness, digest_json, evaluate_readiness,
)
from .portfolio import LaunchPortfolio
from .risk import RiskRejected


def build_fingerprint():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def pilot_prior_attempts(orders) -> int:
    """Do not let the first unsent durable intent fail its own preflight."""
    return sum(not (order.status in {OrderStatus.INTENT, OrderStatus.SUBMITTING}
                    and not order.venue_order_id and order.filled_quantity == 0)
               for order in orders)


def current_geographic_attestation(snapshot, account_fingerprint: str,
                                   environment: str, now: datetime) -> bool:
    """Require an account-bound, unexpired native location result at dispatch.

    An authenticated wallet, a production host, or a prior launch certificate
    does not establish the account's present trading location. Venues without
    a read-only attestation remain unable to submit automatically.
    """
    expiry = getattr(snapshot, "geographic_attestation_expires_at", None)
    return bool(snapshot is not None and getattr(snapshot, "identity_verified", False)
                and getattr(snapshot, "account_fingerprint", None) == account_fingerprint
                and getattr(snapshot, "environment", None) == environment
                and isinstance(expiry, datetime) and expiry.tzinfo is not None
                and expiry.utcoffset() is not None and now < expiry)


class NativeRuntime:
    def __init__(self, engine):
        self.engine = engine
        self.profile = None
        self.portfolio = None
        self.profile_error = ""
        self.structural_review = None
        self.build_id = ""
        self.mapper = None
        self.books = {}
        self.candidates = {}
        self.signals = {}
        self.account_snapshots = {}
        self.external_positions = ()
        self.pending_external_orders = ()
        # Pre-profile diagnostics are never consulted by risk, sizing or the
        # release gate. They let an operator identify an account before writing
        # the reviewed launch profile that enables normal account probing.
        self.diagnostic_snapshots = {}
        self._diagnostic_bindings = {}
        self._diagnostic_probe_lock = asyncio.Lock()
        self.dirty_venues = set()
        self.last_account_probe = 0.0
        self._last_account_probes = {}
        self._account_exchange_indexes = {}
        self._kalshi_index_cache = None
        self.last_settlement_probe = 0.0
        self.execution_lease = None
        self._account_generations = {}
        self._account_snapshot_fences = {}
        self._account_probes = set()
        # A real ProphetX location check is bound to one order attempt and is
        # never inferred from merely running in the production environment.
        self._prophetx_location = None
        self._account_probe_lock = asyncio.Lock()
        self._settlement_probe_lock = asyncio.Lock()
        self.manager = NativeOrderManager(engine.risk, engine.storage, engine.bus, engine.metrics,
            lock=engine.oms.lock, revalidate=self.revalidate_order, preflight=self.require_preflight)

    async def start(self):
        self.build_id = await asyncio.to_thread(build_fingerprint)
        from .paths import runtime_data_file
        from .external_inventory import load_external_inventory_review
        from .structural_review import StructuralPilotReview

        external_inventory_error = ""
        if self.engine.settings.mode == "production":
            external_path = runtime_data_file("external_native_positions.json", self.engine.settings.data_dir)
            try:
                review = await asyncio.to_thread(load_external_inventory_review, external_path)
                self.external_positions = review.positions
                self.pending_external_orders = review.pending_orders
                self.engine.risk.external_positions = self.external_positions
                self.engine.risk.pending_external_orders = self.pending_external_orders
            except (OSError, ValueError, KeyError, TypeError):
                external_inventory_error = "External native inventory review is invalid; native execution is blocked"
        review_path = runtime_data_file("nba_pilot_structural_review.json", self.engine.settings.data_dir)
        if review_path.exists():
            try:
                self.structural_review = await asyncio.to_thread(
                    lambda: StructuralPilotReview.model_validate_json(review_path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                self.profile_error = "Structural pilot review is invalid; no pilot execution is permitted"
        profile_path = runtime_data_file("launch_profile.json", self.engine.settings.data_dir)
        portfolio_path = runtime_data_file("launch_portfolio.json", self.engine.settings.data_dir)
        if profile_path.exists() and portfolio_path.exists():
            self.profile_error = "Choose a portfolio or a legacy profile; conflicting launch files are blocked"
        elif portfolio_path.exists():
            try:
                self.portfolio = await asyncio.to_thread(lambda: LaunchPortfolio.model_validate_json(portfolio_path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                self.profile_error = "Launch portfolio is invalid; review its accounts and shared limits"
        elif profile_path.exists():
            try:
                self.profile = await asyncio.to_thread(lambda: LaunchProfile.model_validate_json(profile_path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                self.profile_error = "Launch profile is invalid; validate it before native execution"
        profiles = self.profiles_by_venue()
        primary = next(iter(profiles.values()), None)
        if primary and primary.environment == self.engine.settings.mode:
            from .execution_lease import ExecutionLease
            self.execution_lease = ExecutionLease("portfolio" if self.portfolio else primary.venue, primary.environment,
                                                  ledger_path=Path(self.engine.settings.database_path))
            try:
                acquired = await asyncio.to_thread(self.execution_lease.acquire)
            except OSError:
                acquired = False
            if not acquired:
                self.profile_error = "Native execution is owned by another local workspace/ledger, or its lock is unavailable"
            # Native money limits have no implicit defaults: only an explicitly
            # written profile selects them. This does not grant write access.
            values = primary.limits.model_dump()
            limits = {key if key == "unit_size_usd" else key.removesuffix("_usd"): value for key,value in values.items()}
            limits["live_max_order_stake"] = limits["max_order_stake"]
            limits["live_stake_authorized"] = True
            self.engine.risk.config = type(self.engine.risk.config).model_validate({
                **self.engine.risk.config.model_dump(), **limits})
            self.engine.settings.risk = self.engine.risk.config
        from ..venues.book_normalization import NativeBookMapper
        try:
            self.mapper = await asyncio.to_thread(NativeBookMapper.from_file,
                runtime_data_file("native_mappings.json", self.engine.settings.data_dir))
        except (ValueError, OSError):
            self.profile_error = "Native mappings are invalid; review the exact market and fee evidence"
        if external_inventory_error:
            self.profile_error = (self.profile_error + "; " if self.profile_error else "") + external_inventory_error
        await self.manager.start()

    def close(self):
        if self.execution_lease is not None:
            self.execution_lease.release()

    def owns_execution_lease(self):
        lease = getattr(self, "execution_lease", None)
        primary = next(iter(self.profiles_by_venue().values()), None)
        return bool(primary and lease and lease.held
                    and lease.scope == ("portfolio" if getattr(self, "portfolio", None) else primary.venue, primary.environment))

    def profiles_by_venue(self):
        if getattr(self, "portfolio", None):
            return {p.venue: p for p in self.portfolio.profiles}
        return {self.profile.venue: self.profile} if self.profile else {}

    def profile_for(self, venue=None):
        profiles = self.profiles_by_venue()
        return profiles.get(venue) if venue is not None else next(iter(profiles.values()), None)

    def research_discovery_status(self, market, comparisons):
        """Explain the first observed research-stage stop without asserting unseen depth."""
        from decimal import Decimal
        from .native_research import expected_kalshi_mlb_event

        if comparisons:
            priced = [item for item in comparisons if item.get("net_estimated_profit_usd") is not None]
            if any(Decimal(item["net_estimated_profit_usd"]) > 0 for item in priced):
                return "provisional_positive", "Current native depth has a positive research estimate after fees and the configured uncertainty allowance; mapping and execution remain unapproved"
            if len(priced) == len(comparisons):
                return "nonpositive_net_ev", "Valid simultaneous references and native depth were compared; no direction has positive net estimated EV after fees and the configured uncertainty allowance"
            if all(item.get("fee_adjusted_estimated_profit_usd") is not None and
                   Decimal(item["fee_adjusted_estimated_profit_usd"]) <= 0 for item in comparisons):
                return "nonpositive_net_ev", "Every fee-adjusted estimate is nonpositive; any nonnegative source-uncertainty allowance can only reduce it further (allowance is not configured)"
            if any(item.get("rejection") == "No synchronized executable opposing bid depth" for item in comparisons):
                return "matched_no_depth", "An exact native discovery lead exists, but its monitored book has no synchronized executable opposing bid depth"
            if any(item.get("price") is not None for item in comparisons):
                return "economics_unavailable", "An exact native discovery lead has depth, but fees or the source-uncertainty allowance are unavailable"
            return "matched_no_depth", "An exact native discovery lead exists, but current executable depth or fee evidence is unavailable"

        kalshi_record = self.engine.registry.records.get("kalshi")
        kalshi = kalshi_record.plugin if kalshi_record and kalshi_record.enabled else None
        if kalshi is not None:
            if getattr(kalshi, "research_matches", {}).get(market.id):
                return "matched_no_depth", "An exact Kalshi discovery lead exists, but the native stream or executable depth is unavailable"
            expected = expected_kalshi_mlb_event(market)
            if expected:
                series = expected.split("-", 1)[0]
                configured = kalshi.context.config.get("discovery_series", [])
                if series not in configured:
                    return "not_discovered", f"Kalshi {series} is outside the configured discovery series; public existence does not imply monitored depth"
                if any(row.get("event_ticker") == expected for row in getattr(kalshi, "research_catalog_rows", [])):
                    return "discovered_unmatched", "The exact Kalshi event is in the catalog, but its outcome, threshold, timing or rule identity did not pass exact research matching"
                return "not_discovered", "The expected Kalshi event is absent from the current monitored catalog; no native depth was tested"
        reasons = self.research_identity_rejections(market)
        if reasons:
            return "discovered_unmatched", reasons[0]["reason"]
        return "not_discovered", "No exact native market was found in the currently monitored catalogs; public existence and executable depth are separate checks"

    def research_identity_rejections(self, market):
        """Expose observed schedule/rule conflicts without broadening matches."""
        if market is None or market.market_type != "moneyline":
            return []
        from ..integrations.polymarket_us_research import _clock, _rule_start
        from .native_research import _aware
        reasons = []
        for venue in ("novig", "prophetx", "polymarket_us"):
            record = self.engine.registry.records.get(venue)
            monitor = record.plugin if record and record.enabled else None
            if monitor is None:
                continue
            if venue == "prophetx":
                for event in monitor.research_events.values():
                    if event.get("name") != f"{market.participants[1]} at {market.participants[0]}":
                        continue
                    native_start = _clock(event.get("scheduled"))
                    if native_start is not None:
                        delta = abs((native_start-market.start_time).total_seconds())
                        if delta > 120:
                            reasons.append({"venue": venue, "reason":
                                f"ProphetX catalog start differs from sportsbook by {int(delta)} seconds; event identity needs review"})
            elif venue == "polymarket_us":
                for row in monitor.research_catalog_rows:
                    if (row.get("league") != market.league or
                            {team.get("name") for team in row.get("teams", [])} != set(market.participants)):
                        continue
                    native_start = _clock(row.get("market_start"))
                    rule_start = _rule_start(row.get("rules"))
                    if native_start is not None and rule_start is not None and native_start != rule_start:
                        reasons.append({"venue": venue, "reason":
                            "Polymarket US written-rule time conflicts with its catalog start; market remains unmatched"})
                    elif native_start is not None:
                        delta = abs((native_start-market.start_time).total_seconds())
                        if delta > 120:
                            reasons.append({"venue": venue, "reason":
                                f"Polymarket US catalog start differs from sportsbook by {int(delta)} seconds; event identity needs review"})
            else:
                for event in monitor.research_events.values():
                    label = event.get("description", "")
                    if not isinstance(label, str) or " @ " not in label:
                        continue
                    away, home = label.split(" @ ", 1)
                    if (home, away) != market.participants:
                        continue
                    native_start = _aware(event.get("startsTs"))
                    if native_start is not None:
                        delta = abs((native_start-market.start_time).total_seconds())
                        if delta > 120:
                            reasons.append({"venue": venue, "reason":
                                f"Novig catalog start differs from sportsbook by {int(delta)} seconds; event identity needs review"})
        return reasons

    def research_venue_funnel(self, market, comparisons):
        """Explain each read-only venue's first observed stop, without new API calls."""
        from .native_research import NOVIG_PROP_TYPES

        by_venue = {}
        for item in comparisons:
            by_venue.setdefault(item["venue"], []).append(item)
        conflicts = self.research_identity_rejections(market)
        result = {}
        for venue in ("kalshi", "novig", "prophetx", "polymarket_us"):
            record = self.engine.registry.records.get(venue)
            monitor = record.plugin if record and record.enabled else None
            if monitor is None:
                result[venue] = {"stage": "disconnected", "reason": "Native monitor is disabled"}
                continue
            if venue == "kalshi":
                supported = (market.league == "NBA" and market.market_type == "moneyline"
                    or market.league == "MLB" and market.market_type in {"moneyline", "total"})
            elif venue == "novig":
                supported = (market.market_type == "moneyline"
                    or market.market_type == "player_prop" and market.statistic in NOVIG_PROP_TYPES)
            elif venue == "prophetx":
                supported = market.market_type in {"moneyline", "player_prop"}
            else:
                supported = market.market_type == "moneyline"
            if not supported:
                result[venue] = {"stage": "unsupported_family",
                    "reason": f"The reviewed {venue} research matcher does not support this sport/market family"}
                continue
            catalog_at = getattr(monitor, "research_catalog_at", None)
            if catalog_at is None or getattr(monitor, "discovery_status", None) in {"read_failed", "disabled"}:
                result[venue] = {"stage": "catalog_unavailable",
                    "reason": f"Native catalog unavailable ({getattr(monitor, 'discovery_status', 'unknown')})"}
                continue
            now = utcnow()
            catalog_limit = 2 * monitor.context.config.get("discovery_interval_seconds", 300)
            if (now-catalog_at).total_seconds() > catalog_limit:
                result[venue] = {"stage": "catalog_expired",
                    "reason": "Native catalog is older than its bounded discovery interval",
                    "catalog_observed_at": catalog_at.isoformat()}
                continue
            matches = getattr(monitor, "research_matches", {}).get(market.id, ())
            if not matches:
                conflict = next((item["reason"] for item in conflicts if item["venue"] == venue), None)
                catalog_reason = self._research_catalog_identity_reason(market, venue, monitor)
                result[venue] = {"stage": "identity_rejected" if conflict or catalog_reason else "not_discovered",
                    "reason": conflict or catalog_reason or
                        "No exact event/outcome/period/threshold identity in the bounded native catalog",
                    "catalog_observed_at": catalog_at.isoformat()}
                continue
            subscribed = getattr(monitor, "_subscribed_event_ids", ()) if venue == "prophetx" else (
                getattr(monitor, "_subscribed_market_ids", ()))
            def is_subscribed(match):
                if venue != "prophetx":
                    return match.native_market_id in subscribed
                if match.native_event_id not in subscribed:
                    return False
                if market.market_type != "player_prop":
                    return True
                catalog = getattr(monitor.client.public_market_data, "catalog", {})
                native = catalog.get((match.native_event_id, match.native_market_id), {})
                return native.get("sub_type") in getattr(monitor, "_subscribed_prop_subtypes", {}).get(
                    match.native_event_id, ())
            in_scope = any(is_subscribed(match) for match in matches)
            offers = by_venue.get(venue, ())
            client = getattr(monitor, "client", None)
            stream_ok = bool(client and getattr(monitor, "task", None)
                and not monitor.task.done() and client.health_check().get("streaming"))
            public_poly = venue == "polymarket_us" and client is not None and bool(
                getattr(client, "rest_books", {}))
            if not (stream_ok or public_poly):
                stage, reason = "stream_unavailable", "Native market-data connection is unavailable"
            elif not in_scope:
                stage, reason = "not_subscribed", "Exact native market exists but is outside the current subscription slots"
            elif not offers:
                stage, reason = "book_unavailable", "Subscribed exact market has no current comparison; check book, catalog and fee clocks"
            elif not any(offer.get("price") is not None for offer in offers):
                stage, reason = "depth_or_fee_rejected", next((offer.get("rejection") for offer in offers
                    if offer.get("rejection")), "No executable depth or verified fee at the comparison size")
            else:
                stage, reason = "priced_research", "Executable native offer compared; inspect fees and uncertainty separately"
            result[venue] = {"stage": stage, "reason": reason,
                "catalog_observed_at": catalog_at.isoformat(),
                "exact_native_matches": len(matches), "subscribed": in_scope,
                "comparison_directions": len(offers)}
        return result

    @staticmethod
    def _research_catalog_identity_reason(market, venue, monitor):
        """Diagnose already-cached catalog rows; this grants no mapping."""
        if venue == "polymarket_us" and market.market_type == "moneyline":
            from ..integrations.polymarket_us_research import _clock, _rule_start
            rows = [row for row in getattr(monitor, "research_catalog_rows", ())
                if row.get("league") == market.league and row.get("market_type") ==
                    "SPORTS_MARKET_TYPE_MONEYLINE" and
                    {team.get("name") for team in (row.get("teams") or ())} == set(market.participants)]
            if not rows:
                return None
            if len(rows) != 1:
                return "Multiple native moneylines name the same teams; exact question is ambiguous"
            row = rows[0]
            event_start, native_start = _clock(row.get("event_start")), _clock(row.get("market_start"))
            if event_start is None or native_start is None:
                return "Native event or market start time is missing"
            if event_start != native_start:
                return "Native event and market start times conflict"
            if abs((native_start-market.start_time).total_seconds()) > 120:
                return "Native start differs from sportsbook start beyond the exact-match window"
            if (rule_start := _rule_start(row.get("rules"))) is not None and rule_start != native_start:
                return "Native written-rule start conflicts with the catalog"
            if row.get("status") != "MARKET_STATUS_OPEN" or not row.get("active"):
                return "Native market is not affirmatively open"
            sides = row.get("sides") or ()
            if (len(sides) != 2 or {side.get("long") for side in sides} != {True, False}
                    or not all(side.get("tradable") for side in sides)):
                return "Native LONG/SHORT side identities or tradability are incomplete"
            return "Native question, written rules, or team-side identity failed exact matching"
        if venue == "prophetx":
            events = [event for event in getattr(monitor, "research_events", {}).values()
                if event.get("name") == market.event_name]
            if not events:
                return None
            if market.market_type == "player_prop":
                return "Event is discovered, but no active two-strike prop has the exact player, statistic, threshold and explicit period"
            return "Event is discovered, but no active two-strike market has the exact normal winner identity"
        if venue == "novig" and market.market_type == "player_prop":
            from .native_research import NOVIG_PROP_TYPES
            expected = NOVIG_PROP_TYPES.get(market.statistic)
            rows = [row for row in getattr(monitor, "research_catalog_rows", ())
                if row.get("marketType") == expected and type(row.get("startsTs")) is int
                and abs(row["startsTs"]-int(market.start_time.timestamp()*1000)) <= 120000]
            if rows and not getattr(monitor, "research_events", {}):
                return "Plausible prop catalog rows exist, but authoritative native event details are unavailable"
            if rows:
                return "Plausible prop catalog rows exist, but player, statistic, threshold, period or opposing sides do not match exactly"
        return None

    def research_comparisons(self, market, fair, *, pricing_config=None):
        """Compare research fair values with existing read-only native streams.

        This path constructs dictionaries for display only. It never creates a
        NativeBook, execution mapping, candidate, intent, or account authority.
        """
        from decimal import Decimal, ROUND_FLOOR
        from .native_research import (compare_minimum_lot, compare_prophetx_stake_lot,
            kalshi_research_taker_fee, novig_taker_fee)
        from .pricing import NoSignal, market_config, validate_fair_references

        now = utcnow()
        pricing_config = pricing_config or self.engine.settings.pricing
        scoped = market_config(pricing_config, market)
        try:
            validate_fair_references(market, fair, pricing_config, now)
        except NoSignal:
            return []
        market_margins = scoped.trusted_research_market_margins
        margin = market_margins.get(market.id, scoped.trusted_research_margin_probability)
        output = []
        for venue in ("kalshi", "novig", "prophetx", "polymarket_us"):
            record = self.engine.registry.records.get(venue)
            monitor = record.plugin if record else None
            client = getattr(monitor, "client", None)
            stream_ok = bool(getattr(monitor, "task", None) and not monitor.task.done()
                             and client is not None and client.health_check().get("streaming"))
            public_poly = (venue == "polymarket_us" and client is not None
                           and bool(getattr(client, "rest_books", {})))
            if (not record or not record.enabled or client is None
                    or client.settings.environment != self.engine.settings.mode
                    or not (stream_ok or public_poly)
                    or getattr(monitor, "research_catalog_at", None) is None
                    or (now-monitor.research_catalog_at).total_seconds() > 2 * monitor.context.config.get(
                        "discovery_interval_seconds", 300)):
                continue
            matches = getattr(monitor, "research_matches", {}).get(market.id, ())
            if not matches:
                continue
            for match in matches:
                if match.outcome not in fair.probabilities:
                    continue
                if venue == "kalshi":
                    current = getattr(monitor, "research_book_current", None)
                    levels = (client.book.asks(match.native_market_id, match.side)
                        if current is None or current(match.native_market_id, now=now) else [])
                    metadata = monitor.research_fee_metadata.get(match.native_event_id)
                    series_response = metadata.get("series") if metadata else None
                    event_response = metadata.get("event") if metadata else None
                    series = series_response.get("series", {}) if isinstance(series_response, dict) else {}
                    native_event = event_response.get("event", {}) if isinstance(event_response, dict) else {}
                    series = series if isinstance(series, dict) else {}
                    native_event = native_event if isinstance(native_event, dict) else {}
                    fee_evidence = {"source": "Kalshi series/event fee metadata",
                        "observed_at": metadata["observed_at"].isoformat() if metadata else None,
                        "series_fee_type": series.get("fee_type"),
                        "series_fee_multiplier": str(series["fee_multiplier"]) if series.get("fee_multiplier") is not None else None,
                        "event_fee_type_override": native_event.get("fee_type_override"),
                        "event_fee_multiplier_override": str(native_event["fee_multiplier_override"])
                            if native_event.get("fee_multiplier_override") is not None else None}
                    if metadata and (now-metadata["observed_at"]).total_seconds() <= 360:
                        fee_for = lambda price, qty, match=match, metadata=metadata: kalshi_research_taker_fee(
                            match, metadata["series"], metadata["event"], metadata["series_changes"],
                            metadata["event_changes"], price, qty, now=now)
                    else:
                        fee_for = lambda price, qty: (_ for _ in ()).throw(ValueError("Kalshi fee metadata missing"))
                elif venue == "prophetx":
                    view = client.public_market_data.snapshot(now=now, connected=stream_ok, limit=10000)
                    rows = [row for row in view["levels"] if row["stream_current"]
                        and row["event_id"] == match.native_event_id
                        and row["market_id"] == match.native_market_id
                        and row["strike_id"] == match.native_strike_id
                        and row["outcome_id"] == match.native_outcome_id
                        and Decimal(row["executable_stake_usd"]) > 0]
                    levels = sorted(((Decimal(row["american_price"]),
                        Decimal(row["executable_stake_usd"])) for row in rows), reverse=True)
                    fee_evidence = {"source": "ProphetX published 2% fee on net winnings",
                        "url": "https://www.prophetx.co/lobby/",
                        "rate_on_winnings": "0.02", "rounding_verified": False,
                        "book_source": "scoped Talaria v2.market_selections",
                        "book_receipt_at": max((row["local_receipt_at"] for row in rows), default=None)}
                elif venue == "polymarket_us":
                    from ..integrations.polymarket_us_research import current_book, direction_levels, taker_fee
                    native = next((row for row in monitor.research_catalog_rows
                                   if row.get("slug") == match.native_market_id), None)
                    if native is None:
                        continue
                    book = current_book(native, client, now=now,
                                        stream_connected=client.health_check().get("market_streaming") is True)
                    levels = direction_levels(book, match.side)
                    fee_evidence = {"source": "Polymarket US market feeCoefficient and current fee schedule",
                        "observed_at": monitor.research_catalog_at.isoformat(),
                        "coefficient": native.get("fee_coefficient"), "book_source": book.get("source"),
                        "book_source_time": book.get("source_time"),
                        "book_rejection": book.get("rejection"),
                        "fee_rounding": "nearest cent, half even; one-fill research estimate"}
                    fee_for = lambda price, qty, native=native: taker_fee(native, price, qty)
                else:
                    current = getattr(monitor, "research_book_current", None)
                    book_current = current is None or current(match.native_market_id, now=now)
                    levels = (client.book.asks(match.native_market_id, match.opposing_outcome_id)
                        if book_current else [])
                    depth_reason = None
                    if not book_current:
                        stamp = getattr(monitor, "_book_at", {}).get(match.native_market_id)
                        depth_reason = ("Native book observation exceeded 30 seconds"
                            if stamp is not None else "No native book snapshot for this subscription")
                    elif not levels:
                        depth_reason = ("Novig market is not open or book sequence is invalid"
                            if client.book.statuses.get(match.native_market_id) != "OPEN"
                            or f"{match.native_market_id}:book" in client.book.sequences.invalid
                            else "No executable opposing bids in the current Novig book")
                    native = next((row for row in monitor.research_catalog_rows
                                   if row.get("marketId") == match.native_market_id), None)
                    event = monitor.research_events.get(match.native_event_id)
                    raw_fee = native.get("fee") if isinstance(native, dict) else None
                    native_fee = raw_fee if isinstance(raw_fee, dict) else {}
                    fee_evidence = {"source": "Novig market fee and event phase",
                        "observed_at": monitor.research_catalog_at.isoformat(),
                        "charged": native_fee.get("charged"),
                        "coefficient": str(native_fee["coefficient"])
                            if native_fee.get("coefficient") is not None else None,
                        "event_phase": event.get("status") if isinstance(event, dict) else None}
                    fee_for = lambda price, qty, native=native, event=event: novig_taker_fee(
                        native, event, price, qty)
                if venue == "prophetx":
                    result = compare_prophetx_stake_lot(match, fair.probabilities[match.outcome], levels,
                        fee_rate_on_winnings=Decimal("0.02"),
                        uncertainty_margin_probability=margin, now=now)
                    result["native_order_direction"] = "TAKE_STRIKE"
                    result["fee_estimate_only"] = True
                else:
                    result = compare_minimum_lot(match, fair.probabilities[match.outcome], levels,
                        fee_for=fee_for, uncertainty_margin_probability=margin, now=now)
                    if venue == "novig" and depth_reason is not None:
                        result["rejection"] = depth_reason
                if venue == "polymarket_us":
                    result["native_order_direction"] = "BUY_LONG" if match.side == "long" else "SELL_SHORT"
                    result["instrument_outcome"] = next((s["description"] for s in native["sides"]
                        if s["long"]), None)
                    result["short_collateral_note"] = ("Short posts $1 margin per contract; YES sale proceeds offset "
                        "buying-power use. No separate NO instrument or book exists.") if match.side == "short" else None
                    if result.get("comparison_quantity") and result.get("cash_cost_usd"):
                        quantity = Decimal(result["comparison_quantity"])
                        result["margin_locked_usd"] = str(quantity) if match.side == "short" else "0"
                        result["sale_proceeds_usd"] = str(quantity-Decimal(result["cash_cost_usd"])) if match.side == "short" else "0"
                        result["buying_power_required_usd"] = str(Decimal(result["cash_cost_usd"])
                            + Decimal(result["taker_fee_usd"]))
                    try:
                        if Decimal(native["minimum_quantity"]) > Decimal(1):
                            result["positive_after_costs"] = False
                            result["net_estimated_profit_usd"] = None
                            result["rejection"] = "Native minimum quantity exceeds the one-contract research lot"
                    except (ValueError, TypeError, ArithmeticError):
                        result["positive_after_costs"] = False
                        result["net_estimated_profit_usd"] = None
                        result["rejection"] = "Native minimum quantity is unknown"
                result["fee_evidence"] = fee_evidence
                if venue == "novig":
                    # Per-market book evidence is distinct from connection
                    # heartbeats and from repeated /api/state reads. Retain it
                    # with each research comparison for later edge-duration
                    # and price/quantity-change analysis.
                    subject = f"{match.native_market_id}:book"
                    result["native_book_sequence"] = client.book.sequences.values.get(subject)
                    book_at = getattr(monitor, "_book_at", {}).get(match.native_market_id)
                    result["native_book_update_at"] = book_at.isoformat() if book_at else None
                    result["native_market_status"] = client.book.statuses.get(match.native_market_id)
                result["fair_probability"] = str(fair.probabilities[match.outcome])
                result["reference_profile_id"] = fair.reference_profile_id
                result["reference_books"] = [ref.get("bookmaker") for ref in fair.references]
                result["catalog_age_ms"] = (now-match.catalog_observed_at).total_seconds()*1000
                result["source_quality_mode"] = scoped.reference_quality_mode
                result["reference_strategy"] = (
                    "multi_book_consensus" if fair.reference_profile_id == "multi_book_consensus"
                    else "sharp_books")
                result["independent_book_count"] = len(fair.references)
                result["uncertainty_allowance_policy"] = (
                    "exact_market_research_scenario" if market.id in market_margins else
                    "global_research_assumption" if margin is not None else "unconfigured")
                result["uncertainty_allowance_probability"] = str(margin) if margin is not None else None
                if fair.reference_profile_id == "multi_book_consensus" and result.get(
                        "fee_adjusted_estimated_profit_usd") is not None:
                    # Illustrations only. None of these authorizes execution or
                    # supplies a default live allowance.
                    after_fee = Decimal(result["fee_adjusted_estimated_profit_usd"])
                    payout = Decimal(result["normal_payout_usd"])
                    result["research_uncertainty_scenarios"] = [
                        {"allowance_probability": str(example),
                         "net_estimated_profit_usd": str(after_fee - example*payout),
                         "approved": False} for example in
                        (Decimal(0), Decimal("0.005"), Decimal("0.01"), Decimal("0.02"))]
                snapshot = self.account_snapshots.get(venue)
                cash = (snapshot.balance_usd if snapshot and snapshot.cash_available_verified
                        and (venue != "kalshi" or snapshot.exchange_index == match.exchange_index)
                        else None)
                result["spendable_cash_usd"] = str(cash) if cash is not None else None
                result["quarter_kelly_budget_usd"] = None
                result["quarter_kelly_quantity"] = None
                if (venue == "prophetx" and cash is not None and Decimal(cash) > 0
                        and result.get("positive_after_costs") and margin is not None):
                    p = Decimal(fair.probabilities[match.outcome]) - Decimal(margin)
                    win_profit = (Decimal(result["normal_payout_usd"])-Decimal(1))*Decimal("0.98")
                    fraction = max(Decimal(0), (p*win_profit-(Decimal(1)-p))/win_profit) if win_profit > 0 else Decimal(0)
                    budget = min(Decimal(1), Decimal(cash)*Decimal("0.25")*fraction,
                        Decimal(result["available_stake_usd"]))
                    result["quarter_kelly_budget_usd"] = str(budget)
                    result["quarter_kelly_stake_usd"] = str(budget)
                    result["reasons"].append("Informational USD-stake sizing; no ProphetX execution mapping or approval")
                elif (venue != "prophetx" and cash is not None and Decimal(cash) > 0 and result.get("positive_after_costs")
                        and result.get("normal_payout_usd") and result.get("price") is not None):
                    # Informational only: the production calculator rechecks
                    # split-fill fee bounds, minimums, cash, exposure and risk.
                    p = Decimal(fair.probabilities[match.outcome]) - Decimal(margin)
                    payout = Decimal(result["normal_payout_usd"])
                    debit = Decimal(result["cash_cost_usd"]) + Decimal(result["taker_fee_usd"])
                    full = max(Decimal(0), (p*payout-debit)/(payout-debit)) if payout > debit else Decimal(0)
                    budget = min(Decimal("1"), Decimal(cash)*Decimal("0.25")*full)
                    minimum = Decimal(result["comparison_quantity"])
                    quantity = (budget/debit).to_integral_value(rounding=ROUND_FLOOR)*minimum
                    result["quarter_kelly_budget_usd"] = str(budget)
                    result["quarter_kelly_quantity"] = str(quantity if quantity >= minimum else Decimal(0))
                    if quantity < minimum:
                        result["reasons"].append("Quarter-Kelly budget is below the comparison lot; no upward rounding")
                output.append(result)
        return sorted(output, key=lambda item: Decimal(item["net_estimated_roi"])
                      if item.get("net_estimated_roi") is not None else Decimal("-Infinity"), reverse=True)

    def invalidate_account(self, venue):
        """Fence every in-flight read before removing an obsolete account view."""
        self._account_generations[venue] = self._account_generations.get(venue, 0)+1
        self.account_snapshots.pop(venue, None)
        self._account_snapshot_fences.pop(venue, None)
        self._account_exchange_indexes.pop(venue, None)
        self.last_account_probe = 0.0
        self._last_account_probes.pop(venue, None)

    async def _kalshi_account_exchange_index(self, client, profile):
        """Resolve the exact pilot shard from current public native listings."""
        review = self.structural_review
        if (review is None or review.native_venue != "kalshi"
                or review.canonical_market_id not in profile.market_ids):
            raise ValueError("No reviewed Kalshi market for account index lookup")
        from ..venues.reconciliation import ReconciliationError, _object
        from urllib.parse import quote as urlquote
        cache_key = (id(client), review.native_event_id, tuple(sorted(review.native_outcomes.values())))
        cache = self._kalshi_index_cache
        if cache is not None and cache[0] == cache_key and time.monotonic()-cache[2] < 60:
            return cache[1]
        indexes = set()
        for ticker in sorted(set(review.native_outcomes.values())):
            response = await client.request("GET", f"/markets/{urlquote(ticker, safe='')}", authenticated=False)
            market = _object(_object(response, "Kalshi market listing").get("market"), "Kalshi market")
            index = market.get("exchange_index")
            if (market.get("ticker") != ticker or market.get("event_ticker") != review.native_event_id
                    or type(index) is not int or index < 0):
                raise ReconciliationError("Reviewed Kalshi ticker/event/exchange index changed")
            indexes.add(index)
        if len(indexes) != 1:
            raise ReconciliationError("Reviewed Kalshi outcomes have different exchange indexes")
        index = indexes.pop()
        self._kalshi_index_cache = (cache_key, index, time.monotonic())
        return index

    def diagnostic_account(self, venue):
        """Return a short-lived, non-authorizing monitor inventory summary."""
        record = self.engine.registry.records.get(venue)
        client = getattr(record.plugin, "client", None) if record else None
        snapshot = self.diagnostic_snapshots.get(venue)
        binding = self._diagnostic_bindings.get(venue)
        now = utcnow()
        if (not record or not record.enabled or client is None or snapshot is None
                or binding is None or binding[0] is not client
                or binding[1] != client.settings.environment
                or not getattr(record.plugin, "task", None)
                or record.plugin.task.done()
                or record.health.get("authenticated") is not True
                or snapshot.observed_at.tzinfo is None
                or not 0 <= (now - snapshot.observed_at).total_seconds() < 60):
            return None
        return {
            "venue": snapshot.venue, "environment": snapshot.environment,
            "account_fingerprint": snapshot.account_fingerprint if snapshot.identity_verified else None,
            "identity_verified": snapshot.identity_verified,
            "observed_at": snapshot.observed_at.isoformat(),
            "balance_usd": str(snapshot.balance_usd) if snapshot.cash_available_verified
                and snapshot.balance_usd is not None else None,
            "exchange_index": snapshot.exchange_index,
            "cash_available_verified": snapshot.cash_available_verified,
            "open_order_count": snapshot.open_order_count,
            "position_count": snapshot.position_count,
            "inventory_reconciled": snapshot.reconciled,
            "trading_permission_verified": snapshot.trading_permission_verified,
            "geographic_attestation_expires_at": snapshot.geographic_attestation_expires_at.isoformat()
                if snapshot.geographic_attestation_expires_at else None,
            "usable_for_execution": False,
        }

    async def probe_monitor_account(self, venue):
        """Read an authenticated monitor account without binding an executor."""
        from ..plugins.external import NativeMonitor
        from ..venues.account_probe import probe_native_account

        record = self.engine.registry.records.get(venue)
        if (not record or not isinstance(record.plugin, NativeMonitor)
                or not record.enabled or record.plugin.client is None
                or not getattr(record.plugin, "task", None)
                or record.plugin.task.done()
                or record.health.get("authenticated") is not True):
            raise ValueError("Select a connected authenticated native monitor")
        if self._diagnostic_probe_lock.locked():
            raise ValueError("An account diagnostic is already running")
        async with self._diagnostic_probe_lock:
            client = record.plugin.client
            self.diagnostic_snapshots.pop(venue, None)
            self._diagnostic_bindings.pop(venue, None)
            try:
                from ..venues.kalshi import KalshiClient
                p = self.profile_for(venue)
                exchange_index = (await self._kalshi_account_exchange_index(client, p)
                    if isinstance(client, KalshiClient) and p is not None else None)
                snapshot = await asyncio.wait_for(probe_native_account(
                    client, managed_orders=tuple(self.engine.risk.native_orders.values()),
                    settled_order_ids=frozenset(self.engine.risk.settled),
                    external_positions=tuple(row for row in self.external_positions
                        if row.venue == venue),
                    **({"exchange_index": exchange_index} if isinstance(client, KalshiClient) else {}),
                    **({"adapter": self.manager.adapters.get(venue)} if venue == "polymarket_us"
                       and getattr(self.manager.adapters.get(venue), "client", None) is client else {}),
                    **({"management_client": record.plugin.management_client}
                       if venue == "novig" and getattr(record.plugin, "management_client", None) else {})), timeout=20)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ValueError("Authenticated account inventory diagnostic failed") from None
            current = self.engine.registry.records.get(venue)
            if (current is not record or not record.enabled or record.plugin.client is not client
                    or record.health.get("authenticated") is not True
                    or snapshot.venue != venue or snapshot.environment != client.settings.environment):
                raise ValueError("Monitor account changed during diagnostic")
            self.diagnostic_snapshots[venue] = snapshot
            self._diagnostic_bindings[venue] = (client, client.settings.environment)
            return self.diagnostic_account(venue)

    def _ledger_fence(self):
        # Reporting is a slow path. Capture values, not mutable object identity,
        # including settlements made concurrently by the settlement worker.
        risk = self.engine.risk
        # The current OMS transaction creates a local intent and SUBMITTING
        # record before its final preflight. Those unsent records have no venue
        # identity/fills and must not invalidate their own prerequisite proof.
        # The shared OMS lock prevents another transmission concurrently, cash
        # reservations still include them, and ACK/UNKNOWN changes are fenced.
        return (tuple(sorted((key, order.model_dump_json()) for key, order in risk.native_orders.items()
                             if not (order.status in {OrderStatus.INTENT, OrderStatus.SUBMITTING}
                                     and not order.venue_order_id and order.filled_quantity == 0))),
                frozenset(risk.settled), frozenset(self.manager.invalidated_settlements))

    def require_preflight(self, order, *, preview_only=False):
        report = self.report()
        manual = order.execution_origin == "manual" and self.engine.risk.manual_order_active(order.id)
        dry_run = (preview_only and order.execution_origin == "manual"
                   and report.safe_to_arm and self.engine.settings.live_enabled)
        if not (dry_run or (not preview_only and (
                report.live_trading_ready or report.sandbox_trading_ready or
                (manual and report.safe_to_arm and self.engine.settings.live_enabled
                 and not self.engine.risk.kill_switch)))):
            raise RiskRejected("Native release gate has unresolved checks")
        p = self.profile_for(order.venue)
        if (p is None or order.venue != p.venue or order.mode != p.environment or
                order.account_fingerprint != p.account_fingerprint or order.market_id not in p.market_ids or
                order.strategy not in p.strategy_ids):
            raise RiskRejected("Native order is outside the authorized launch scope")
        market = getattr(self.engine, "markets", {}).get(order.market_id)
        if hasattr(self.engine, "markets") and market is None:
            raise RiskRejected("Native canonical market is unavailable")
        if market is not None:
            source_config = market_config(self.engine.settings.pricing, market)
            if source_config.reference_quality_mode == "trusted_provider":
                pilot = source_config.trusted_pilot_policy
                automatic = (order.execution_origin == "automatic"
                             and self.engine.risk.auto_trade and not self.engine.risk.kill_switch)
                one_shot_scope = (p.validation_pilot and p.limits.max_native_order_attempts == 1
                    and pilot.maximum_native_attempts == 1 and not pilot.recurring_authorization
                    and not pilot.automatic_followups)
                recurring_scope = (not p.validation_pilot and pilot.recurring_authorization
                    and pilot.automatic_followups and pilot.maximum_native_attempts is None
                    and p.limits.max_native_order_attempts is None
                    and p.limits.available_cash_only and p.limits.stake_sizing == "quarter_kelly")
                if (not (manual or automatic or (preview_only and order.execution_origin == "manual"))
                        or not (one_shot_scope or recurring_scope)
                        or not pilot.enabled or not pilot.owner_approved
                        or not pilot.permits_market(market)
                        or (pilot.market_scope is not None and pilot.market_scope.venue != order.venue)
                        or ((ceiling := pilot.cost_ceiling()) is not None
                            and order.reserved_cost > ceiling)):
                    raise RiskRejected("Trusted-provider pilot permits one scoped authorized attempt only"
                        if not pilot.recurring_authorization else
                        "Recurring trusted-provider source policy does not authorize this launch scope")
                if any(previous.id != order.id and previous.market_id == order.market_id
                       and previous.venue == order.venue and previous.mode == order.mode
                       and previous.account_fingerprint == order.account_fingerprint
                       for previous in self.engine.risk.native_orders.values()):
                    raise RiskRejected("Trusted-provider pilot attempt already exists in the durable ledger")
        if p.settlement_policy in {"novig_ioc_win_loss_halt_on_exception", "kalshi_ioc_binary_halt_on_exception",
                                   "prophetx_funded_cash_win_loss_halt_on_exception"} and any(
                previous.id != order.id and previous.venue == order.venue and previous.mode == order.mode
                and previous.account_fingerprint == order.account_fingerprint
                and previous.intent.instrument.instrument_id == order.intent.instrument.instrument_id
                for previous in self.engine.risk.native_orders.values()):
            raise RiskRejected(f"Scoped {p.venue.capitalize()} settlement permits only one managed order per native market")
        snapshot = self.account_snapshots.get(order.venue)
        if self.engine.risk.config.available_cash_only and not getattr(snapshot, "cash_available_verified", False):
            raise RiskRejected("Verified spendable account cash is required for the selected exposure policy")
        # Filled costs are already debited from a reconciled available balance.
        # Unresolved reservations are subtracted conservatively, even if the
        # venue has already frozen part of that cash. Never pool other accounts.
        reserved = sum((o.reserved_cost for o in self.engine.risk.native_orders.values()
                        if o.venue == order.venue and o.mode == order.mode
                        and o.account_fingerprint == order.account_fingerprint and o.status in UNRESOLVED), 0)
        required = reserved + (0 if order.id in self.engine.risk.native_orders else order.reserved_cost)
        if snapshot is None or snapshot.balance_usd is None or snapshot.balance_usd < required:
            raise RiskRejected("Authenticated native cash balance cannot cover reserved exposure")
        if order.venue == "kalshi" and (
                order.intent.instrument.exchange_index is None
                or snapshot.exchange_index != order.intent.instrument.exchange_index
                or self._account_exchange_indexes.get("kalshi") != order.intent.instrument.exchange_index):
            raise RiskRejected("Kalshi order and spendable cash must share the reviewed exchange shard")
        from ..venues.bridge import NativePreflight
        if not preview_only:
            adapter = self.manager.adapters[order.venue]
            location_verified = (p.environment == "production" and
                (self._prophetx_location_valid(order, adapter.client) if order.venue == "prophetx"
                 else current_geographic_attestation(snapshot, order.account_fingerprint,
                                                     order.mode, utcnow())))
            if p.environment == "production" and not location_verified:
                raise RiskRejected("Current account-bound native geographic eligibility is unavailable")
            adapter.preflight = NativePreflight(environment=p.environment, account_authorized=True,
                geographic_eligibility_verified=location_verified, native_trading_enabled=True,
                evidence=p.digest(), valid_until=min(time.time()+5, *(e.expires_at.timestamp() for e in p.evidence)))

    def _prophetx_location_valid(self, order, client):
        proof = self._prophetx_location
        return bool(proof and proof[0] == order.id and proof[1] is client
                    and time.monotonic() < proof[2]
                    and client.location_valid_for(order.intent.client_order_id))

    async def verify_prophetx_location(self, order):
        """Bind one fresh, actual owner-machine check to this exact attempt."""
        if order.venue != "prophetx" or order.mode != "production":
            return
        adapter = self.manager.adapters.get("prophetx")
        self._prophetx_location = None
        if adapter is None or not await adapter.client.check_current_location(order.intent.client_order_id):
            raise RiskRejected("ProphetX current location check failed or is unavailable")
        self._prophetx_location = (order.id, adapter.client, time.monotonic() + 10)

    def preview_check(self, order):
        """Run the placement economics and risk checks without issuing authority."""
        if not self.manager.recovered or order.venue not in self.manager.adapters:
            raise RiskRejected("Native recovery or execution adapter is unavailable")
        if not self.owns_execution_lease():
            raise RiskRejected("Native execution lease is unavailable")
        self.require_preflight(order, preview_only=True)
        self.revalidate_order(order)
        self.engine.risk.check_native_reservation(order, preview_only=True)

    def revalidate_order(self, order):
        from .native_pricing import native_revalidate_intent
        market = self.engine.markets.get(order.market_id)
        strategy = self.engine.registry.records.get(order.strategy)
        if market is None or market.identity() != order.market_fingerprint or strategy is None or not strategy.enabled:
            raise RiskRejected("Native canonical market or strategy changed")
        book = self.books.get((order.market_id, order.venue, order.outcome))
        if book is None:
            raise RiskRejected("Native executable book is missing")
        try:
            fair = fair_value(market, list(self.engine.quotes.get(market.id, {}).values()), self.engine.settings.pricing)
            if self.engine.settings.mode == "production" and not fair.live_execution_allowed:
                raise NoSignal("Reference profile has not been validated for live execution")
            native_revalidate_intent(order.intent, market, fair, book, self.engine.settings.pricing,
                                     remaining_quantity=order.quantity-order.filled_quantity,
                                     fees_already_paid_usd=order.fees)
            risk = self.engine.risk.config
            if risk.stake_sizing == "quarter_kelly":
                from .native_pricing import _settlement_bounds
                from .stake_sizing import quarter_kelly_budget
                margin = fair.trusted_pilot_margin_probability or 0
                probabilities = [fair.probabilities[order.outcome] - margin]
                if fair.sensitivity_probabilities:
                    probabilities.append(fair.sensitivity_probabilities[order.outcome] - margin)
                if min(probabilities) <= 0:
                    raise NoSignal("Pilot uncertainty allowance eliminates the modeled winning chance")
                caps = [v for v in (risk.max_order_stake, risk.live_max_order_stake) if v is not None]
                if market_config(self.engine.settings.pricing, market).reference_quality_mode == "trusted_provider":
                    pilot = market_config(self.engine.settings.pricing, market).trusted_pilot_policy
                    caps.extend(value for value in (pilot.max_debit_usd, pilot.max_loss_usd)
                                if value is not None)
                sizing = quarter_kelly_budget(book, min(probabilities),
                    _settlement_bounds(market, winner_only=fair.normal_outcome_policy == "winner_only_binary"),
                    available_cash_usd=self.sizing_cash(order.venue, exclude_order_id=order.id),
                    unit_size_usd=risk.unit_size_usd, max_stake_usd=min(caps) if caps else None,
                    price=order.intent.limit_price, now=utcnow())
                if order.reserved_cost > sizing.budget_usd or order.quantity < sizing.minimum_quantity:
                    raise NoSignal("Native order exceeds current quarter-Kelly budget or market minimum")
        except (NoSignal, ValueError) as exc:
            raise RiskRejected(str(exc)) from exc

    def sizing_cash(self, venue, *, exclude_order_id=None):
        """Only this account's reconciled, current available cash can size risk."""
        from decimal import Decimal
        profile = self.profile_for(venue)
        snapshot = self.account_snapshots.get(venue)
        now = utcnow()
        if (profile is None or snapshot is None or not snapshot.identity_verified
                or not snapshot.reconciled or not getattr(snapshot, "cash_available_verified", False)
                or snapshot.account_fingerprint != profile.account_fingerprint
                or snapshot.venue != venue or snapshot.environment != profile.environment
                or snapshot.balance_usd is None or snapshot.observed_at.tzinfo is None
                or not 0 <= (now-snapshot.observed_at).total_seconds() < 30
                or venue in getattr(self, "_account_probes", ())
                or self._account_snapshot_fences.get(venue) != self._ledger_fence()):
            raise NoSignal("Quarter Kelly requires current reconciled cash for this account")
        if venue == "kalshi" and (snapshot.exchange_index is None
                or snapshot.exchange_index != self._account_exchange_indexes.get(venue)):
            raise NoSignal("Quarter Kelly requires cash scoped to the reviewed Kalshi exchange index")
        reserved = sum((o.reserved_cost for o in self.engine.risk.native_orders.values()
            if o.id != exclude_order_id and o.venue == venue and o.mode == profile.environment
            and o.account_fingerprint == profile.account_fingerprint and o.status in UNRESOLVED), Decimal(0))
        cash = snapshot.balance_usd - reserved
        if not cash.is_finite() or cash <= 0:
            raise NoSignal("No account cash remains after pending reservations")
        return cash

    def dispatch_guard(self, intent):
        """Synchronous final placement check after transport pacing.

        This does not consume the one-use risk permit again. Cancellation
        deliberately does not use this placement guard, including while killed.
        """
        order = self.engine.risk.native_orders.get(intent.id)
        self.engine.risk.check_enabled(intent.id if order and order.execution_origin == "manual" else None)
        if not self.owns_execution_lease():
            raise RiskRejected("Native execution lease is no longer held")
        if order is None or order.intent != intent or order.status != OrderStatus.SUBMITTING:
            raise RiskRejected("Current durable submitting intent is required")
        age_ms = (utcnow()-intent.created_at).total_seconds()*1000
        if age_ms < 0 or age_ms > self.engine.risk.config.max_candidate_age_ms:
            raise RiskRejected("Queued native intent expired before transmission")
        self.require_preflight(order)
        self.revalidate_order(order)
        self.engine.risk.check_native_reservation(order, reserved=True)

    def build_order(self, candidate, *, execution_origin="automatic"):
        p = self.profile_for(candidate.venue)
        if not p:
            raise RiskRejected("Native launch profile required")
        market = self.engine.markets[candidate.market_id]
        return NativeOrder(intent=candidate.intent, candidate_id=candidate.id,
            strategy=candidate.strategy, account_fingerprint=p.account_fingerprint,
            market_snapshot=market, market_fingerprint=market.identity(), risk_family_id=risk_family(market),
            reviewed_correlation_groups=reviewed_correlations(market), sizing=candidate.sizing,
            execution_origin=execution_origin)

    async def execute(self, candidate, *, execution_origin="automatic"):
        market = self.engine.markets[candidate.market_id]
        book = self.books.get((market.id, candidate.venue, candidate.outcome))
        age = (utcnow()-candidate.calculated_at).total_seconds()*1000
        if (book is None or candidate.book_revision != book.revision or age < 0 or
                age > self.engine.risk.config.max_candidate_age_ms or not candidate.qualified):
            raise RiskRejected("Native candidate or order book changed")
        order = self.build_order(candidate, execution_origin=execution_origin)
        try:
            return await self.manager.execute(order)
        finally:
            if self._prophetx_location and self._prophetx_location[0] == order.id:
                self._prophetx_location = None
            if order.id in self.engine.risk.native_orders:
                # Even an ordinary acknowledgement changes account inventory.
                # Block new orders until the durable ledger and account agree.
                self.dirty_venues.add(order.venue)
                self.invalidate_account(order.venue)

    async def recalculate(self, market_id, event=None):
        record = self.engine.registry.records.get("native_positive_ev")
        books = [book for key,book in self.books.items() if key[0] == market_id]
        if not books:
            return
        for key in [k for k,c in self.candidates.items() if c.market_id == market_id]:
            del self.candidates[key]
        reason, candidates = "Native strategy is disabled", []
        if record and record.enabled:
            try:
                account_cash = {}
                if self.engine.risk.config.stake_sizing == "quarter_kelly":
                    for venue in {b.instrument.venue for b in books}:
                        try:
                            account_cash[venue] = self.sizing_cash(venue)
                        except NoSignal:
                            account_cash[venue] = None
                caps = [v for v in (self.engine.risk.config.live_max_order_stake,
                    self.engine.risk.config.max_order_stake) if v is not None]
                native_budget_authorized = (self.engine.risk.config.live_max_order_stake is not None
                                            or self.engine.risk.config.live_stake_authorized)
                candidates = await record.plugin.evaluate(event, {
                    "market": self.engine.markets[market_id],
                    "quotes": list(self.engine.quotes.get(market_id, {}).values()),
                    "native_books": books, "config": self.engine.settings.pricing,
                    "max_stake_usd": min(caps) if caps and native_budget_authorized else None,
                    "stake_sizing": self.engine.risk.config.stake_sizing,
                    "unit_size_usd": self.engine.risk.config.unit_size_usd,
                    "account_cash": account_cash,
                    "mode": self.engine.settings.mode,
                })
                reason = "Native fee-bounded executable offers"
            except (NoSignal, ValueError) as exc:
                reason = str(exc)
        status = "POSITIVE EV" if any(c.qualified for c in candidates) else "NEGATIVE EV" if candidates else "NO SIGNAL"
        observation = NativeSignalObservation(market_id=market_id, status=status, reason=reason,
            candidates=tuple(c.model_dump(mode="json") for c in candidates),
            market=self.engine.markets[market_id].model_dump(mode="json"),
            reference_quotes=tuple(q.model_dump(mode="json") for q in self.engine.quotes.get(market_id, {}).values()),
            pricing_policy=self.engine.settings.pricing.model_dump(mode="json"))
        self.signals[market_id] = observation
        await self.engine.bus.publish(Envelope(schema_name="native.signal", source="native_positive_ev", payload=observation))
        for candidate in candidates:
            self.candidates[(candidate.market_id, candidate.venue, candidate.outcome)] = candidate
            if (candidate.qualified and self.engine.risk.auto_trade and not self.engine.risk.kill_switch):
                await self.engine.bus.publish(Envelope(schema_name="trade.native_candidate", source=candidate.strategy, payload=candidate))

    def bind_client(self, venue, client):
        """Bind before the first private frame so its snapshot cannot be lost."""
        p = self.profile_for(venue)
        if (not p or not self.mapper or venue != p.venue or client.settings.environment != p.environment
                or not self.owns_execution_lease()):
            return None
        from ..venues.bridge import NativeExecutionVenue, VerifiedVenueMapping
        adapter = self.manager.adapters.get(p.venue)
        if adapter is None:
            mappings = [VerifiedVenueMapping(canonical_market_id=m.market.id, canonical_outcome=m.instrument.outcome,
                native_instrument_id=m.instrument.instrument_id, native_outcome=m.instrument.native_outcome,
                native_exchange_index=m.instrument.exchange_index,
                settlement_fingerprint=m.market.identity(), mapping_evidence=m.instrument.source,
                verified=True, fee_schedule_verified=m.fee_bound.verified,
                native_payout_usd=m.instrument.payout_per_unit_usd, native_quantity_unit=m.instrument.quantity_unit)
                for m in self.mapper.mappings if m.instrument.venue == p.venue]
            if not mappings: return None
            if venue == "polymarket_us":
                from ..venues.polymarket_us_execution import PolymarketUSExecutionVenue
                adapter = PolymarketUSExecutionVenue(client, self.engine.risk.authority, mappings=mappings)
            else:
                adapter = NativeExecutionVenue(client, self.engine.risk.authority, mappings=mappings)
            adapter.dispatch_guard = self.dispatch_guard
            for order in self.engine.risk.native_orders.values():
                if order.venue == p.venue and order.venue_order_id:
                    adapter.bind_native_intent(order.venue_order_id, order.intent)
                    if venue == "polymarket_us":
                        adapter.seed_fills(order.venue_order_id, order.intent, self.manager.recovered_fills(order.id))
            self.manager.register(p.venue, adapter)
        if adapter.client is not client:
            self.engine.risk.halt("Native account connection changed; restart recovery required")
            return None
        # Bound method lookup creates a new object each time. Keep the same
        # callback identity while an order waits at the transport boundary.
        if getattr(adapter, "dispatch_guard", None) != self.dispatch_guard:
            adapter.dispatch_guard = self.dispatch_guard
        return adapter

    async def on_frame(self, venue, client, frame):
        if venue == "prophetx" and hasattr(client, "public_market_data"):
            if frame.get("type") in {"transport_invalidated", "snapshot_required"}:
                client.public_market_data.invalidate(
                    catalog_uncertain=frame.get("type") == "transport_invalidated")
            else:
                client.public_market_data.apply(frame, received_at=utcnow())
        adapter = self.bind_client(venue, client)
        if adapter and hasattr(adapter, "observe_private_frame"):
            adapter.observe_private_frame(frame)
        private_execution_update = (frame.get("type") == "polymarket_us_monitor"
            and frame.get("channel") == "private" and frame.get("kind") == "data")
        if private_execution_update:
            # Invalidate before durable I/O; no order can use the earlier
            # account snapshot while exact private execution evidence saves.
            self.invalidate_account(venue)
            if self.engine.risk.native_orders:
                self.dirty_venues.add(venue)
            if adapter is not None:
                for order in tuple(self.engine.risk.native_orders.values()):
                    if order.venue == venue and order.venue_order_id:
                        fills = adapter.execution_fills(order.venue_order_id, order.intent)
                        if fills:
                            await self.manager.record_private_fills(order.id, fills, adapter=adapter)
        if frame.get("type") in {"transport_invalidated", "snapshot_required"}:
            self.invalidate_account(venue)
            if self.mapper:
                for market, book in self.mapper.invalidate(venue):
                    await self.engine.bus.publish(Envelope(schema_name="exchange.native_book", source=venue, payload=book))
            for key, book in list(self.books.items()):
                if key[1] == venue:
                    self.books[key] = book.model_copy(update={"synchronized": False})
            self.dirty_venues.add(venue)
            if any(o.venue == venue and o.status in UNRESOLVED for o in self.engine.risk.native_orders.values()):
                self.engine.risk.halt("Native private/public stream lost; reconciliation required")
            return
        # Private evidence is kept in the backend. Authoritative reconciliation
        # fetches actual cash fees; private acknowledgements are never fills.
        if ("orders" in frame or "positions" in frame or frame.get("private_event_type") == "orders"
                or frame.get("event") in {"orders", "v2.orders", "fill", "user_orders"}
                or frame.get("type") in {"fill", "user_order", "market_position"}):
            if self.engine.risk.native_orders:
                self.dirty_venues.add(venue)
            self.invalidate_account(venue)
        if self.mapper:
            for market, book in self.mapper.process(venue, client, frame):
                await self.engine.bus.publish(Envelope(schema_name="sports.market", source=venue, payload=market))
                await self.engine.bus.publish(Envelope(schema_name="exchange.native_book", source=venue, payload=book))

    def prophetx_research_books(self, *, limit: int = 20, event_id: str | None = None,
                                market_id: str | None = None, strike_id: str | None = None) -> dict:
        """Read-only native research view; no canonical or execution mapping."""
        record = self.engine.registry.records.get("prophetx")
        monitor = record.plugin if record and record.enabled else None
        client = getattr(monitor, "client", None)
        if client is None or not hasattr(client, "public_market_data"):
            return {"available": False, "reason": "ProphetX Trading/MM monitor unavailable",
                    "levels": [], "read_only": True}
        connected = (getattr(monitor, "task", None) is not None and not monitor.task.done()
                     and client.health_check().get("streaming") is True)
        return {"available": True, "environment": client.settings.environment,
                "streaming": connected,
                **client.public_market_data.snapshot(
                    now=utcnow(), connected=connected, limit=limit, event_id=event_id,
                    market_id=market_id, strike_id=strike_id)}

    def polymarket_us_research_books(self, *, limit: int = 20) -> dict:
        """Current public/streamed US instrument depth, before mapping approval."""
        from ..integrations.polymarket_us_research import current_book, direction_levels, taker_fee
        from decimal import Decimal

        record = self.engine.registry.records.get("polymarket_us")
        monitor = record.plugin if record and record.enabled else None
        client = getattr(monitor, "client", None)
        if client is None:
            return {"available": False, "markets": [], "read_only": True,
                    "reason": "Polymarket US public discovery is not enabled"}
        now = utcnow()
        catalog_at = getattr(monitor, "research_catalog_at", None)
        catalog_current = bool(catalog_at and 0 <= (now-catalog_at).total_seconds()
                               <= 2*monitor.context.config.get("discovery_interval_seconds", 300))
        stream_connected = client.health_check().get("market_streaming") is True
        rows = []
        for native in getattr(monitor, "research_catalog_rows", [])[:limit]:
            book = current_book(native, client, now=now, stream_connected=stream_connected)
            if not catalog_current:
                book = {**book, "current": False, "bids": [], "offers": [],
                        "rejection": "Native catalog has expired"}
            directions = []
            for direction in ("long", "short"):
                levels = direction_levels(book, direction)
                first_price, first_qty = levels[0] if levels else (None, None)
                fee = None
                if first_price is not None:
                    try:
                        fee = str(taker_fee(native, first_price, min(Decimal(1), first_qty)))
                    except (ValueError, TypeError, ArithmeticError):
                        pass
                directions.append({"direction": direction, "outcome": next((s["description"] for s in native["sides"]
                    if s["long"] == (direction == "long")), None),
                    "native_price": str(first_price if direction == "long" else 1-first_price) if first_price is not None else None,
                    "effective_win_cost_usd": str(first_price) if first_price is not None else None,
                    "available_contracts": str(first_qty) if first_qty is not None else "0",
                    "one_contract_taker_fee_usd": fee,
                    "margin_locked_per_contract_usd": "1" if direction == "short" else "0",
                    "sale_proceeds_per_contract_usd": str(1-first_price) if direction == "short" and first_price is not None else None})
            rows.append({"slug": native["slug"], "event_slug": native["event_slug"],
                         "league": native["league"], "event": native["event_title"],
                         "question": native["question"], "market_type": native["market_type"],
                         "market_status": native["status"], "book_state": book.get("state"),
                         "book_source": book.get("source"), "source_time": book.get("source_time"),
                         "source_age_seconds": book.get("source_age_seconds"),
                         "current": book.get("current", False), "rejection": book.get("rejection"),
                         "fee_coefficient": native.get("fee_coefficient"),
                         "minimum_quantity": native.get("minimum_quantity"),
                         "bid_levels": book.get("bids", [])[:5], "offer_levels": book.get("offers", [])[:5],
                         "directions": directions, "mapping_reviewed": False,
                         "execution_eligible": False})
        return {"available": True, "environment": client.settings.environment,
                "authenticated": client.authenticated, "market_streaming": stream_connected,
                "catalog_at": catalog_at.isoformat() if catalog_at else None,
                "catalog_current": catalog_current, "discovered_count": len(getattr(monitor, "research_catalog_rows", [])),
                "subscribed_count": len(client.market_slugs),
                "current_market_count": sum(row["current"] for row in rows),
                "markets": rows, "read_only": True, "execution_eligible": False}

    def runtime_facts(self, venue=None):
        from decimal import Decimal
        from .native_pricing import instrument_fingerprint
        from ..venues.settlement import supports_settlement_scope

        engine = self.engine
        # Loaded profiles are validated in start(); an invalid/absent artifact
        # must never be replaced by inferred defaults in a readiness report.
        selected = self.profile_for(venue)
        p = selected if isinstance(selected, LaunchProfile) else None
        now = utcnow()
        venue = p.venue if p else ""
        record = engine.registry.records.get(venue)
        health = record.health if record else {}
        client = getattr(record.plugin, "client", None) if record else None
        adapter = self.manager.adapters.get(venue)
        snapshot = self.account_snapshots.get(venue)
        workers = {task.get_name(): task for task in getattr(engine, "tasks", ())}

        def running(task):
            return task is not None and not task.done() and not task.cancelled()

        def worker(name):
            return running(workers.get(name))

        def available_queue(name):
            queue = getattr(engine, name, None)
            return queue is not None and not queue.full()

        def numeric(value):
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                return None
            number = Decimal(str(value))
            return number if number.is_finite() else None

        # A stale, naive, foreign-environment or foreign-account inventory must
        # not authorize orders after account changes. A successful balance read
        # does not imply matching inventory or private channel synchronization.
        account_age = None
        if snapshot and isinstance(snapshot.observed_at, datetime) and snapshot.observed_at.tzinfo is not None:
            account_age = (now-snapshot.observed_at).total_seconds()
        account_current = bool(p and snapshot and snapshot.identity_verified and account_age is not None
            and 0 <= account_age < 30 and snapshot.venue == venue and snapshot.environment == p.environment
            and snapshot.account_fingerprint == p.account_fingerprint
            and client is not None and client.settings.environment == p.environment
            and venue not in getattr(self, "_account_probes", ())
            and (not engine.risk.config.available_cash_only or getattr(snapshot, "cash_available_verified", False)))
        if venue == "kalshi" and account_current:
            account_current = (snapshot.exchange_index is not None
                and snapshot.exchange_index == self._account_exchange_indexes.get(venue))
        # Every installed snapshot must describe the exact durable ledger.
        fences = getattr(self, "_account_snapshot_fences", {})
        if account_current:
            account_current = venue in fences and fences[venue] == self._ledger_fence()
        risk = engine.risk.config
        limits = None
        try:
            if risk.live_max_order_stake is not None or risk.live_stake_authorized:
                caps = [v for v in (risk.max_order_stake, risk.live_max_order_stake) if v is not None]
                limits = LiveLimits(
                    max_order_stake_usd=min(caps) if caps else None,
                    max_event_exposure_usd=risk.max_event_exposure, max_player_exposure_usd=risk.max_player_exposure,
                    max_venue_exposure_usd=risk.max_venue_exposure, max_correlated_exposure_usd=risk.max_correlated_exposure,
                    max_total_exposure_usd=risk.max_total_exposure, daily_loss_limit_usd=risk.daily_loss_limit,
                    available_cash_only=risk.available_cash_only,
                    block_overlapping_events=risk.block_overlapping_events,
                    block_overlapping_players=risk.block_overlapping_players,
                    stake_sizing=risk.stake_sizing, unit_size_usd=risk.unit_size_usd,
                    max_open_orders=risk.max_open_orders, max_orders_per_minute=risk.max_orders_per_minute,
                    # Legacy profiles did not authorize a lifetime attempt
                    # count. Preserve their prior comparison while a pilot
                    # binds and rechecks its explicit one-attempt limit.
                    max_native_order_attempts=(risk.max_native_order_attempts
                        if p and p.limits.max_native_order_attempts is not None else None),
                    max_native_daily_attempts=risk.max_native_daily_attempts,
                    max_native_daily_debit_usd=risk.max_native_daily_debit)
        except (ValueError, TypeError):
            # Contradictory runtime limits are a missing authorization, not a
            # dashboard exception and never a reason to weaken the limits.
            pass
        feeds = []
        for name in p.provider_ids if p else ():
            item = engine.registry.records.get(name)
            h = item.health if item else {}
            healthy_states = {"connected", "streaming", "polling"}
            connected = bool(item and item.enabled and running(getattr(item.plugin, "task", None))
                and h.get("state", h.get("status")) in healthy_states
                and h.get("status", h.get("state")) in healthy_states)
            quota = h.get("quota", {})
            quota_ok = isinstance(quota, dict) and bool(quota)
            if quota_ok:
                cooldown = numeric(quota.get("cooldown_seconds"))
                quota_ok = cooldown is not None and cooldown == 0
                for key in ("provider_remaining", "minute_remaining"):
                    value = quota.get(key)
                    if value is not None:
                        number = numeric(value)
                        quota_ok = quota_ok and number is not None and number > 0
                budget, used = numeric(quota.get("local_session_budget")), numeric(quota.get("reserved_cost"))
                # The adapter's local budget is required even when the provider
                # omits usage headers. Missing or malformed usage stays unknown.
                quota_ok = quota_ok and budget is not None and used is not None and 0 <= used < budget
            transports = item.plugin.manifest.transports if item else []
            transport = next((t for t in ("websocket", "sse", "rest") if t in transports), "unknown")
            synchronized = connected and (transport == "rest" or h.get("streaming") is True)
            if transport == "websocket" and h.get("awaiting_snapshot_sports"):
                synchronized = False
            feeds.append(FeedReadiness(provider_id=name, enabled=bool(item and item.enabled),
                authenticated=h.get("authenticated") is True, connected=connected,
                entitlement_confirmed=connected, quota_available=connected and quota_ok,
                circuit_open=not connected, transport=transport,
                synchronization_ready=synchronized))
        markets = []
        for market_id in p.market_ids if p else ():
            market = engine.markets.get(market_id)
            books = [b for key, b in self.books.items() if key[0] == market_id and key[1] == venue]
            refs, fair = (), None
            if market:
                try:
                    fair = fair_value(market, list(engine.quotes.get(market_id, {}).values()), engine.settings.pricing, now=now)
                    refs = fair.references
                except NoSignal: pass
            ages = [b.age_ms(now) for b in books]
            valid_ages = bool(ages) and all(a is not None and a >= 0 for a in ages)
            reference_ages = [r.get("age_ms") for r in refs]
            oldest_reference_age = (max(reference_ages) if reference_ages and all(
                isinstance(age, (int, float)) and age >= 0 for age in reference_ages) else None)
            age_basis = "selection_observation"
            if (market and fair and fair.live_execution_allowed
                    and market_config(engine.settings.pricing, market).reference_quality_mode == "trusted_provider"):
                delivery_ages = [r.get("delivery_age_ms") for r in refs]
                oldest_reference_age = (max(delivery_ages) if delivery_ages and all(
                    isinstance(age, (int, float)) and age >= 0 for age in delivery_ages) else None)
                age_basis = "provider_delivery"
            exact_mapping = bool(market and books and all(b.market_fingerprint == market.identity()
                and b.instrument.settlement_rules_fingerprint == market.identity() and b.instrument.verified
                and b.instrument.canonical_market_id == market_id and b.instrument.venue == venue
                and b.instrument.outcome in market.outcomes for b in books))
            markets.append(MarketReadiness(market_id=market_id,
                identity_verified=exact_mapping,
                provider_mapping_verified=bool(refs) and all(r.get("provider") in p.provider_ids for r in refs),
                venue_mapping_verified=exact_mapping,
                fee_schedule_verified=bool(books) and all(b.fee_bound.verified
                    and b.fee_bound.instrument_fingerprint == instrument_fingerprint(b.instrument)
                    and b.fee_bound.observed_at <= now < b.fee_bound.valid_until for b in books),
                native_units_supported=bool(books), order_minimum_verified=bool(books) and all(
                    b.instrument.order_minimum_verified and b.instrument.minimum_order_cost_usd is not None
                    and b.instrument.order_minimum_source for b in books), usable_reference_books=len(refs),
                required_books_present=bool(fair and fair.execution_eligible and
                    (engine.settings.mode != "production" or fair.live_execution_allowed)),
                oldest_reference_age_ms=oldest_reference_age,
                reference_age_basis=age_basis,
                orderbook_age_ms=max(ages) if valid_ages else None,
                orderbook_synchronized=bool(books) and all(b.synchronized for b in books),
                market_active=bool(market and market.status not in {"closed", "suspended"} and books and all(b.status == "active" for b in books)),
                executable_liquidity=any(b.asks for b in books),
                synthetic=fair is None or fair.synthetic or any(b.synthetic for b in books)))
        unknown = sum(o.status == OrderStatus.UNKNOWN for o in engine.risk.native_orders.values())
        # The first pilot intent must survive the durable reservation and both
        # preflight checks. Count it as consumed once an outcome exists (or its
        # identity is uncertain), but not while this one unsent intent is being
        # checked. The shared OMS lock and one-attempt risk cap prevent a second
        # intent during that short window.
        prior_attempts = pilot_prior_attempts(engine.risk.native_orders.values())
        storage_healthy = (engine.storage.connection is not None and engine.storage.failure is None
            and running(engine.storage.task) and not engine.storage.queue.full())
        execution_workers = worker("risk-routing") and worker("normalization-strategy") and worker("native-reconciliation")
        native_bound = bool(record and record.enabled and running(getattr(record.plugin, "task", None))
            and self.owns_execution_lease()
            and adapter is not None and getattr(adapter, "client", None) is client and account_current
            and getattr(adapter, "bound_account_fingerprint", "") == snapshot.account_fingerprint
            and callable(getattr(adapter, "submit_intent", None)) and callable(getattr(adapter, "reconcile_native", None))
            and callable(getattr(adapter, "cancel_native", None)))
        return RuntimeReadiness(environment=engine.settings.mode, venue=venue,
            account_fingerprint=snapshot.account_fingerprint if account_current else "", build_id=self.build_id,
            mapping_digest=self.mapper.mapping_digest if self.mapper else "", pricing_digest=digest_json(engine.settings.pricing.model_dump(mode="json")),
            configured_limits=limits, enabled_strategy_ids=tuple(name for name,r in engine.registry.records.items()
                if r.enabled and name == "native_positive_ev"),
            central_risk_enforced=execution_workers and available_queue("risk_queue"), opposing_position_guards=True,
            external_exposure_bounded=not getattr(self, "pending_external_orders", ()),
            durable_intents=storage_healthy and execution_workers,
            persistence_healthy=storage_healthy and worker("audit-enqueue") and available_queue("audit_queue"),
            signal_ledger_healthy=engine.signal_ledger.started and storage_healthy and worker("signal-ledger") and available_queue("signal_queue"),
            native_routing_supported=native_bound and execution_workers and not self.profile_error,
            private_stream_authenticated=bool(record and record.enabled and health.get("authenticated") is True
                                              and running(getattr(record.plugin, "task", None))),
            private_stream_synchronized=self.private_synchronized(record, snapshot) and account_current,
            reconciliation_healthy=bool(account_current and snapshot.reconciled and worker("native-reconciliation")
                                        and not self.manager.invalidated_settlements),
            startup_recovery_complete=self.manager.recovered and unknown == 0 and not self.manager.invalidated_settlements and worker("native-reconciliation"),
            cancellation_races_supported=native_bound,
            settlement_reconciliation_supported=native_bound and supports_settlement_scope(
                p, adapter, self.mapper.mappings if self.mapper else (), tuple(engine.risk.native_orders.values())),
            native_credentials_present=bool(record and record.credential_status != "missing"),
            account_identity_verified=account_current,
            geographic_attestation_current=bool(p and account_current and
                current_geographic_attestation(snapshot, p.account_fingerprint, p.environment, now)),
            unresolved_unknown_orders=unknown,
            reconciliation_discrepancies=(1 if snapshot and not snapshot.reconciled else 0) + len(self.manager.invalidated_settlements),
            native_pilot_prior_attempts=prior_attempts,
            feeds=tuple(feeds), markets=tuple(markets), live_enabled=engine.settings.live_enabled,
            auto_trade=engine.risk.auto_trade, kill_switch=engine.risk.kill_switch)

    def reports(self):
        return {venue: evaluate_readiness(p, self.runtime_facts(venue))
                for venue, p in self.profiles_by_venue().items()}

    def runtime_facts_by_venue(self):
        return {venue: self.runtime_facts(venue) for venue in self.profiles_by_venue()}

    def report(self, venue=None):
        if not getattr(self, "portfolio", None) or venue is not None:
            report = evaluate_readiness(self.profile_for(venue), self.runtime_facts(venue))
        else:
            reports = self.reports()
            report = ReadinessReport(evaluated_at=utcnow(), environment=self.engine.settings.mode,
                venue="portfolio", profile_digest=self.portfolio.digest(),
                technical_ready=all(r.technical_ready for r in reports.values()),
                authorization_ready=all(r.authorization_ready for r in reports.values()),
                safe_to_arm=all(r.safe_to_arm for r in reports.values()),
                live_trading_ready=all(r.live_trading_ready for r in reports.values()),
                sandbox_trading_ready=all(r.sandbox_trading_ready for r in reports.values()),
                blockers=tuple(b.model_copy(update={"scope": f"{v}/{b.scope}"})
                    for v, r in reports.items() for b in r.blockers),
                performance=next(iter(reports.values())).performance)
        if self.profile_error:
            report = report.model_copy(update={"technical_ready": False, "safe_to_arm": False,
                "live_trading_ready": False, "sandbox_trading_ready": False,
                "blockers": (*report.blockers, ReadinessBlocker(code="profile.invalid", category="technical",
                    scope="configuration", message=self.profile_error, action="Correct the launch files and restart paused"))})
        return report

    def private_synchronized(self, record, snapshot):
        if (record is None or not record.enabled or snapshot is None or not snapshot.identity_verified or not snapshot.reconciled
                or record.health.get("authenticated") is not True or record.health.get("streaming") is not True):
            return False
        client = getattr(record.plugin, "client", None)
        book = getattr(client, "book", None)
        tracker = getattr(book, "sequences", None)
        if client is None or snapshot.venue != getattr(client, "venue_id", None) or snapshot.environment != client.settings.environment:
            return False
        if getattr(client, "venue_id", None) == "novig" and tracker is not None:
            orders, positions = tracker.values.get("PRIVATE:orders"), tracker.values.get("PRIVATE:positions")
            # None == None is not evidence of an authenticated initial snapshot.
            known = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        for value in (orders, positions, snapshot.order_sequence, snapshot.position_sequence))
            return (known and not tracker.invalid and orders == snapshot.order_sequence
                    and positions == snapshot.position_sequence)
        if getattr(client, "venue_id", None) == "kalshi":
            from ..venues.kalshi import KalshiClient
            return isinstance(client, KalshiClient) and client.private.matches(snapshot)
        if getattr(client, "venue_id", None) == "prophetx":
            from ..venues.prophetx import ProphetXClient
            return isinstance(client, ProphetXClient) and client.private.matches(snapshot, client=client)
        if getattr(client, "venue_id", None) == "polymarket_us":
            from ..venues.polymarket_us_execution import PolymarketUSExecutionVenue
            adapter = self.manager.adapters.get("polymarket_us")
            return bool(isinstance(adapter, PolymarketUSExecutionVenue) and adapter.client is client
                and adapter.private_snapshot_complete and getattr(client, "_ready", {}).get("private") is True
                and snapshot is not None
                and type(snapshot.private_connection_generation) is int
                and type(snapshot.private_revision) is int
                and snapshot.private_connection_generation == adapter.private_generation
                and snapshot.private_revision == adapter.private_revision)
        return False

    async def maintain(self):
        while True:
            await asyncio.sleep(1)
            await self.maintenance_step()

    async def maintenance_step(self):
        if self.engine.risk.kill_switch:
            await self.manager.cancel_all()
        # Establish authenticated identity even during restart. Known pending
        # local changes temporarily block new orders without declaring an
        # ordinary acknowledgement to be an unexplained inventory mismatch.
        await self.refresh_account()
        reconciled_venues = set()
        for venue in list(self.dirty_venues):
            self.dirty_venues.discard(venue)
            for order in list(self.engine.risk.native_orders.values()):
                if order.venue != venue:
                    continue
                try:
                    await asyncio.wait_for(self.manager.reconcile(order.id), timeout=20)
                    reconciled_venues.add(venue)
                except Exception:
                    self.engine.risk.halt("Native order reconciliation failed")
        if reconciled_venues:
            for venue in reconciled_venues:
                self.invalidate_account(venue)
            await self.refresh_account()
        # Recheck standing orders against fresh references and original limits.
        # Racing fills remain reserved until authoritative reconciliation.
        for order in list(self.engine.risk.native_orders.values()):
            if order.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}:
                try:
                    self.engine.risk.check_enabled()
                    self.require_preflight(order)
                    self.revalidate_order(order)
                except RiskRejected:
                    await self.manager.cancel(order.id)
        if self.engine.risk.kill_switch:
            await self.manager.cancel_all()

    async def maintain_settlements(self):
        while True:
            await asyncio.sleep(30)
            await self.reconcile_settlements()

    async def reconcile_settlements(self, *, force=False, venue=None, order_ids=None, halt_unverified=True) -> int | None:
        # None means a forced check was deferred, never a completed check with
        # no payment. Concurrent portfolio probes share this coordinator.
        if self._settlement_probe_lock.locked():
            return None if force else 0
        if not force and time.monotonic()-self.last_settlement_probe < 30:
            return 0
        async with self._settlement_probe_lock:
            return await self._reconcile_settlements(force=force, venue=venue, order_ids=order_ids,
                                                     halt_unverified=halt_unverified)

    async def _reconcile_settlements(self, *, force, venue, order_ids, halt_unverified):
        if not force:
            self.last_settlement_probe = time.monotonic()
        from ..venues.settlement import reconcile_novig_settlement
        from ..venues.kalshi_settlement import reconcile_kalshi_settlement
        from ..venues.prophetx_settlement import reconcile_prophetx_settlement
        reconcilers = {"novig": reconcile_novig_settlement, "kalshi": reconcile_kalshi_settlement,
            "prophetx": lambda client, order, inventory: reconcile_prophetx_settlement(
                client, order, inventory, review=getattr(client, "account_review", None))}
        inventory = tuple(self.engine.risk.native_orders.values())
        completed = 0
        for order in inventory:
            adapter = self.manager.adapters.get(order.venue)
            if (order.venue not in reconcilers or adapter is None or order.filled_quantity <= 0 or
                    (venue is not None and order.venue != venue) or (order_ids is not None and order.id not in order_ids) or
                    order.status not in {OrderStatus.FILLED, OrderStatus.CANCELLED} or
                    not order.reconciliation_complete or order.id in self.manager.invalidated_settlements):
                continue
            accepted = order.id in self.manager.settlements
            try:
                result = await asyncio.wait_for(reconcilers[order.venue](adapter.client, order, inventory), timeout=20)
                if result is None:
                    if accepted:
                        self.invalidate_account(order.venue)
                        await self.manager.invalidate_settlement(order.id, "Previously settled native market is no longer graded")
                else:
                    # Clear cash trust before any awaitable durable settlement
                    # write; the matching account must subsequently be reprobed.
                    if not accepted:
                        self.invalidate_account(order.venue)
                    await self.manager.settle(result)
                    if not accepted:
                        completed += 1
            except Exception as exc:
                self.invalidate_account(order.venue)
                if accepted:
                    await self.manager.invalidate_settlement(order.id, "Accepted native settlement could not be reverified")
                elif force and not halt_unverified and isinstance(exc, TimeoutError):
                    # No result arrived: keep its exposure and retry without
                    # treating the elapsed deadline as contrary cash evidence.
                    return None
                elif halt_unverified:
                    self.engine.risk.halt("Native settlement requires complete attributable cash evidence")
        return completed

    async def refresh_account(self):
        # Only one read owns installation. Neither a concurrent private frame
        # nor a newer ledger may be overwritten by an earlier in-flight probe.
        if self._account_probe_lock.locked() or not self.profiles_by_venue():
            return
        async with self._account_probe_lock:
            if self.portfolio:
                await asyncio.gather(*(self._refresh_account(p) for p in self.portfolio.profiles))
            else:
                await self._refresh_account()

    async def _refresh_account(self, p=None):
        p = p or self.profile
        record = self.engine.registry.records.get(p.venue)
        client = getattr(record.plugin, "client", None) if record else None
        if client is None or client.settings.environment != p.environment:
            return
        adapter = self.bind_client(p.venue, client)
        last_probe = self._last_account_probes.get(p.venue, 0) if self.portfolio else self.last_account_probe
        if time.monotonic()-last_probe < (5 if adapter is not None else 20):
            return
        from ..venues.account_probe import probe_native_account
        async def read_account():
            from ..venues.kalshi import KalshiClient
            exchange_index = (await self._kalshi_account_exchange_index(client, p)
                if isinstance(client, KalshiClient) else None)
            async with self.manager.lock:
                self.invalidate_account(p.venue)
                self.last_account_probe = time.monotonic()
                self._last_account_probes[p.venue] = self.last_account_probe
                token = (self._account_generations[p.venue], self._ledger_fence())
                inventory = tuple(self.engine.risk.native_orders.values())
                settled = frozenset(self.engine.risk.settled)
            snapshot = await asyncio.wait_for(probe_native_account(client,
                managed_orders=inventory, settled_order_ids=settled,
                **({"external_positions": tuple(row for row in self.external_positions
                     if row.venue == p.venue)} if any(row.venue == p.venue
                     for row in self.external_positions) else {}),
                **({"exchange_index": exchange_index} if isinstance(client, KalshiClient) else {}),
                **({"adapter": adapter} if p.venue == "polymarket_us" else {}),
                **({"management_client": record.plugin.management_client}
                   if p.venue == "novig" and getattr(record.plugin, "management_client", None) else {})), timeout=20)
            return snapshot, token, exchange_index

        def unchanged(token):
            current = self.engine.registry.records.get(p.venue)
            return (self.profile_for(p.venue) is p and current and getattr(current.plugin, "client", None) is client
                    and self.manager.adapters.get(p.venue) is adapter
                    and token == (self._account_generations.get(p.venue, 0), self._ledger_fence()))

        self._account_probes.add(p.venue)
        try:
            snapshot, token, exchange_index = await read_account()
            async with self.manager.lock:
                if not unchanged(token):
                    self.last_account_probe = 0.0
                    self._last_account_probes.pop(p.venue, None)
                    return
                pending = any(o.venue == p.venue and (not o.reconciliation_complete or o.status == OrderStatus.UNKNOWN)
                              for o in self.engine.risk.native_orders.values())
                possible_settlements = {o.id for o in self.engine.risk.native_orders.values()
                    if o.venue == p.venue and o.mode == p.environment and o.account_fingerprint == snapshot.account_fingerprint
                    and o.status in {OrderStatus.FILLED, OrderStatus.CANCELLED} and o.filled_quantity > 0
                    and o.reconciliation_complete and o.id not in self.engine.risk.settled
                    and o.id not in self.manager.invalidated_settlements}
            if snapshot.identity_verified and not snapshot.reconciled and not pending and possible_settlements:
                # A filled position can legitimately disappear when settled.
                # One bounded actual-cash attempt precedes an unexplained-
                # inventory halt; no order is excluded merely for disappearing.
                try:
                    completed = await asyncio.wait_for(self.reconcile_settlements(force=True, venue=p.venue,
                        order_ids=set(sorted(possible_settlements)[:20]), halt_unverified=False), timeout=20)
                except TimeoutError:
                    completed = None
                if completed is None:
                    # Busy coordinators and deadlines leave the account
                    # unverified. A later maintenance pass retries immediately;
                    # neither old cash nor the incomplete probe can authorize.
                    self.invalidate_account(p.venue)
                    return
                snapshot, token, exchange_index = await read_account()
            async with self.manager.lock:
                if not unchanged(token):
                    self.last_account_probe = 0.0
                    self._last_account_probes.pop(p.venue, None)
                    return
                self.account_snapshots[p.venue] = snapshot
                if exchange_index is not None:
                    self._account_exchange_indexes[p.venue] = exchange_index
                self._account_snapshot_fences[p.venue] = token[1]
                if adapter is not None:
                    adapter.bound_account_fingerprint = snapshot.account_fingerprint if snapshot.identity_verified else ""
                    if p.venue == "kalshi":
                        adapter.bound_exchange_index = snapshot.exchange_index if snapshot.identity_verified else None
                pending = any(o.venue == p.venue and (not o.reconciliation_complete or o.status == OrderStatus.UNKNOWN)
                              for o in self.engine.risk.native_orders.values())
                if self.engine.risk.auto_trade and (not snapshot.identity_verified or (not snapshot.reconciled and not pending)):
                    self.engine.risk.halt("Native account inventory differs from the managed order ledger")
        except asyncio.CancelledError:
            self.invalidate_account(p.venue)
            raise
        except Exception:
            self.invalidate_account(p.venue)
            if self.engine.risk.auto_trade:
                self.engine.risk.halt("Native account inventory probe failed")
        finally:
            self._account_probes.discard(p.venue)


class NativeSignalObservation(Contract):
    market_id: str
    status: str
    reason: str
    candidates: tuple[dict, ...] = ()
    market: dict = Field(default_factory=dict)
    reference_quotes: tuple[dict, ...] = ()
    pricing_policy: dict = Field(default_factory=dict)
    # source observations and quantity-specific cash calculations stay nested;
    # no conversion into legacy one-dollar signal/fill records.
    evaluated_at: datetime = Field(default_factory=utcnow)
