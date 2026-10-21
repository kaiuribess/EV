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
from pathlib import Path
from pydantic import Field

from .conflicts import UNRESOLVED, reviewed_correlations, risk_family
from .models import Contract, Envelope, OrderStatus, utcnow
from .native_oms import NativeOrder, NativeOrderManager
from .pricing import NoSignal, fair_value
from .readiness import (
    FeedReadiness, LaunchProfile, LiveLimits, MarketReadiness,
    RuntimeReadiness, digest_json, evaluate_readiness,
)
from .risk import RiskRejected


def build_fingerprint():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class NativeRuntime:
    def __init__(self, engine):
        self.engine = engine
        self.profile = None
        self.profile_error = ""
        self.build_id = ""
        self.mapper = None
        self.books = {}
        self.candidates = {}
        self.signals = {}
        self.account_snapshots = {}
        self.dirty_venues = set()
        self.last_account_probe = 0.0
        self.last_settlement_probe = 0.0
        self.manager = NativeOrderManager(engine.risk, engine.storage, engine.bus, engine.metrics,
            lock=engine.oms.lock, revalidate=self.revalidate_order, preflight=self.require_preflight)

    async def start(self):
        self.build_id = await asyncio.to_thread(build_fingerprint)
        profile_path = Path("data/launch_profile.json")
        if profile_path.exists():
            try:
                self.profile = await asyncio.to_thread(lambda: LaunchProfile.model_validate_json(profile_path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                self.profile_error = "Launch profile is invalid; validate it before native execution"
        if self.profile and self.profile.environment == self.engine.settings.mode:
            # Native money limits have no implicit defaults: only an explicitly
            # written profile selects them. This does not grant write access.
            values = self.profile.limits.model_dump()
            limits = {key.removesuffix("_usd"): value for key,value in values.items()}
            limits["live_max_order_stake"] = limits["max_order_stake"]
            self.engine.risk.config = type(self.engine.risk.config).model_validate({
                **self.engine.risk.config.model_dump(), **limits})
            self.engine.settings.risk = self.engine.risk.config
        from ..venues.book_normalization import NativeBookMapper
        try:
            self.mapper = await asyncio.to_thread(NativeBookMapper.from_file, "data/native_mappings.json")
        except (ValueError, OSError):
            self.profile_error = "Native mappings are invalid; review the exact market and fee evidence"
        await self.manager.start()

    def require_preflight(self, order):
        report = self.report()
        if not (report.live_trading_ready or report.sandbox_trading_ready):
            raise RiskRejected("Native release gate has unresolved checks")
        p = self.profile
        if (p is None or order.venue != p.venue or order.mode != p.environment or
                order.account_fingerprint != p.account_fingerprint or order.market_id not in p.market_ids or
                order.strategy not in p.strategy_ids):
            raise RiskRejected("Native order is outside the authorized launch scope")
        if p.settlement_policy == "novig_ioc_win_loss_halt_on_exception" and any(
                previous.id != order.id and previous.venue == order.venue and previous.mode == order.mode
                and previous.account_fingerprint == order.account_fingerprint
                and previous.intent.instrument.instrument_id == order.intent.instrument.instrument_id
                for previous in self.engine.risk.native_orders.values()):
            raise RiskRejected("Scoped Novig settlement permits only one managed order per native market")
        snapshot = self.account_snapshots.get(order.venue)
        if snapshot is None or snapshot.balance_usd is None or snapshot.balance_usd < self.engine.risk.exposures() + (0 if order.id in self.engine.risk.native_orders else order.reserved_cost):
            raise RiskRejected("Authenticated native cash balance cannot cover reserved exposure")
        from ..venues.bridge import NativePreflight
        adapter = self.manager.adapters[order.venue]
        adapter.preflight = NativePreflight(environment=p.environment, account_authorized=True,
            geographic_eligibility_verified=p.environment == "production", native_trading_enabled=True,
            evidence=p.digest(), valid_until=min(time.time()+5, *(e.expires_at.timestamp() for e in p.evidence)))

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
            native_revalidate_intent(order.intent, market, fair, book, self.engine.settings.pricing,
                                     remaining_quantity=order.quantity-order.filled_quantity,
                                     fees_already_paid_usd=order.fees)
        except (NoSignal, ValueError) as exc:
            raise RiskRejected(str(exc)) from exc

    async def execute(self, candidate):
        if not self.profile:
            raise RiskRejected("Native launch profile required")
        market = self.engine.markets[candidate.market_id]
        book = self.books.get((market.id, candidate.venue, candidate.outcome))
        age = (utcnow()-candidate.calculated_at).total_seconds()*1000
        if (book is None or candidate.book_revision != book.revision or age < 0 or
                age > self.engine.risk.config.max_candidate_age_ms or not candidate.qualified):
            raise RiskRejected("Native candidate or order book changed")
        order = NativeOrder(intent=candidate.intent, candidate_id=candidate.id,
            strategy=candidate.strategy, account_fingerprint=self.profile.account_fingerprint,
            market_snapshot=market, market_fingerprint=market.identity(), risk_family_id=risk_family(market),
            reviewed_correlation_groups=reviewed_correlations(market))
        try:
            return await self.manager.execute(order)
        finally:
            if order.id in self.engine.risk.native_orders:
                # Even an ordinary acknowledgement changes account inventory.
                # Block new orders until the durable ledger and account agree.
                self.dirty_venues.add(order.venue)
                self.account_snapshots.pop(order.venue, None)
                self.last_account_probe = 0.0

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
                candidates = await record.plugin.evaluate(event, {
                    "market": self.engine.markets[market_id],
                    "quotes": list(self.engine.quotes.get(market_id, {}).values()),
                    "native_books": books, "config": self.engine.settings.pricing,
                    "max_stake_usd": self.engine.risk.config.live_max_order_stake,
                    "mode": self.engine.settings.mode,
                })
                reason = "Native fee-bounded executable offers"
            except (NoSignal, ValueError) as exc:
                reason = str(exc)
        status = "POSITIVE EV" if any(c.qualified for c in candidates) else "NEGATIVE EV" if candidates else "NO SIGNAL"
        observation = NativeSignalObservation(market_id=market_id, status=status, reason=reason,
            candidates=tuple(c.model_dump(mode="json") for c in candidates))
        self.signals[market_id] = observation
        await self.engine.bus.publish(Envelope(schema_name="native.signal", source="native_positive_ev", payload=observation))
        for candidate in candidates:
            self.candidates[(candidate.market_id, candidate.venue, candidate.outcome)] = candidate
            if candidate.qualified and self.engine.risk.auto_trade and not self.engine.risk.kill_switch:
                await self.engine.bus.publish(Envelope(schema_name="trade.native_candidate", source=candidate.strategy, payload=candidate))

    def bind_client(self, venue, client):
        """Bind before the first private frame so its snapshot cannot be lost."""
        p = self.profile
        if not p or not self.mapper or venue != p.venue or client.settings.environment != p.environment:
            return None
        from ..venues.bridge import NativeExecutionVenue, VerifiedVenueMapping
        adapter = self.manager.adapters.get(p.venue)
        if adapter is None:
            mappings = [VerifiedVenueMapping(canonical_market_id=m.market.id, canonical_outcome=m.instrument.outcome,
                native_instrument_id=m.instrument.instrument_id, native_outcome=m.instrument.native_outcome,
                settlement_fingerprint=m.market.identity(), mapping_evidence=m.instrument.source,
                verified=True, fee_schedule_verified=m.fee_bound.verified,
                native_payout_usd=m.instrument.payout_per_unit_usd, native_quantity_unit=m.instrument.quantity_unit)
                for m in self.mapper.mappings if m.instrument.venue == p.venue]
            if not mappings: return None
            adapter = NativeExecutionVenue(client, self.engine.risk.authority, mappings=mappings)
            for order in self.engine.risk.native_orders.values():
                if order.venue == p.venue and order.venue_order_id:
                    adapter.bind_native_intent(order.venue_order_id, order.intent)
            self.manager.register(p.venue, adapter)
        if adapter.client is not client:
            self.engine.risk.halt("Native account connection changed; restart recovery required")
            return None
        return adapter

    async def on_frame(self, venue, client, frame):
        adapter = self.bind_client(venue, client)
        if adapter and hasattr(adapter, "observe_private_frame"):
            adapter.observe_private_frame(frame)
        if frame.get("type") in {"transport_invalidated", "snapshot_required"}:
            self.account_snapshots.pop(venue, None)
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
        if self.mapper:
            for market, book in self.mapper.process(venue, client, frame):
                await self.engine.bus.publish(Envelope(schema_name="sports.market", source=venue, payload=market))
                await self.engine.bus.publish(Envelope(schema_name="exchange.native_book", source=venue, payload=book))
        # Private evidence is kept in the backend. Authoritative reconciliation
        # fetches actual cash fees; private acknowledgements are never fills.
        if "orders" in frame or "positions" in frame or frame.get("event") in {"orders", "v2.orders", "fill", "user_orders"}:
            if self.engine.risk.native_orders:
                self.dirty_venues.add(venue)
            self.account_snapshots.pop(venue, None)

    def runtime_facts(self):
        from decimal import Decimal
        from .native_pricing import instrument_fingerprint
        from ..venues.settlement import supports_settlement_scope

        engine = self.engine
        # Loaded profiles are validated in start(); an invalid/absent artifact
        # must never be replaced by inferred defaults in a readiness report.
        p = self.profile if isinstance(self.profile, LaunchProfile) else None
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
            and client is not None and client.settings.environment == p.environment)
        risk = engine.risk.config
        limits = None
        try:
            if risk.live_max_order_stake is not None:
                limits = LiveLimits(
                    max_order_stake_usd=min(risk.max_order_stake, risk.live_max_order_stake),
                    max_event_exposure_usd=risk.max_event_exposure, max_player_exposure_usd=risk.max_player_exposure,
                    max_venue_exposure_usd=risk.max_venue_exposure, max_correlated_exposure_usd=risk.max_correlated_exposure,
                    max_total_exposure_usd=risk.max_total_exposure, daily_loss_limit_usd=risk.daily_loss_limit,
                    max_open_orders=risk.max_open_orders, max_orders_per_minute=risk.max_orders_per_minute)
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
                native_units_supported=bool(books), usable_reference_books=len(refs), required_books_present=bool(fair),
                oldest_reference_age_ms=max((r["age_ms"] for r in refs), default=None),
                orderbook_age_ms=max(ages) if valid_ages else None,
                orderbook_synchronized=bool(books) and all(b.synchronized for b in books),
                market_active=bool(market and market.status not in {"closed", "suspended"} and books and all(b.status == "active" for b in books)),
                executable_liquidity=any(b.asks for b in books),
                synthetic=fair is None or fair.synthetic or any(b.synthetic for b in books)))
        unknown = sum(o.status == OrderStatus.UNKNOWN for o in engine.risk.native_orders.values())
        storage_healthy = (engine.storage.connection is not None and engine.storage.failure is None
            and running(engine.storage.task) and not engine.storage.queue.full())
        execution_workers = worker("risk-routing") and worker("normalization-strategy") and worker("native-reconciliation")
        native_bound = bool(record and record.enabled and running(getattr(record.plugin, "task", None))
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
            account_identity_verified=account_current, unresolved_unknown_orders=unknown,
            reconciliation_discrepancies=(1 if snapshot and not snapshot.reconciled else 0) + len(self.manager.invalidated_settlements),
            feeds=tuple(feeds), markets=tuple(markets), live_enabled=engine.settings.live_enabled,
            auto_trade=engine.risk.auto_trade, kill_switch=engine.risk.kill_switch)

    def report(self):
        return evaluate_readiness(self.profile, self.runtime_facts())

    @staticmethod
    def private_synchronized(record, snapshot):
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
        reconciled = False
        for venue in list(self.dirty_venues):
            self.dirty_venues.discard(venue)
            for order in list(self.engine.risk.native_orders.values()):
                if order.venue != venue:
                    continue
                try:
                    await asyncio.wait_for(self.manager.reconcile(order.id), timeout=20)
                    reconciled = True
                except Exception:
                    self.engine.risk.halt("Native order reconciliation failed")
        if reconciled:
            self.last_account_probe = 0.0
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

    async def reconcile_settlements(self):
        if time.monotonic()-self.last_settlement_probe < 30:
            return
        self.last_settlement_probe = time.monotonic()
        from ..venues.settlement import reconcile_novig_settlement
        inventory = tuple(self.engine.risk.native_orders.values())
        for order in inventory:
            adapter = self.manager.adapters.get(order.venue)
            if (order.venue != "novig" or adapter is None or order.filled_quantity <= 0 or
                    order.status not in {OrderStatus.FILLED, OrderStatus.CANCELLED} or
                    not order.reconciliation_complete or order.id in self.manager.invalidated_settlements):
                continue
            accepted = order.id in self.manager.settlements
            try:
                result = await asyncio.wait_for(reconcile_novig_settlement(adapter.client, order, inventory), timeout=20)
                if result is None:
                    if accepted:
                        await self.manager.invalidate_settlement(order.id, "Previously settled native market is no longer graded")
                else:
                    await self.manager.settle(result)
            except Exception:
                if accepted:
                    await self.manager.invalidate_settlement(order.id, "Accepted native settlement could not be reverified")
                else:
                    self.engine.risk.halt("Native settlement requires complete attributable cash evidence")

    async def refresh_account(self):
        if not self.profile or not self.mapper:
            return
        p = self.profile
        record = self.engine.registry.records.get(p.venue)
        client = getattr(record.plugin, "client", None) if record else None
        if client is None or client.settings.environment != p.environment:
            return
        adapter = self.bind_client(p.venue, client)
        if adapter is None:
            return
        if time.monotonic()-self.last_account_probe < 5:
            return
        self.last_account_probe = time.monotonic()
        from ..venues.account_probe import probe_native_account
        try:
            snapshot = await asyncio.wait_for(probe_native_account(client,
                managed_orders=tuple(self.engine.risk.native_orders.values()),
                settled_order_ids=frozenset(self.engine.risk.settled)), timeout=20)
        except Exception:
            self.account_snapshots.pop(p.venue, None)
            if self.engine.risk.auto_trade:
                self.engine.risk.halt("Native account inventory probe failed")
            return
        self.account_snapshots[p.venue] = snapshot
        adapter.bound_account_fingerprint = snapshot.account_fingerprint if snapshot.identity_verified else ""
        pending = any(o.venue == p.venue and (not o.reconciliation_complete or o.status == OrderStatus.UNKNOWN)
                      for o in self.engine.risk.native_orders.values())
        if self.engine.risk.auto_trade and (not snapshot.identity_verified or (not snapshot.reconciled and not pending)):
            self.engine.risk.halt("Native account inventory differs from the managed order ledger")


class NativeSignalObservation(Contract):
    market_id: str
    status: str
    reason: str
    candidates: tuple[dict, ...] = ()
    # source observations and quantity-specific cash calculations stay nested;
    # no conversion into legacy one-dollar signal/fill records.
    evaluated_at: datetime = Field(default_factory=utcnow)
