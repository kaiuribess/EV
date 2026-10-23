from __future__ import annotations

import asyncio
import heapq
import time
from collections import OrderedDict, deque
from datetime import datetime
from decimal import Decimal

from .core.bus import EventBus, SchemaRegistry
from .core.config import Settings
from .core.metrics import Metrics
from .core.models import (
    Candidate, Contract, Envelope, Fill, GameState, Market, Order, OrderBook,
    PlayerProjection, ProviderFrame, Quote, SettlementResult, utcnow,
)
from .core.normalization import MarketMatcher
from .core.oms import OrderManager
from .core.native_oms import NativeOrder, NativeSettlement
from .core.native_pricing import NativeBook, NativeCandidate
from .core.native_runtime import NativeRuntime, NativeSignalObservation
from .core.plugins import ExecutionVenue, InputPlugin, PluginContext, PluginRegistry, StrategyPlugin
from .core.pricing import NoSignal, expected_fee, fair_value, market_config, opportunity, resolved_reference_policy, validate_fair_references, worst_fee
from .core.reference_profiles import freshness_rule, quote_eligibility, trusted_quote_research_eligibility
from .core.risk import RiskEngine, RiskRejected
from .core.signals import ResultProvenance, SignalLedger, SignalObservation, SignalResultImport
from .core.storage import Storage
from .plugins.demo import DemoInput
from .plugins.paper import PaperVenue
from .plugins.positive_ev import PositiveEV
from .plugins.native_positive_ev import NativePositiveEV
from .plugins.parlay_paper import ParlayPaperEV, ParlayPaperInput
from .plugins.oddize_paper import OddizePaperInput


class Notice(Contract):
    level: str
    message: str
    component: str


class Engine:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        self.risk = RiskEngine(self.settings.risk)
        self.schemas = SchemaRegistry()
        for name, model in [
            ("sports.market", Market), ("odds.quote", Quote), ("exchange.book", OrderBook),
            ("sports.game_state", GameState), ("player.projection", PlayerProjection),
            ("trade.candidate", Candidate), ("trade.order", Order), ("trade.fill", Fill),
            ("trade.settlement", SettlementResult),
            ("signal.observation", SignalObservation), ("signal.result", SignalResultImport),
            ("system.notice", Notice), ("provider.frame", ProviderFrame),
            ("exchange.native_book", NativeBook), ("trade.native_candidate", NativeCandidate),
            ("native.order", NativeOrder), ("native.settlement", NativeSettlement),
            ("native.signal", NativeSignalObservation),
        ]:
            self.schemas.register(name, 1, model)
        self.bus = EventBus(self.schemas, self.risk.halt)
        self.metrics = Metrics()
        from .core.paths import runtime_database_path
        self.settings.database_path = str(runtime_database_path(self.settings.database_path))
        self.storage = Storage(self.settings.database_path)
        self.storage.on_failure = self.risk.halt
        self.signal_ledger = SignalLedger(self.storage)
        self.matcher = MarketMatcher()
        self.markets = self.matcher.markets
        self.quotes: dict[str, dict[tuple[str, str], Quote]] = {}
        self.quote_observation_watermarks = {}
        self.quote_verification_latches = {}
        self.books: dict[str, dict[tuple[str, str], OrderBook]] = {}
        self.opportunities = {}
        self.reference_estimates = {}
        self.reference_fairs = {}
        self.research_history = deque(maxlen=100)
        self._last_current_research = {}
        self.signals = {}
        # Full raw inputs stay in the audit log. Repeating an unchanged
        # no-candidate evaluation for the same market every feed tick otherwise
        # writes the entire pricing policy and quote universe thousands of
        # times without creating a new signal episode.
        self._signal_audit_state = OrderedDict()
        self._signal_audit_estimate_fingerprints = {}
        self.history = {}
        self.latest_typed = {}
        self.expirations = []
        self.expiration_revisions = {}
        self.events = deque(maxlen=100)
        self.registry = PluginRegistry()
        self.paper = PaperVenue(self.risk.authority)
        self.registry.register(DemoInput(), enabled=(self.settings.mode in {"demo", "paper"}
                                                  and self.settings.sample_feed_enabled))
        self.registry.register(PositiveEV(), enabled=True)
        # The native pricing strategy only emits signals. Starting an explicitly
        # requested native workspace must not leave its reviewed strategy absent
        # from readiness; the separate risk controls still veto all orders.
        self.registry.register(NativePositiveEV(), enabled=(self.settings.mode in {"sandbox", "production"}
            and self.settings.live_enabled))
        self.registry.register(ParlayPaperEV(), enabled=(self.settings.mode == "paper"
            and not self.settings.sample_feed_enabled))
        self.registry.register(ParlayPaperInput(self), enabled=False)
        self.registry.register(OddizePaperInput(self), enabled=False)
        self.registry.register(self.paper, enabled=True)
        self.registry.configure("synthetic_schedule", {"interval": self.settings.demo_interval, "seed": self.settings.demo_seed})
        self.registry.configure("positive_ev", self.settings.pricing.model_dump(mode="json"))
        self.oms = OrderManager(self.risk, self.storage, self.bus, self.paper, self.book, self.revalidate, self.metrics)
        self.native = NativeRuntime(self)
        self.tasks = []
        self.started_at = utcnow()
        self.process_queue = self.bus.subscribe("strategy-engine", ["sports.market", "odds.quote", "exchange.book",
                                                                   "exchange.native_book",
                                                                   "sports.game_state", "player.projection"], critical=True)
        self.risk_queue = self.bus.subscribe("central-risk", ["trade.candidate", "trade.native_candidate"], critical=True)
        self.audit_queue = self.bus.subscribe("audit", critical=True)
        self.dashboard_queue = self.bus.subscribe("dashboard-events", ["trade.order", "trade.fill", "system.notice"])
        self.settlement_queue = self.bus.subscribe("settlement-accounting", ["trade.settlement"], critical=True)
        self.signal_queue = self.bus.subscribe("signal-ledger", ["signal.observation", "signal.result"], critical=True)
        self.settlements = {}
        self.closing_observations = {}
        self.stopping = False
        self.halt_persisted = False
        self.risk.on_halt = self._persist_halt
        self._plugin_enable_preferences: dict[str, bool] = {}
        self._plugin_toggle_lock = asyncio.Lock()

    def context(self, name):
        return PluginContext(self.bus, self.schemas, self.registry.records[name].config)

    def persistent_plugin_toggle(self, name):
        """Only source/monitor intent survives restart; never execution authorization."""
        from .plugins.external import NativeMonitor
        record = self.registry.records.get(name)
        if record is None or name == "synthetic_schedule":
            return False
        plugin = record.plugin
        return (not isinstance(plugin, (StrategyPlugin, ExecutionVenue))
                and isinstance(plugin, (InputPlugin, NativeMonitor)))

    def _restore_plugin_toggles(self, saved):
        self._plugin_enable_preferences = {}
        if not isinstance(saved, dict):
            return
        for name, enabled in saved.items():
            # bool is deliberate: strings, integers and objects must not activate feeds.
            if type(enabled) is bool and self.persistent_plugin_toggle(name):
                self._plugin_enable_preferences[name] = enabled
                self.registry.records[name].enabled = enabled

    async def toggle_plugin(self, name, enabled):
        async with self._plugin_toggle_lock:
            if self.persistent_plugin_toggle(name):
                preferences = {**self._plugin_enable_preferences, name: enabled}
                try:
                    await self.storage.save_config("plugin_enabled", preferences)
                except Exception:
                    raise RuntimeError("Plugin startup preference could not be saved") from None
                self._plugin_enable_preferences = preferences
            if enabled:
                await self.registry.start(name, self.context(name))
            else:
                await self.registry.stop(name)
                if name in {"synthetic_schedule", "positive_ev", "paper_execution"}:
                    self.risk.auto_trade = False
                    await self.oms.cancel_all()

    async def start(self):
        await self.storage.open()
        await self.signal_ledger.start()
        saved = await self.storage.load_config()
        if "pricing" in saved:
            self.settings.pricing = type(self.settings.pricing).model_validate(saved["pricing"])
            self.registry.configure("positive_ev", saved["pricing"])
        if "risk" in saved:
            self.settings.risk = type(self.settings.risk).model_validate(saved["risk"])
            self.risk.config = self.settings.risk
        await self.oms.recover()
        accounting = saved.get("accounting", {})
        self.risk.settled = set(accounting.get("settled_order_ids", []))
        self.settlements = accounting.get("settlements", {})
        if accounting.get("daily_date") == utcnow().date().isoformat():
            self.risk.daily_realized = Decimal(accounting.get("daily_realized", "0"))
        await self.native.start()
        if saved.get("kill_switch", False) or saved.get("unclean_shutdown", False):
            self.risk.halt("Persisted emergency stop; reset explicitly")
        await self.storage.save_config("unclean_shutdown", True)
        self.risk.auto_trade = self.settings.auto_trade and not self.risk.kill_switch and self.settings.mode in {"demo", "paper"}
        from .plugins.external import register_external
        register_external(self.registry, self)
        self.registry.discover()
        for name, config in saved.get("plugin_configs", {}).items():
            if name in self.registry.records:
                self.registry.configure(name, config)
        self._restore_plugin_toggles(saved.get("plugin_enabled", {}))
        # Restored flags cannot revive a missing/cyclic dependency tree.
        startup_order = self.registry.compatible_startup_order()
        self.tasks = [
            asyncio.create_task(self._process(), name="normalization-strategy"),
            asyncio.create_task(self._risk_loop(), name="risk-routing"),
            asyncio.create_task(self._audit(), name="audit-enqueue"),
            asyncio.create_task(self._dashboard_events(), name="dashboard-observer"),
            asyncio.create_task(self._maintenance(), name="freshness-fill-maintenance"),
            asyncio.create_task(self._health_loop(), name="plugin-health-refresh"),
            asyncio.create_task(self._storage_maintenance(), name="storage-retention"),
            asyncio.create_task(self._settlement_loop(), name="settlement-accounting"),
            asyncio.create_task(self._signal_loop(), name="signal-ledger"),
            asyncio.create_task(self.native.maintain(), name="native-reconciliation"),
            asyncio.create_task(self.native.maintain_settlements(), name="native-settlement"),
        ]
        for task in self.tasks:
            task.add_done_callback(self._task_done)
        for name in startup_order:
            record = self.registry.records[name]
            if record.enabled:
                await self.registry.start(name, self.context(name))
        mode_label = ("SYNTHETIC mode active" if self.settings.mode in {"demo", "paper"} and self.settings.sample_feed_enabled
                      else "PAPER live-input mode" if self.settings.mode == "paper"
                      else f"Native {self.settings.mode} mode")
        await self.notice("info", mode_label + ". Automatic trading is " +
                          ("enabled for paper fills." if self.risk.auto_trade else "disabled."), "engine")

    async def stop(self):
        self.stopping = True
        self.risk.auto_trade = False
        await self.oms.cancel_all()
        await self.native.manager.cancel_all()
        for name in list(self.registry.records):
            await self.registry.stop(name)
        # Stop producers first, then finish pricing decisions and their ledger records.
        clean = True
        for queue, worker in [(self.process_queue, "normalization-strategy"),
                              (self.risk_queue, "risk-routing"),
                              (self.settlement_queue, "settlement-accounting"),
                              (self.signal_queue, "signal-ledger")]:
            clean = await self._drain_worker(queue, worker) and clean
        if clean and self.signal_ledger.started:
            await self.signal_ledger.close_all(reason="shutdown")
        # A dead observer must not hang shutdown or mark an incomplete audit clean.
        clean = await self._drain_worker(self.audit_queue, "audit-enqueue") and clean
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if clean and not self.storage.failure:
            await self.storage.save_config("unclean_shutdown", False)
        try:
            await self.storage.close()
        finally:
            self.native.close()

    async def _drain_worker(self, queue, worker):
        if not any(task.get_name() == worker and not task.done() for task in self.tasks):
            if queue.empty():
                return True
            self.risk.halt(f"Undrained worker on shutdown: {worker}")
            return False
        try:
            await asyncio.wait_for(queue.join(), timeout=5)
            return True
        except TimeoutError:
            self.risk.halt(f"Worker failed to drain on shutdown: {worker}")
            return False

    def _persist_halt(self, reason):
        if not self.storage.connection or self.storage.failure or self.halt_persisted:
            return
        self.halt_persisted = True
        try:
            self.storage.enqueue_config("kill_switch", True)
        except asyncio.QueueFull:
            # The durable unclean_shutdown marker also blocks unattended restart.
            self.storage.failure = "Emergency-stop persistence queue full"

    def _task_done(self, task):
        if not self.stopping and not task.cancelled():
            exception = task.exception()
            self.risk.halt(f"Critical worker stopped: {task.get_name()} ({type(exception).__name__})")

    async def notice(self, level, message, component):
        await self.bus.publish(Envelope(schema_name="system.notice", source=component,
                                       payload=Notice(level=level, message=message, component=component)))

    async def _audit(self):
        while True:
            event = await self.audit_queue.get()
            try:
                await self.storage.event(event)
            except Exception:
                self.risk.halt("Audit persistence unavailable")
            finally:
                self.audit_queue.task_done()

    async def _signal_loop(self):
        while True:
            event = await self.signal_queue.get()
            try:
                if event.schema_name == "signal.result":
                    payload = event.payload
                    await self.signal_ledger.apply_result(
                        payload.market, payload.result, payload.provenance,
                        refund_fees_returned=payload.refund_fees_returned)
                else:
                    await self.signal_ledger.process(event.payload)
            finally:
                self.signal_queue.task_done()

    async def _record_signal(self, market, status, reason, candidates=()):
        identity = market.identity()
        now_monotonic = time.monotonic()
        input_presence = (bool(self.quotes.get(market.id)), bool(self.books.get(market.id)))
        estimate = self.reference_estimates.get(market.id)
        estimate_fingerprint = (tuple(sorted(estimate["probabilities"].items())) if estimate else None)
        previous = self._signal_audit_state.get(market.id)
        if (status in {"NO SIGNAL", "RESEARCH ONLY"} and previous is not None
                and previous[0] == identity and previous[1] == status
                and now_monotonic - previous[2] < 60
                and previous[3] == input_presence
                and previous[4] is self.settings.pricing
                and self._signal_audit_estimate_fingerprints.get(market.id) == estimate_fingerprint):
            self.metrics.counters["coalesced_no_signal_evaluations"] += 1
            return
        self._signal_audit_state[market.id] = (identity, status, now_monotonic, input_presence,
                                               self.settings.pricing)
        self._signal_audit_estimate_fingerprints[market.id] = estimate_fingerprint
        self._signal_audit_state.move_to_end(market.id)
        while len(self._signal_audit_state) > 10000:
            old_market_id, _ = self._signal_audit_state.popitem(last=False)
            self._signal_audit_estimate_fingerprints.pop(old_market_id, None)
        config = market_config(self.settings.pricing, market)
        eligible = tuple(c.id for c in candidates
                         if c.execution_eligible and c.ev_per_contract >= config.min_ev and c.roi >= config.min_roi)
        quotes = list(self.quotes.get(market.id, {}).values())
        books = list(self.books.get(market.id, {}).values())
        synthetic = (any(c.synthetic for c in candidates) if candidates else
                     not any(not item.synthetic for item in [*quotes, *books]))
        fees = {c.id: book.fee for c in candidates
                if (book := self.book(market.id, c.venue, c.outcome)) is not None}
        try:
            actual_model = (candidates[0].primary_model or config.devig_method) if candidates else resolved_reference_policy(
                self.settings.pricing, market, utcnow())[0].devig_method
        except NoSignal:
            actual_model = "no_applicable_profile"
        observation = SignalObservation(
            market=market, candidates=tuple(candidates), status=status, reason=reason,
            qualified_candidate_ids=eligible, model=actual_model,
            pricing_thresholds={"min_ev": config.min_ev, "min_roi": config.min_roi},
            fee_schedules=fees, synthetic=synthetic,
            reference_quotes=tuple(quotes), pricing_policy=self.settings.pricing.model_dump(mode="json"),
            strategies=tuple(name for name, record in self.registry.records.items()
                             if isinstance(record.plugin, StrategyPlugin)),
        )
        await self.bus.publish(Envelope(schema_name="signal.observation", source="strategy-engine", payload=observation))

    async def record_signal_result(self, payload: SignalResultImport):
        # Complete all earlier observations before matching a supplied result.
        await self.process_queue.join()
        await self.signal_queue.join()
        await self.signal_ledger.apply_result(
            payload.market, payload.result, payload.provenance,
            refund_fees_returned=payload.refund_fees_returned)
        # The independent audit and ledger consumers see the same typed result.
        # Ledger result application is idempotent; this replay cannot double-grade.
        await self.bus.publish(Envelope(schema_name="signal.result", source=payload.provenance.source, payload=payload))

    async def _dashboard_events(self):
        while True:
            event = await self.dashboard_queue.get()
            self.events.appendleft({
                "id": event.id, "type": event.schema_name, "timestamp": event.emitted_at.isoformat(),
                "source": event.source, **event.payload.model_dump(mode="json"),
            })
            self.dashboard_queue.task_done()

    async def _process(self):
        while True:
            event = await self.process_queue.get()
            started = time.perf_counter_ns()
            try:
                await self.process_event(event)
            except Exception as exc:
                self.risk.halt(f"Processing failure: {type(exc).__name__}")
                await self.notice("error", f"Processing failed: {type(exc).__name__}", "engine")
            finally:
                self.metrics.observe("internal_processing_ms", (time.perf_counter_ns() - started) / 1e6)
                self.process_queue.task_done()

    async def process_event(self, event):
        payload = event.payload
        self.latest_typed[event.schema_name] = payload
        market_id = getattr(payload, "market_id", None)
        if isinstance(payload, Market):
            market_id = payload.id
            self.matcher.register(payload)
            self.markets[market_id] = payload
        elif isinstance(payload, Quote):
            if market_id not in self.markets:
                self.matcher.quarantine.append({"reason": "Unknown market", "market_id": market_id})
                return
            key = (payload.provider, payload.bookmaker)
            old = self.quotes.setdefault(market_id, {}).get(key)
            if payload.source_time and payload.source_time > utcnow():
                self.matcher.quarantine.append({"reason": "Future source clock", "market_id": market_id, "provider": payload.provider})
                return
            # Only a documented outcome observation may replace the legacy
            # source clock. Transport receipt never promotes an old quote.
            old_clock = (old.source_observed_at if old.observation_verified else old.source_time) if old else None
            # Source-only trusted estimates have no quote-bound observation
            # clock. Their carried oldest-side price-change time is not a
            # same-observation watermark for a later one-sided stream delta.
            # Keep these latches unchanged for reviewed production markets.
            market = self.markets[market_id]
            trusted_research = (market.extensions.get("research_only") is True
                and market_config(self.settings.pricing, market).reference_quality_mode == "trusted_provider")
            strict = self.settings.pricing.reference_profile_mode == "strict" and not trusted_research
            source_key = (market_id, *key)
            watermark = self.quote_observation_watermarks.get(source_key)
            if strict and watermark is not None:
                old_clock = watermark
            flags = ("observation_verified", "observation_complete", "market_status_verified")
            downgraded = strict and old and any(getattr(old, flag) and not getattr(payload, flag) for flag in flags)
            new_clock = payload.source_observed_at if payload.observation_verified else payload.source_time
            if new_clock and new_clock > utcnow():
                self.matcher.quarantine.append({"reason": "Future observation clock", "market_id": market_id})
                return
            if old and old_clock and new_clock and new_clock < old_clock and payload.status == "active" and not downgraded:
                self.metrics.counters["out_of_order_quotes"] += 1
                return
            if old and old.status != "active" and payload.status == "active" and (not new_clock or old_clock and new_clock <= old_clock):
                self.metrics.counters["out_of_order_quotes"] += 1
                return
            if old and payload.status != "active" and old_clock and new_clock and new_clock < old_clock:
                payload = old.model_copy(update={"status": payload.status, "received_at": payload.received_at})
            if downgraded:
                self.quote_verification_latches[source_key] = "verification_downgrade_requires_new_observation"
            if (strict and old and old_clock
                    and new_clock == old_clock and payload.status == "active"
                    and payload.decimal_odds != old.decimal_odds):
                # Same-observation contradictions stay research-only until a
                # strictly newer complete observation resolves them. Receipt
                # order and repeated packets cannot clear this condition.
                self.quote_verification_latches[source_key] = "conflicting_same_observation"
                self.metrics.counters["conflicting_quote_observations"] += 1
            if strict and source_key in self.quote_verification_latches:
                recovered = (payload.status == "active" and all(getattr(payload, flag) for flag in flags)
                    and payload.source_observed_at is not None
                    and (old_clock is None or payload.source_observed_at > old_clock))
                if recovered:
                    del self.quote_verification_latches[source_key]
                else:
                    payload = payload.model_copy(update={"observation_complete": False,
                        "verification_reason": self.quote_verification_latches[source_key]})
            if payload.observation_verified and payload.source_observed_at is not None:
                self.quote_observation_watermarks[source_key] = max(
                    payload.source_observed_at, watermark or payload.source_observed_at)
            self.quotes[market_id][key] = payload
        elif isinstance(payload, NativeBook):
            market_id = payload.instrument.canonical_market_id
            market = self.markets.get(market_id)
            if market is None or market.identity() != payload.market_fingerprint:
                self.risk.halt("Native book canonical mapping changed")
                return
            key = (market_id, payload.instrument.venue, payload.instrument.outcome)
            old = self.native.books.get(key)
            if old and old.revision > payload.revision:
                return
            self.native.books[key] = payload
        elif isinstance(payload, OrderBook):
            if market_id not in self.markets:
                self.matcher.quarantine.append({"reason": "Unknown book market", "market_id": market_id})
                return
            key = (payload.venue, payload.outcome)
            previous = self.books.setdefault(market_id, {}).get(key)
            if payload.source_time and payload.source_time > utcnow():
                self.matcher.quarantine.append({"reason": "Future order-book source clock", "market_id": market_id})
                return
            if previous and payload.sequence < previous.sequence:
                self.metrics.counters["out_of_order_books"] += 1
                return
            self.books[market_id][key] = payload
        if market_id and market_id in self.markets:
            await self.recalculate(market_id, event)
            calculated_at = utcnow()
            self.metrics.observe("bus_publish_to_calculation_ms",
                                 (calculated_at - event.emitted_at).total_seconds() * 1000)
            received = getattr(payload, "received_at", None)
            # Paper inputs can republish a retained quote on a heartbeat with
            # its *original* data receipt clock. That is quote age, not a
            # 30-second delay in the engine's processing queue.
            if received and event.source not in {"parlay_paper_input", "oddize_paper_input"}:
                self.metrics.observe("receipt_to_calculation_ms",
                                     (calculated_at - received).total_seconds() * 1000)
        # Non-odds strategies can also subscribe independently to typed bus events in start().

    def refresh_reference_estimate(self, market_id):
        """Refresh the read-only fair-price display without routing an order."""
        market = self.markets[market_id]
        if market.extensions.get("paper_only"):
            return
        quotes = list(self.quotes.get(market_id, {}).values())
        try:
            fair = fair_value(market, quotes, self.settings.pricing)
            from .core.native_pricing import research_native_estimate
            native_research, native_rejections = [], []
            matching_native = [book for key, book in self.native.books.items() if key[0] == market_id]
            for native_book in matching_native:
                try:
                    native_research.append(research_native_estimate(
                        market, fair, native_book, self.settings.pricing))
                except NoSignal as exc:
                    native_rejections.append(str(exc))
            self.reference_fairs[market_id] = fair
            self.reference_estimates[market_id] = {
                "market_id": market_id, "event_name": market.event_name,
                "sport": market.sport, "league": market.league,
                "market_type": market.market_type, "player": market.player,
                "statistic": market.statistic,
                "line": str(market.line) if market.line is not None else None,
                "start_time": market.start_time.isoformat(),
                "probabilities": {k: str(v) for k, v in fair.probabilities.items()},
                "references": list(fair.references),
                "model": fair.model, "reference_profile_id": fair.reference_profile_id,
                "reference_disagreement_probability": str(fair.disagreement),
                "quality_mode": market_config(self.settings.pricing, market).reference_quality_mode,
                "execution_eligible": fair.execution_eligible and not market.extensions.get("research_only"),
                "native_market_matched": not market.extensions.get("research_only") and bool(matching_native),
                "native_research_estimates": native_research,
                "limitations": list(fair.ineligibility_reasons) + ([
                    "Source identity only; no reviewed native market mapping"] if market.extensions.get("research_only") else [])
                    + list(dict.fromkeys(native_rejections)),
                "calculated_at": fair.calculated_at.isoformat(), "synthetic": fair.synthetic,
                "status": ("PROVISIONAL POSITIVE EV" if any(
                    estimate["positive_estimate"] for estimate in native_research)
                    else "REFERENCE ESTIMATE")}
        except NoSignal as exc:
            self._archive_research(market_id, str(exc))
            self.reference_estimates.pop(market_id, None)
            self.reference_fairs.pop(market_id, None)

    def _archive_research(self, market_id, reason):
        """Retain the last evaluated evidence with its original clocks, never as a live offer."""
        previous = self._last_current_research.pop(market_id, None)
        if previous is None:
            return
        self.research_history.append({**previous, "historical": True,
            "status": "EXPIRED RESEARCH", "expiration_reason": reason,
            "native_research_stage": "references_expired",
            "native_discovery_reason": "Sportsbook references expired; any earlier native depth and estimated EV are historical only",
            "expired_at": utcnow().isoformat(), "execution_eligible": False,
            "native_market_matched": False,
            "native_research_estimates": [{**offer, "historical": True,
                "execution_eligible": False} for offer in previous["native_research_estimates"]]})

    def public_reference_estimates(self):
        """Use current monitored depth without querying a venue or routing orders."""
        result = []
        for market_id, row in self.reference_estimates.items():
            market, fair = self.markets.get(market_id), self.reference_fairs.get(market_id)
            if market and fair and market_config(self.settings.pricing, market).reference_quality_mode == "trusted_provider":
                try:
                    validate_fair_references(market, fair, self.settings.pricing, utcnow())
                except NoSignal as exc:
                    self._archive_research(market_id, str(exc))
                    continue  # Keep the historical audit record, not a stale live card.
            comparisons = self.native.research_comparisons(market, fair) if market and fair else []
            # A discovery lead's prior calculation can outlive its native
            # stream. Keep reviewed native mappings on their existing path.
            research_lead = bool(market and market.extensions.get("research_only"))
            all_estimates = (comparisons if research_lead else
                             [*row.get("native_research_estimates", []), *comparisons])
            stage, discovery_reason = self.native.research_discovery_status(market, all_estimates)
            displayed = {**row, "native_market_matched": bool(comparisons) if research_lead else
                           bool(comparisons) or row["native_market_matched"],
                           "native_research_estimates": all_estimates,
                           "native_research_stage": stage,
                           "native_discovery_reason": discovery_reason,
                           "native_identity_rejections": (self.native.research_identity_rejections(market)
                               if hasattr(self.native, "research_identity_rejections") else []),
                           "native_match_count": len({(item["venue"], item["native_market_id"])
                                                      for item in comparisons}),
                           "status": ("PROVISIONAL POSITIVE EV" if any(
                               item.get("positive_after_costs") or item.get("positive_estimate")
                               for item in all_estimates) else
                               "REFERENCE ESTIMATE" if research_lead else row["status"])}
            if row["quality_mode"] == "trusted_provider":
                self._last_current_research[market_id] = displayed
            result.append(displayed)
        return result

    async def recalculate(self, market_id, event=None):
        market = self.markets[market_id]
        await self.native.recalculate(market_id, event)
        self.schedule_expiration(market_id)
        for key in [k for k, v in self.opportunities.items() if v.market_id == market_id]:
            del self.opportunities[key]
        quotes = list(self.quotes.get(market_id, {}).values())
        books = list(self.books.get(market_id, {}).values())
        self.refresh_reference_estimate(market_id)
        if not any(r.enabled and isinstance(r.plugin, StrategyPlugin) for r in self.registry.records.values()):
            self.signals[market_id] = {"status": "NO SIGNAL", "reason": "Strategy disabled"}
            await self._record_signal(market, **self.signals[market_id])
            return
        view = {"market": market, "quotes": quotes, "books": books, "config": self.settings.pricing,
                "typed_events": self.latest_typed}
        if market_id in self.settlements:
            self.signals[market_id] = {"status": "NO SIGNAL", "reason": "Market settled"}
            await self._record_signal(market, **self.signals[market_id])
            return
        generated, reasons = [], []
        for name, strategy in self.registry.records.items():
            if strategy.enabled and isinstance(strategy.plugin, StrategyPlugin) and not isinstance(strategy.plugin, NativePositiveEV):
                manifest = strategy.plugin.manifest
                if not any(s in manifest.sports for s in (market.sport, "*", "discoverable")):
                    continue
                if not any(m in manifest.markets for m in (market.market_type, "*", "discoverable")):
                    continue
                required = {schema.split("@")[0] for schema in manifest.input_schemas}
                available = set(self.latest_typed)
                if market:
                    available.add("sports.market")
                if quotes:
                    available.add("odds.quote")
                if books:
                    available.add("exchange.book")
                if required - available:
                    reasons.append(f"{name}: required input unavailable")
                    continue
                try:
                    generated.extend(await strategy.plugin.evaluate(event, view))
                except NoSignal as exc:
                    reasons.append(str(exc))
                except Exception as exc:
                    strategy.enabled = False
                    strategy.health = {"status": "error", "message": f"Strategy stopped: {type(exc).__name__}"}
                    self.risk.halt("Strategy failure")
        if not generated:
            reference_only = market_id in self.reference_estimates
            self.signals[market_id] = {"status": "RESEARCH ONLY" if reference_only else "NO SIGNAL",
                                       "reason": ("Fair sportsbook probability available; no matched executable native offer"
                                                  if reference_only else "; ".join(reasons) if reasons else "No executable synchronized book")}
            await self._record_signal(market, **self.signals[market_id])
            return
        config = market_config(self.settings.pricing, market)
        eligible = [c for c in generated if c.execution_eligible and c.ev_per_contract >= config.min_ev and c.roi >= config.min_roi]
        research_only = all(not c.reference_inputs_eligible for c in generated)
        self.signals[market_id] = {"status": "POSITIVE EV" if eligible else "RESEARCH ONLY" if research_only else "NEGATIVE EV",
                                  "reason": "; ".join(dict.fromkeys(r for c in generated for r in c.ineligibility_reasons)) if research_only else "Fee-adjusted executable offers",
                                  "best_ev": str(max(c.ev_per_contract for c in generated))}
        await self._record_signal(market, self.signals[market_id]["status"],
                                  self.signals[market_id]["reason"], generated)
        for candidate in generated:
            key = f"{candidate.strategy}:{market_id}:{candidate.venue}:{candidate.outcome}"
            self.opportunities[key] = candidate
            if candidate in eligible and self.risk.auto_trade and not self.risk.kill_switch:
                await self.bus.publish(Envelope(schema_name="trade.candidate", source=candidate.strategy, payload=candidate))
        timestamp = utcnow()
        history = self.history.setdefault(market_id, deque(maxlen=180))
        if not history or (timestamp - datetime.fromisoformat(history[-1]["time"])).total_seconds() > 1:
            history.append({"time": timestamp.isoformat(), "fair": str(generated[0].fair_probability),
                            "price": str(generated[0].price)})

    def book(self, market_id, venue, outcome):
        return self.books.get(market_id, {}).get((venue, outcome))

    def schedule_expiration(self, market_id):
        config = market_config(self.settings.pricing, self.markets[market_id])
        now = utcnow().timestamp()
        times = [q.source_time.timestamp() + config.max_quote_age_ms / 1000
                 for q in self.quotes.get(market_id, {}).values() if q.source_time and q.status == "active"]
        if config.reference_profile_mode == "strict":
            market = self.markets[market_id]
            if config.reference_quality_mode == "trusted_provider":
                times = [q.received_at.timestamp() + config.trusted_max_delivery_age_ms / 1000
                         for q in self.quotes.get(market_id, {}).values()
                         if q.status in {"active", "unknown"}]
                times += [q.source_scope_poll_at.timestamp() + config.trusted_max_indicator_age_ms / 1000
                          for q in self.quotes.get(market_id, {}).values()
                          if q.source_scope_poll_at and q.status in {"active", "unknown"}]
            else:
                times = [q.source_observed_at.timestamp() + float(rule.ttl_seconds)
                     for q in self.quotes.get(market_id, {}).values()
                     if q.source_observed_at and q.observation_verified and q.status == "active"
                     and (rule := freshness_rule(config, market, q.provider, q.bookmaker)) is not None]
            # A pregame profile must expire at the scheduled start even when
            # the provider never sends a status update or price change.
            times.append(market.start_time.timestamp())
        times += [b.source_time.timestamp() + config.max_book_age_ms / 1000
                  for b in self.books.get(market_id, {}).values() if b.source_time and b.status == "active"]
        times += [b.source_time.timestamp() + config.max_book_age_ms / 1000
                  for key,b in self.native.books.items() if key[0] == market_id and b.source_time and b.status == "active"]
        future = [t for t in times if t > now]
        revision = self.expiration_revisions.get(market_id, 0) + 1
        self.expiration_revisions[market_id] = revision
        if future:
            heapq.heappush(self.expirations, (min(future) + .001, revision, market_id))

    def revalidate(self, candidate_or_order):
        c = candidate_or_order
        if self.settings.mode in {"demo", "paper"} and not self.registry.records["paper_execution"].enabled:
            raise RiskRejected("Paper execution plugin is disabled")
        if c.market_id in self.settlements:
            raise RiskRejected("Market settled")
        market = self.markets.get(c.market_id)
        strategy = self.registry.records.get(c.strategy)
        if market is None or strategy is None or not strategy.enabled or not isinstance(strategy.plugin, StrategyPlugin):
            raise RiskRejected("Market or strategy unavailable")
        if isinstance(c, Order):
            if c.market_snapshot is not None and c.market_snapshot.identity() != market.identity():
                raise RiskRejected("Market identity changed after order reservation")
            self.risk.check_position_conflicts(c, market, exclude_order_id=c.id)
        book = self.book(c.market_id, c.venue, c.outcome)
        if book is None:
            raise RiskRejected("Executable book unavailable")
        config = market_config(self.settings.pricing, market)
        if (book.market_id != market.id or book.venue != c.venue or book.outcome != c.outcome or
                book.outcome not in market.outcomes or not book.synchronized or book.status != "active" or
                not book.fresh(config.max_book_age_ms) or not book.asks or book.asks[0].price > c.price or
                market.status in {"closed", "suspended"}):
            raise RiskRejected("Fresh executable market validation failed")
        try:
            current = strategy.plugin.revalidate(c, {
                "market": market, "quotes": list(self.quotes.get(market.id, {}).values()),
                "book": book, "config": self.settings.pricing, "typed_events": self.latest_typed,
                "strategy_config": strategy.config})
        except (NoSignal, NotImplementedError) as exc:
            raise RiskRejected(str(exc)) from exc
        if (current.strategy, current.market_id, current.venue, current.outcome) != (c.strategy, c.market_id, c.venue, c.outcome):
            raise RiskRejected("Strategy changed approved trade identity")
        if not current.execution_eligible or self.settings.mode == "production" and not current.live_execution_allowed:
            raise RiskRejected("Reference profile is not eligible for this execution mode")
        if current.ev_per_contract < config.min_ev or current.roi < config.min_roi:
            raise RiskRejected("Signal no longer meets thresholds")
        if current.price > c.price:
            raise RiskRejected("Executable price moved above order limit")
        if isinstance(c, Candidate):
            if book.sequence != c.book_sequence:
                raise RiskRejected("Book changed; wait for a newly calculated candidate")
            # Recheck fee rounding at the actually affordable quantity.
            if self.settings.risk.max_order_stake is None:
                raise RiskRejected("Uncapped account sizing requires the native quarter-Kelly route")
            qty = min(c.max_quantity, int(self.settings.risk.max_order_stake / c.price))
            if qty <= 0:
                raise RiskRejected("No affordable quantity")
        # Standing limits retain their approved price even when the new best ask
        # changes. Never approve a fill by evaluating only the cheaper current ask.
        from .core.pricing import validate_candidate_size
        try:
            validate_candidate_size(current, book.fee, c.price, 1, config)
        except NoSignal as exc:
            raise RiskRejected(str(exc)) from exc
        if isinstance(c, Order):
            self.risk.check_reserved_limits(c, book)
        return current.model_copy(update={"id": c.id}) if isinstance(c, Candidate) else current

    async def _risk_loop(self):
        while True:
            event = await self.risk_queue.get()
            try:
                c = event.payload
                if isinstance(c, NativeCandidate):
                    try:
                        await self.native.execute(c)
                    finally:
                        cap = self.risk.config.max_native_order_attempts
                        if (cap is not None and len(self.risk.native_orders) >= cap
                                and self.risk.auto_trade):
                            self.risk.auto_trade = False
                            await self.notice("info", "Native order attempt limit reached; automatic trading paused", "operator")
                else:
                    await self.oms.execute(c, self.markets[c.market_id], self.book(c.market_id, c.venue, c.outcome), self.settings.mode)
                self.metrics.counters["orders_routed"] += 1
            except RiskRejected as exc:
                self.metrics.counters["risk_rejections"] += 1
                self.metrics.counters[f"risk:{str(exc)}"] += 1
            except Exception as exc:
                self.risk.halt(f"Routing failure: {type(exc).__name__}")
            finally:
                self.risk_queue.task_done()

    async def _maintenance(self):
        while True:
            await asyncio.sleep(.5)
            now = utcnow().timestamp()
            while self.expirations and self.expirations[0][0] <= now:
                _, revision, market_id = heapq.heappop(self.expirations)
                if self.expiration_revisions.get(market_id) == revision:
                    await self.recalculate(market_id)
            await self.oms.tick()
            if self.risk.kill_switch:
                await self.oms.cancel_all()
            positive = {k for k, c in self.opportunities.items()
                        if c.execution_eligible and c.ev_per_contract >= self.settings.pricing.min_ev}
            self.metrics.update_opportunities(positive, utcnow())

    async def _health_loop(self):
        """Health reporting must not wait behind pricing or order maintenance."""
        while True:
            await asyncio.sleep(1)
            for name, record in self.registry.records.items():
                if record.enabled:
                    try:
                        record.health = await asyncio.wait_for(record.plugin.health_check(), timeout=2)
                    except Exception as exc:
                        record.health = {"status": "error", "message": type(exc).__name__}

    async def _storage_maintenance(self):
        # Raw tick cleanup uses a separate observer connection and small durable
        # delete batches. It never runs in the pricing or order-routing loop.
        while True:
            await asyncio.sleep(300)
            await self.storage.retention_pass(max_batches=24)

    async def _settlement_loop(self):
        while True:
            event = await self.settlement_queue.get()
            try:
                await self.apply_settlement(event.payload)
            finally:
                self.settlement_queue.task_done()

    async def apply_settlement(self, result: SettlementResult):
        market = self.markets.get(result.market_id)
        if not market or result.market_id in self.settlements:
            return
        if set(result.outcome_payouts) != set(market.outcomes) or not result.evidence.strip():
            raise ValueError("Settlement must cover the exact opposing outcome set and include evidence")
        if result.settled_at.tzinfo is None or result.settled_at > utcnow():
            raise ValueError("Settlement must have an aware timestamp no later than the current time")
        if not result.refund and sum(result.outcome_payouts.values()) != Decimal(1):
            raise ValueError("Binary settlement must distribute exactly one winning payout")
        if not result.synthetic:
            raise ValueError("Native settlement reconciliation is not enabled")
        async with self.oms.lock:
            if result.market_id in self.settlements:
                return
            relevant = [o for o in self.risk.orders.values() if o.market_id == result.market_id]
            if any(o.synthetic != result.synthetic for o in relevant):
                raise ValueError("Simulated and real settlements cannot be mixed")
            if any(o.status.value == "unknown" for o in relevant):
                raise ValueError("Reconcile unknown orders before settlement")
            for order in relevant:
                await self.oms._cancel(order)
            relevant = [self.risk.orders[o.id] for o in relevant]
            if any(o.status.value in {"unknown", "cancel_pending"} for o in relevant):
                raise ValueError("Confirm cancellations before settlement")
            realized = Decimal(0)
            settled_ids = set(self.risk.settled)
            self.risk.roll_day()
            for order in relevant:
                cost = order.average_fill_price * order.filled_quantity + order.fees
                payout = cost if result.refund else order.filled_quantity * result.outcome_payouts[order.outcome]
                realized += payout - cost
                settled_ids.add(order.id)
            settlements = {**self.settlements, result.market_id: {**result.model_dump(mode="json"), "realized_pnl": str(realized)}}
            daily_pnl = self.risk.daily_realized + realized
            await self.storage.save_config("accounting", {
                "daily_date": self.risk.daily_date.isoformat(),
                "daily_realized": str(daily_pnl-self.native.manager.realized_today()),
                "settled_order_ids": sorted(settled_ids-set(self.risk.native_orders)), "settlements": settlements,
            })
            self.risk.settled, self.settlements, self.risk.daily_realized = settled_ids, settlements, daily_pnl
        if self.signal_ledger.started:
            await self.record_signal_result(SignalResultImport(
                market=market, result=result,
                provenance=ResultProvenance(
                    source="paper_operator", reference=result.evidence,
                    verified_by="local_paper_operator", verified_at=utcnow(),
                    verification="canonical_settlement_verified", synthetic=True),
                refund_fees_returned=True if result.refund else None,
            ))
        await self.recalculate(result.market_id)
        await self.notice("info", f"Simulated settlement recorded; realized P&L {realized:.2f}", "paper_settlement")

    async def control(self, *, auto_trade=None, kill_switch=None, expected_launch_digest=None):
        def check_native_activation():
            if self.settings.mode in {"demo", "paper"} or not (auto_trade or kill_switch is False):
                return
            report = self.native.report()
            if (expected_launch_digest is None or expected_launch_digest != report.profile_digest):
                raise ValueError("The reviewed launch scope changed; refresh and review before enabling")
            if not self.settings.live_enabled or not report.safe_to_arm:
                raise ValueError("Native execution requires explicit enablement and all launch checks passing")

        # Reject an invalid combined enable/reset before modifying or persisting
        # the emergency stop. An explicit stop still takes precedence.
        if (kill_switch is False and self.settings.mode in {"sandbox", "production"}
                and auto_trade is None):
            raise ValueError("Explicitly keep automation paused or enable it with the reviewed scope")
        if kill_switch is not True:
            check_native_activation()
        previous_stop = (self.risk.kill_switch, self.risk.kill_reason, self.halt_persisted)
        if kill_switch is True:
            self.risk.halt("Operator emergency stop")
            await self.storage.save_config("kill_switch", True)
            await self.oms.cancel_all()
            await self.native.manager.cancel_all()
        elif kill_switch is False:
            if any(o.status.value == "unknown" for o in self.risk.all_orders()):
                raise ValueError("Reconcile unknown orders before resetting")
            if self.native.manager.invalidated_settlements:
                raise ValueError("Repair invalidated native settlement accounting before resetting")
            self.risk.kill_switch, self.risk.kill_reason = False, ""
            self.halt_persisted = False
            await self.storage.save_config("kill_switch", False)
        if auto_trade is not None:
            try:
                # Persisting a reset yields to feed/reconciliation work. Recheck
                # the reviewed scope and gate after that yield; on failure keep
                # the previous emergency-stop state in memory and on disk.
                check_native_activation()
            except ValueError:
                if kill_switch is False:
                    # A new safety halt raised during the persistence await
                    # takes precedence over the state from before this request.
                    if not self.risk.kill_switch:
                        self.risk.kill_switch, self.risk.kill_reason, self.halt_persisted = previous_stop
                    await self.storage.save_config("kill_switch", self.risk.kill_switch)
                raise
            if auto_trade and self.risk.kill_switch:
                raise ValueError("Reset kill switch first")
            self.risk.auto_trade = auto_trade
            if not auto_trade:
                await self.oms.cancel_all()
                await self.native.manager.cancel_all()
        await self.notice("info", f"Automatic {self.settings.mode} trading: {self.risk.auto_trade}; kill switch: {self.risk.kill_switch}", "operator")

    def candidate_public(self, candidate):
        market = self.markets[candidate.market_id]
        config = market_config(self.settings.pricing, market)
        paper_only = market.extensions.get("paper_only", False)
        target = candidate.venue.removeprefix("parlay_paper:") if paper_only else None
        target_quote = next((quote for quote in self.quotes.get(candidate.market_id, {}).values()
            if quote.provider == "parlay_paper_input" and quote.bookmaker == target), None)
        return {**candidate.model_dump(mode="json"), "event_name": market.event_name,
                "sport": market.sport, "league": market.league, "market_type": market.market_type,
                "player": market.player, "line": str(market.line) if market.line is not None else None,
                "paper_only": paper_only, "target_bookmaker": target,
                "target_quote_age_ms": target_quote.age_ms() if target_quote else None,
                "capacity_kind": "simulated" if paper_only else "observed",
                "status": "RESEARCH ONLY" if not candidate.reference_inputs_eligible else "POSITIVE EV" if candidate.execution_eligible and candidate.ev_per_contract >= config.min_ev and candidate.roi >= config.min_roi else "NEGATIVE EV"}

    def quote_public(self, quote):
        market = self.markets.get(quote.market_id)
        config = market_config(self.settings.pricing, market) if market else self.settings.pricing
        strict = config.reference_profile_mode == "strict"
        trusted = strict and config.reference_quality_mode == "trusted_provider"
        eligible, reasons, age = ((trusted_quote_research_eligibility(config, market, quote, utcnow()) if trusted
                                  else quote_eligibility(config, market, quote, utcnow()))
                                  if market else (False, ("Unknown market",), None))
        paper_age = quote.age_ms()
        paper_usable = (bool(market and market.extensions.get("paper_only"))
            and quote.provider in {"parlay_paper_input", "oddize_paper_input"}
            and (quote.status == "active" or quote.provider == "oddize_paper_input"
                 and quote.status == "unknown") and quote.quality == 1.0
            and paper_age is not None and 0 <= paper_age <= 600000)
        observation_age = ((utcnow() - quote.source_observed_at).total_seconds() * 1000
                           if quote.observation_verified and quote.source_observed_at else None)
        poll_age = ((utcnow() - quote.source_scope_poll_at).total_seconds() * 1000
                    if quote.source_scope_poll_at else None)
        stored_age = ((utcnow() - quote.source_stored_at).total_seconds() * 1000
                      if quote.source_stored_at else None)
        return {**quote.model_dump(mode="json"), "age_ms": quote.age_ms(), "observation_age_ms": observation_age,
                "scope_poll_age_ms": poll_age, "stored_price_age_ms": stored_age,
                "delivery_age_ms": (utcnow() - quote.received_at).total_seconds() * 1000,
                "trusted_research_usable": trusted and eligible,
                "execution_freshness_reasons": reasons if strict else (),
                "paper_comparison_usable": paper_usable,
                "fresh": quote.status == "active" and (eligible and not trusted if strict else quote.fresh(config.max_quote_age_ms))}

    def market_detail(self, market_id):
        market = self.markets[market_id]
        if market.extensions.get("paper_only"):
            reference_pricing = {"status": "PAPER ASSUMPTION", "reason":
                "Fair prices are target-specific and exclude the target bookmaker. "
                "See each paper calculation below. Parlay price timestamps do not verify "
                "current offer status, settlement, or executable capacity."}
        else:
            try:
                reference_pricing = fair_value(market, list(self.quotes.get(market_id, {}).values()), self.settings.pricing).model_dump(mode="json")
            except NoSignal as exc:
                reference_pricing = {"status": "NO SIGNAL", "reason": str(exc)}
        return {
            "market": market.model_dump(mode="json"),
            "reference_pricing": reference_pricing,
            "quotes": [self.quote_public(q) for q in self.quotes.get(market_id, {}).values()],
            "books": [b.model_dump(mode="json") for b in self.books.get(market_id, {}).values()],
            "opportunities": [self.candidate_public(c) for c in self.opportunities.values() if c.market_id == market_id],
            "history": list(self.history.get(market_id, [])), "signal": self.signals.get(market_id),
        }

    def state(self):
        orders = list(self.risk.orders.values())
        filled = [o for o in orders if o.filled_quantity]
        positions = [{
            "market_id": o.market_id, "event_name": self.markets[o.market_id].event_name if o.market_id in self.markets else o.event_id,
            "venue": o.venue, "outcome": o.outcome, "quantity": o.filled_quantity,
            "average_price": str(o.average_fill_price), "cost": str(o.average_fill_price * o.filled_quantity + o.fees),
            "synthetic": o.synthetic, "status": "unsettled",
        } for o in filled if o.id not in self.risk.settled]
        metrics = self.metrics.snapshot()
        metrics.update({"fill_rate": len(filled) / len(orders) if orders else 0, "order_count": len(orders),
                        "fill_count": sum(o.filled_quantity for o in orders),
                        "realized_pnl": str(self.risk.daily_realized), "simulated": True,
                        "clv": None, "clv_status": "Requires compatible verified closing observations",
                        "bus_dropped": dict(self.bus.dropped), "bus_events": dict(self.bus.counts)})
        return {
            "mode": self.settings.mode, "auto_trade": self.risk.auto_trade,
            "live_enabled": self.settings.live_enabled,
            "kill_switch": self.risk.kill_switch, "kill_reason": self.risk.kill_reason,
            "synthetic": self.settings.mode in {"demo", "paper"},
            "sample_feed_enabled": self.settings.sample_feed_enabled,
            "started_at": self.started_at.isoformat(),
            "timestamp": utcnow().isoformat(), "plugins": self.registry.public(),
            "markets": [m.model_dump(mode="json") for m in self.markets.values()],
            "quotes": [self.quote_public(q) for values in self.quotes.values() for q in values.values()],
            "opportunities": sorted([self.candidate_public(c) for c in self.opportunities.values()], key=lambda c: Decimal(c["ev_per_contract"]), reverse=True),
            "reference_estimates": self.public_reference_estimates(),
            "research_history": list(reversed(self.research_history))[:50],
            "orders": [o.model_dump(mode="json") for o in reversed(orders[-200:])],
            "native_orders": [o.model_dump(mode="json") for o in list(self.risk.native_orders.values())[-200:]],
            "native_signals": [o.model_dump(mode="json") for o in self.native.signals.values()],
            "native_opportunities": [c.model_dump(mode="json") for c in self.native.candidates.values()],
            "positions": positions, "metrics": metrics,
            "risk": {**self.risk.config.model_dump(mode="json"), "total_exposure": str(self.risk.exposures()),
                     "daily_realized": str(self.risk.daily_realized), "position_conflicts_enforced": True},
            "pricing": self.settings.pricing.model_dump(mode="json"),
            "events": list(self.events),
            "signals": [{"market_id": k, **v} for k, v in self.signals.items()],
            "quarantine": self.matcher.quarantine[-30:] + [
                item for record in self.registry.records.values()
                if getattr(record.plugin, "mapper", None)
                for item in list(record.plugin.mapper.quarantine.values())[-30:]
            ],
            "settlements": self.settlements,
            "plugin_load_errors": self.registry.discovery_errors,
        }

