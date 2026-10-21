"""Durable native order state, isolated from the paper simulator's $1 units.

Uses the SAME central risk object and lock as paper routing. Network transports
are injected; importing or recovering this module never submits an order.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .conflicts import UNRESOLVED, reviewed_correlations, risk_family
from .models import Contract, Envelope, Market, OrderStatus, utcnow
from .native_accounting import NativeAccounting, NativeCumulativeSnapshot, NativeFill, NativeOrderIntent
from .risk import RiskRejected


class NativeOrder(Contract):
    intent: NativeOrderIntent
    candidate_id: str
    strategy: str
    account_fingerprint: str = Field(min_length=8)
    market_snapshot: Market
    market_fingerprint: str
    risk_family_id: str
    reviewed_correlation_groups: tuple[str, ...] = ()
    status: OrderStatus = OrderStatus.INTENT
    filled_quantity: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    filled_cost_usd: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    gross_payout_usd: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    fees: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    venue_order_id: str | None = None
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    reconciled_at: datetime | None = None
    reconciliation_complete: bool = False
    last_snapshot: NativeCumulativeSnapshot | None = None
    unverified_exposure_usd: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    cancel_attempts: int = Field(default=0, ge=0)
    cancel_retry_after: datetime | None = None
    reason: str = ""
    synthetic: Literal[False] = False

    @property
    def id(self): return self.intent.id
    @property
    def client_order_id(self): return self.intent.client_order_id
    @property
    def venue(self): return self.intent.instrument.venue
    @property
    def outcome(self): return self.intent.instrument.outcome
    @property
    def market_id(self): return self.market_snapshot.id
    @property
    def event_id(self): return self.market_snapshot.event_id
    @property
    def correlation_id(self): return self.event_id
    @property
    def player_id(self): return self.market_snapshot.player_id
    @property
    def quantity(self): return self.intent.quantity
    @property
    def reserved_cost(self): return max(self.intent.reserved_cost_usd, self.unverified_exposure_usd)
    @property
    def mode(self): return self.intent.mode
    @property
    def created_at(self): return self.intent.created_at

    @model_validator(mode="after")
    def exact_identity(self):
        m = self.market_snapshot
        if (self.market_id != self.intent.instrument.canonical_market_id or
                self.outcome not in m.outcomes or self.market_fingerprint != m.identity() or
                self.intent.instrument.settlement_rules_fingerprint != m.identity() or
                self.risk_family_id != risk_family(m) or
                self.reviewed_correlation_groups != reviewed_correlations(m)):
            raise ValueError("Native order canonical identity is not verified")
        if self.filled_quantity > self.quantity:
            raise ValueError("Native cumulative fill exceeds original quantity")
        return self


class NativeSettlement(Contract):
    order_id: str
    venue: str
    account_fingerprint: str
    environment: Literal["sandbox", "production"]
    market_fingerprint: str
    settlement_id: str = Field(min_length=1)
    payout_usd: Decimal = Field(ge=0, allow_inf_nan=False)
    # Actual additional settlement fees/refunds, separate from execution fees.
    additional_fee_usd: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    fee_refund_usd: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    settled_at: datetime
    evidence: str = Field(min_length=8)


class NativeOrderManager:
    def __init__(self, risk, storage, bus, metrics, *, lock, revalidate, preflight):
        self.risk, self.storage, self.bus, self.metrics = risk, storage, bus, metrics
        self.lock, self.revalidate, self.preflight = lock, revalidate, preflight
        self.adapters: dict = {}
        self.settlements: dict[str, NativeSettlement] = {}
        self.invalidated_settlements: set[str] = set()
        self._accounted_settlements: set[tuple[str, str]] = set()
        self._reconcile_reads: dict[str, asyncio.Task] = {}
        self._fill_evidence: dict[str, dict[str, NativeFill]] = {}
        self._fill_conflicts: set[str] = set()
        self.recovered = False

    async def start(self):
        await self.storage.write([
            ("CREATE TABLE IF NOT EXISTS native_orders (id TEXT PRIMARY KEY, client_order_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL)", ()),
            ("CREATE TABLE IF NOT EXISTS native_reconciliations (seq INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, data TEXT NOT NULL)", ()),
            ("CREATE TABLE IF NOT EXISTS native_settlements (order_id TEXT PRIMARY KEY, data TEXT NOT NULL)", ()),
            ("CREATE TABLE IF NOT EXISTS native_settlement_invalidations (order_id TEXT PRIMARY KEY, reason TEXT NOT NULL)", ()),
            ("CREATE TABLE IF NOT EXISTS native_fill_evidence (order_id TEXT NOT NULL, fill_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(order_id,fill_id))", ()),
            ("CREATE TABLE IF NOT EXISTS native_fill_conflicts (order_id TEXT PRIMARY KEY, reason TEXT NOT NULL)", ()),
        ], (), True)
        rows = await (await self.storage.connection.execute("SELECT data FROM native_orders")).fetchall()
        for row in rows:
            order = NativeOrder.model_validate_json(row[0])
            if order.status in UNRESOLVED:
                order = order.model_copy(update={"status": OrderStatus.UNKNOWN, "reconciliation_complete": False,
                                                "reason": "Restart requires account order reconciliation; never resend"})
                await self._save(order)
                self.risk.halt("Native orders need restart reconciliation")
            self.risk.native_orders[order.id] = order
        await self._recover_fill_evidence()
        invalid = await (await self.storage.connection.execute("SELECT order_id FROM native_settlement_invalidations")).fetchall()
        self.invalidated_settlements = {row[0] for row in invalid}
        if self.invalidated_settlements:
            self.risk.halt("Native settlement changed; reconcile accounting before resuming")
        rows = await (await self.storage.connection.execute("SELECT data FROM native_settlements")).fetchall()
        for row in rows:
            result = NativeSettlement.model_validate_json(row[0])
            self.settlements[result.order_id] = result
            if result.order_id in self.invalidated_settlements:
                self.risk.settled.discard(result.order_id)
                continue
            self.risk.settled.add(result.order_id)
            # Daily realized P&L is reconstructed from native settlements; the
            # paper accounting checkpoint contains paper-only P&L.
            accounting_key = (result.order_id, self.risk.daily_date.isoformat())
            if result.settled_at.date() == self.risk.daily_date and result.order_id in self.risk.native_orders and accounting_key not in self._accounted_settlements:
                self.risk.daily_realized += self._pnl(self.risk.native_orders[result.order_id], result)
                self._accounted_settlements.add(accounting_key)
        self.recovered = True

    def recovered_fills(self, order_id: str) -> tuple[NativeFill, ...]:
        """Exact journal evidence for adapter restoration; never synthetic fills."""
        if order_id in self._fill_conflicts:
            raise ValueError("Conflicting durable native fill evidence requires review")
        return tuple(sorted(self._fill_evidence.get(order_id, {}).values(),
                            key=lambda fill: (fill.cumulative_quantity_after or Decimal(0), fill.fill_id)))

    def _stage_fills(self, order, fills):
        staged = dict(self._fill_evidence.get(order.id, {}))
        new = []
        for raw in fills:
            fill = NativeFill.model_validate(raw)
            if fill.observed_at > utcnow() or (fill.filled_at and fill.filled_at > fill.observed_at):
                raise ValueError("Future native execution evidence")
            # Validate individual economics even when intervening executions
            # are still missing. Keep its ORIGINAL cumulative watermark in the
            # journal; removing it here permits only isolated validation.
            NativeAccounting(order.intent, order.venue_order_id).apply_fill(
                fill.model_copy(update={"cumulative_quantity_after": None}))
            watermark = fill.cumulative_quantity_after
            if watermark is not None and not fill.quantity <= watermark <= order.quantity:
                raise ValueError("Invalid native fill watermark")
            old = staged.get(fill.fill_id)
            if old:
                if old.model_dump(exclude={"observed_at"}) != fill.model_dump(exclude={"observed_at"}):
                    raise ValueError("Conflicting duplicate native fill ID")
                continue
            if watermark is not None:
                for known in staged.values():
                    end = known.cumulative_quantity_after
                    if end is not None and max(watermark-fill.quantity, end-known.quantity) < min(watermark, end):
                        raise ValueError("Native execution intervals overlap")
            staged[fill.fill_id] = fill
            new.append(fill)
        if sum((fill.quantity for fill in staged.values()), Decimal(0)) > order.quantity:
            raise ValueError("Journaled fills exceed durable order quantity")
        if sum((fill.fee_usd for fill in staged.values()), Decimal(0)) > order.intent.max_fee_usd:
            raise ValueError("Journaled fill fees exceed reserved order bound")
        return staged, new

    async def _persist_fill_evidence(self, order, fills):
        if order.id in self._fill_conflicts:
            raise ValueError("Conflicting durable native fill evidence requires review")
        staged, new = self._stage_fills(order, fills)
        if new:
            try:
                await self.storage.write([
                    ("INSERT INTO native_fill_evidence VALUES(?,?,?)", (order.id, fill.fill_id, fill.model_dump_json()))
                    for fill in new], (), True)
            except BaseException:
                self.risk.halt("Native fill evidence persistence failed")
                raise
            self._fill_evidence[order.id] = staged

    def _validate_fill_snapshot(self, order, snapshot):
        fills = self.recovered_fills(order.id)
        if not fills:
            return
        pairs = (("quantity", "cumulative_quantity"), ("cost_usd", "cumulative_cost_usd"),
                 ("gross_payout_usd", "cumulative_gross_payout_usd"), ("fee_usd", "cumulative_fee_usd"))
        totals = {source: sum((getattr(fill, source) for fill in fills), Decimal(0)) for source, _ in pairs}
        if any(totals[source] > getattr(snapshot, target) for source, target in pairs):
            raise ValueError("Journaled executions exceed aggregate financial evidence")
        if totals["quantity"] == snapshot.cumulative_quantity and any(
                totals[source] != getattr(snapshot, target) for source, target in pairs):
            raise ValueError("Complete execution journal differs from aggregate cash")
        if any(fill.cumulative_quantity_after is not None and fill.cumulative_quantity_after > snapshot.cumulative_quantity
               for fill in fills):
            raise ValueError("Journaled execution is newer than aggregate quantity")

    async def _fill_conflict(self, order, report=None):
        self.risk.halt("Conflicting native execution evidence requires review")
        exposure = order.reserved_cost
        if report:
            exposure = max(exposure, sum((fill.cost_usd+fill.fee_usd for fill in report.fills), Decimal(0)))
        updated = order.model_copy(update={"status": OrderStatus.UNKNOWN, "reconciliation_complete": False,
            "reason": "Conflicting durable native fill evidence", "unverified_exposure_usd": exposure})
        statements = [
            ("INSERT OR REPLACE INTO native_fill_conflicts VALUES(?,?)", (order.id, updated.reason)),
            ("UPDATE native_orders SET status=?,data=? WHERE id=?", (updated.status.value,updated.model_dump_json(),order.id)),
            ("INSERT OR REPLACE INTO native_settlement_invalidations SELECT ?,? WHERE EXISTS(SELECT 1 FROM native_settlements WHERE order_id=?)",
             (order.id,updated.reason,order.id)),
        ]
        if report is not None:
            statements.append(("INSERT INTO native_reconciliations VALUES(NULL,?,?)", (order.id,report.model_dump_json())))
        await self.storage.write(statements, (), True)
        self._fill_conflicts.add(order.id)
        self.risk.native_orders[order.id] = updated
        if order.id in self.risk.settled:
            self._invalidate_settlement_memory(order)
        await self.bus.publish(Envelope(schema_name="native.order",source="native_oms",payload=updated))
        return updated

    async def _recover_fill_evidence(self):
        self._fill_evidence = {}
        self._fill_conflicts = {row[0] for row in await (await self.storage.connection.execute(
            "SELECT order_id FROM native_fill_conflicts")).fetchall()}
        rows = await (await self.storage.connection.execute("SELECT order_id,data FROM native_fill_evidence")).fetchall()
        for order_id, data in rows:
            order = self.risk.native_orders.get(order_id)
            if order is None:
                self.risk.halt("Native fill journal has no durable order binding")
                raise ValueError("Orphaned native fill journal")
            try:
                staged, _ = self._stage_fills(order, [NativeFill.model_validate_json(data)])
                self._fill_evidence[order_id] = staged
            except ValueError:
                await self._fill_conflict(order)
        # Backward-compatible migration: older versions stored exact fills only
        # inside complete reconciliation reports. Preserve them before the
        # adapters reconnect; incomplete reports now use the same journal.
        rows = await (await self.storage.connection.execute("SELECT order_id,data FROM native_reconciliations ORDER BY seq")).fetchall()
        for order_id, data in rows:
            if order_id in self._fill_conflicts:
                continue
            raw = json.loads(data)
            if not raw.get("fills"):
                continue
            order = self.risk.native_orders.get(order_id)
            if order is None:
                self.risk.halt("Native reconciliation has no durable order binding")
                raise ValueError("Orphaned native reconciliation")
            try:
                if raw.get("native_order_id") != order.venue_order_id or raw.get("client_order_id") != order.client_order_id:
                    raise ValueError("Historical fill identity mismatch")
                await self._persist_fill_evidence(order, raw["fills"])
            except ValueError:
                await self._fill_conflict(order)
        for order_id in set(self._fill_evidence) | self._fill_conflicts:
            order = self.risk.native_orders[order_id]
            if order_id in self._fill_conflicts:
                self.risk.halt("Conflicting durable native fill evidence requires review")
                continue
            # A crash can happen after exact fill journaling and before its
            # aggregate order update. Never keep a stale terminal/settled flag.
            accounting = NativeAccounting(order.intent, order.venue_order_id)
            try:
                if order.last_snapshot is None:
                    raise ValueError("Journaled executions require a fresh aggregate")
                accounting.apply_cumulative(order.last_snapshot)
                self._validate_fill_snapshot(order, order.last_snapshot)
                if accounting.totals.quantity != order.filled_quantity or accounting.totals.cost_usd != order.filled_cost_usd or accounting.totals.fee_usd != order.fees or accounting.totals.gross_payout_usd != order.gross_payout_usd:
                    raise ValueError("Unapplied journaled executions")
            except ValueError:
                self.risk.halt("Journaled native executions need fresh reconciliation")
                await self._save(order.model_copy(update={"status":OrderStatus.UNKNOWN,"reconciliation_complete":False,
                    "reason":"Journaled native executions need fresh reconciliation"}))
                await self.storage.write("INSERT OR REPLACE INTO native_settlement_invalidations SELECT ?,? WHERE EXISTS(SELECT 1 FROM native_settlements WHERE order_id=?)",
                    (order_id,"Journaled executions differ from durable aggregate",order_id),True)

    def register(self, venue: str, adapter):
        if venue in self.adapters:
            raise ValueError("Native adapter already registered")
        self.adapters[venue] = adapter

    async def record_private_fills(self, order_id, fills, *, adapter):
        """Durably retain owned executions before aggregate reconciliation.

        Caller supplies the actual registered adapter so a replaced account's
        private stream cannot write into a previous account's durable ledger.
        This never fills an order or releases its reservation by itself.
        """
        fills = tuple(fills)
        async with self.lock:
            order = self.risk.native_orders[order_id]
            if (self.adapters.get(order.venue) is not adapter or
                    getattr(adapter,"bound_account_fingerprint",None) != order.account_fingerprint):
                self.risk.halt("Private execution account binding is not verified")
                return order
            try:
                await self._persist_fill_evidence(order, fills)
            except ValueError:
                from ..venues.reconciliation import NativeOrderReport
                evidence = NativeOrderReport(native_order_id=order.venue_order_id,client_order_id=order.client_order_id,
                    status="unknown",fills=tuple(fills),reason="Conflicting private execution evidence")
                return await self._fill_conflict(order,evidence)
            try:
                if order.last_snapshot is None:
                    return order
                self._validate_fill_snapshot(order, order.last_snapshot)
            except ValueError:
                # A late execution must reopen a formerly terminal liability
                # until its updated aggregate is independently reconciled.
                updated = order.model_copy(update={"status":OrderStatus.UNKNOWN,"reconciliation_complete":False,
                    "reason":"Private execution requires updated financial reconciliation"})
                await self._save(updated)
                if order.id in self.risk.settled:
                    self.risk.halt("Private execution changed after settlement")
                    await self.storage.write("INSERT OR REPLACE INTO native_settlement_invalidations VALUES(?,?)",
                        (order.id,"Private execution changed after settlement"),True)
                    self._invalidate_settlement_memory(order)
                return updated
            return order

    async def _save(self, order):
        try:
            await self.storage.write(
                "INSERT INTO native_orders VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                (order.id, order.client_order_id, order.status.value, order.model_dump_json()), True)
        except BaseException:
            self.risk.halt("Native order persistence failed")
            raise
        self.risk.native_orders[order.id] = order
        await self.bus.publish(Envelope(schema_name="native.order", source="native_oms", payload=order))
        return order

    def _check(self, order):
        if not self.recovered:
            raise RiskRejected("Native recovery has not finished")
        if order.venue not in self.adapters:
            raise RiskRejected("Native execution adapter unavailable")
        self.preflight(order)  # Required callable; failures cannot be ignored.
        self.revalidate(order)
        self.risk.check_native_reservation(order, reserved=order.id in self.risk.native_orders)

    async def execute(self, order: NativeOrder):
        async with self.lock:
            if order.id in self.risk.native_orders or order.status != OrderStatus.INTENT:
                raise RiskRejected("An existing native intent must never be retransmitted")
            if any(o.client_order_id == order.client_order_id for o in self.risk.all_orders()):
                raise RiskRejected("Duplicate client order ID")
            self._check(order)
            self.risk.native_orders[order.id] = order
            await self._save(order)  # fsync barrier before native writes.
            authorization, transmission_started = None, False
            try:
                self._check(order)
                order = await self._save(order.model_copy(update={"status": OrderStatus.SUBMITTING, "submitted_at": utcnow()}))
                self._check(order)  # Check clocks, limits and kill after fsync.
                authorization = self.risk.authority.issue(order.id)
                transmission_started = True
                response = await self.adapters[order.venue].submit_intent(order.intent, authorization)
                native_id = getattr(response, "native_order_id", None)
                if native_id is None and isinstance(response, dict):
                    native_id = response.get("native_order_id") or response.get("venue_order_id")
                if not native_id:
                    raise ValueError("Placement acknowledgement has no order identity")
                # OPEN here means accepted only. Financial totals remain zero.
                order = await self._save(order.model_copy(update={"status": OrderStatus.OPEN,
                    "venue_order_id": native_id, "acknowledged_at": utcnow(),
                    "reconciliation_complete": False, "reason": "Acknowledged; private financial reconciliation pending"}))
                adapter = self.adapters[order.venue]
                if hasattr(adapter,"execution_fills"):
                    # A WS fill may precede the HTTP acknowledgement. Once its
                    # exact native ID is durable, flush buffered evidence before
                    # releasing the routing lock or waiting for a later poll.
                    fills=adapter.execution_fills(native_id,order.intent)
                    try:
                        await self._persist_fill_evidence(order,fills)
                    except ValueError:
                        from ..venues.reconciliation import NativeOrderReport
                        order=await self._fill_conflict(order,NativeOrderReport(native_order_id=native_id,
                            client_order_id=order.client_order_id,status="unknown",fills=tuple(fills)))
                self.metrics.observe("native_order_ack_ms", (order.acknowledged_at-order.submitted_at).total_seconds()*1000)
            except (Exception, asyncio.CancelledError) as exc:
                from ..venues.common import DispatchRejected
                adapter = self.adapters[order.venue]
                known_bindings = getattr(adapter,"_native_intents",{})
                known_unsent = (isinstance(exc,DispatchRejected) and order.venue_order_id is None
                    and order.acknowledged_at is None and not order.filled_quantity
                    and not self._fill_evidence.get(order.id) and order.id not in self._fill_conflicts
                    and not any(intent.client_order_id == order.client_order_id for intent in known_bindings.values()))
                uncertain = transmission_started and not known_unsent
                state = OrderStatus.UNKNOWN if uncertain else OrderStatus.REJECTED
                order = await asyncio.shield(self._save(order.model_copy(update={"status": state,
                    "reconciliation_complete": known_unsent,
                    "reason": f"Native transmission outcome uncertain: {type(exc).__name__}" if uncertain else "Native pre-transmission validation rejected"})))
                if uncertain:
                    self.risk.halt("Unknown native outcome; reconcile without retrying placement")
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                if authorization:
                    self.risk.authority.revoke(authorization)
            return order

    async def invalidate(self, venue: str, reason="Native connection lost"):
        async with self.lock:
            active = [o for o in self.risk.native_orders.values() if o.venue == venue and o.status in UNRESOLVED]
            for order in active:
                await self._save(order.model_copy(update={"status": OrderStatus.UNKNOWN,
                    "reconciliation_complete": False, "reason": reason}))
            if active:
                self.risk.halt("Native stream lost with unresolved orders")

    async def cancel(self, order_id):
        async with self.lock:
            order = self.risk.native_orders[order_id]
            if order.status not in UNRESOLVED:
                return order
            if order.status == OrderStatus.UNKNOWN and order.cancel_attempts and order.cancel_retry_after is None:
                return order
            if order.status == OrderStatus.CANCEL_PENDING and (order.cancel_retry_after is None or utcnow() < order.cancel_retry_after):
                return order
            if order.cancel_attempts >= 3:
                self.risk.halt("Native cancellation retry limit reached; manual reconciliation required")
                return order
            if not order.venue_order_id or order.venue not in self.adapters:
                self.risk.halt("Native cancellation requires identified account order")
                return order
            adapter = self.adapters[order.venue]
            if getattr(adapter, "bound_account_fingerprint", None) != order.account_fingerprint:
                self.risk.halt("Native cancel account identity is not verified")
                return order
            # Emergency cancellation must remain possible after trading is off.
            order = await self._save(order.model_copy(update={"status": OrderStatus.CANCEL_PENDING,
                "reconciliation_complete": False, "cancel_attempts": order.cancel_attempts+1,
                "cancel_retry_after": None}))
            try:
                response = await self.adapters[order.venue].cancel_native(order.venue_order_id, order.intent)
                if getattr(response, "retry_cancel_allowed", False):
                    order = await self._save(order.model_copy(update={"cancel_retry_after": utcnow()+timedelta(seconds=2**order.cancel_attempts),
                        "reason": "Venue permits a bounded cancellation retry"}))
                elif getattr(response, "status", None) == "unknown":
                    order = await self._save(order.model_copy(update={"status": OrderStatus.UNKNOWN,
                        "reason": "Cancellation outcome unknown; reconcile account order"}))
                    self.risk.halt("Native cancellation outcome is unknown")
            except (Exception, asyncio.CancelledError) as exc:
                order = await asyncio.shield(self._save(order.model_copy(update={"status": OrderStatus.UNKNOWN,
                    "reason": f"Cancellation outcome unknown: {type(exc).__name__}"})))
                self.risk.halt("Native cancel outcome unknown; exposure remains reserved")
                if isinstance(exc, asyncio.CancelledError): raise
            return order

    async def cancel_all(self):
        for order in list(self.risk.native_orders.values()):
            if order.status in UNRESOLVED:
                await self.cancel(order.id)

    async def reconcile(self, order_id):
        async def read_and_apply():
            async with self.lock:
                captured = self.risk.native_orders[order_id]
                # A locally confirmed unsent rejection has no exchange order
                # to discover. Periodic account maintenance must not turn it
                # into a permanently unknown transmission after restart.
                if (captured.status == OrderStatus.REJECTED and captured.reconciliation_complete
                        and captured.venue_order_id is None and not captured.filled_quantity
                        and not self._fill_evidence.get(order_id) and order_id not in self._fill_conflicts):
                    return captured
                adapter = self.adapters.get(captured.venue)
                client = getattr(adapter, "client", None)
                if adapter is None or getattr(adapter, "bound_account_fingerprint", None) != captured.account_fingerprint:
                    self.risk.halt("Native reconciliation account identity is not verified")
                    return captured
                native_id = captured.venue_order_id

            def same_binding(current):
                # The account, transport or durable intent can change while a
                # network read is pending. Such a response must never be applied
                # to the replacement binding, even if its IDs happen to match.
                return (self.adapters.get(captured.venue) is adapter
                    and getattr(adapter, "client", None) is client
                    and getattr(adapter, "bound_account_fingerprint", None) == captured.account_fingerprint
                    and current.account_fingerprint == captured.account_fingerprint
                    and current.intent == captured.intent
                    and current.venue_order_id == native_id)

            # Neither identity discovery nor the HTTP reconciliation may occupy
            # the shared routing/cancellation lock. The bounded read also ends
            # if its caller is cancelled; no background financial read survives
            # an outer timeout or shutdown.
            async with asyncio.timeout(20):
                if not native_id:
                    resolved = await adapter.resolve_client_order(captured.intent) if hasattr(adapter, "resolve_client_order") else None
                    async with self.lock:
                        current = self.risk.native_orders[order_id]
                        if not same_binding(current):
                            self.risk.halt("Native reconciliation binding changed during identity discovery")
                            return current
                        if not isinstance(resolved, str) or not resolved.strip():
                            self.risk.halt("Native order identity/account unavailable for reconciliation")
                            return current
                        await self._save(current.model_copy(update={"venue_order_id": resolved}))
                        native_id = resolved
                report = await adapter.reconcile_native(native_id, captured.intent)

            async with self.lock:
                current = self.risk.native_orders[order_id]
                if not same_binding(current):
                    self.risk.halt("Native reconciliation binding changed during financial read")
                    return current
                # A pre-cancel read cannot establish the outcome of a cancel
                # that subsequently timed out. Keep its full reservation until
                # a new read starts after that cancellation attempt.
                if (current.status == OrderStatus.UNKNOWN and
                        current.cancel_attempts > captured.cancel_attempts and not report.terminal):
                    return current
                return await self._apply_report(current, report)

        async with self.lock:
            task = self._reconcile_reads.get(order_id)
            if task is None or task.done():
                task = asyncio.create_task(read_and_apply())
                self._reconcile_reads[order_id] = task
        try:
            # Concurrent subscribers share one authoritative read. Deliberately
            # propagate cancellation to it rather than leaving orphan HTTP work.
            return await task
        finally:
            if task.done() and self._reconcile_reads.get(order_id) is task:
                del self._reconcile_reads[order_id]

    async def _apply_report(self, order, report):
        # Implemented against typed adapter reports; no raw account payloads are
        # forwarded to dashboard or log. Caller owns the central OMS lock.
        if (report.native_order_id != order.venue_order_id or report.client_order_id != order.client_order_id):
            self.risk.halt("Native reconciliation identity mismatch")
            raise ValueError("Native reconciliation identity mismatch")
        try:
            await self._persist_fill_evidence(order, report.fills)
        except ValueError:
            return await self._fill_conflict(order, report)
        if not report.financial_complete or report.snapshot is None:
            self.risk.halt("Native execution fees/fills require complete reconciliation")
            updated = order.model_copy(update={"status": OrderStatus.UNKNOWN,
                "reconciliation_complete": False, "reason": report.reason or "Incomplete native financial evidence"})
            await self.storage.write([
                ("INSERT INTO native_reconciliations VALUES(NULL,?,?)", (order.id,report.model_dump_json())),
                ("UPDATE native_orders SET status=?,data=? WHERE id=?", (updated.status.value,updated.model_dump_json(),order.id)),
            ],(),True)
            self.risk.native_orders[order.id]=updated
            await self.bus.publish(Envelope(schema_name="native.order",source="native_oms",payload=updated))
            return updated
        return await self._apply_snapshot(order, report)

    async def _apply_snapshot(self, order, report):
        snapshot = report.snapshot
        if snapshot.observed_at > utcnow():
            self.risk.halt("Native reconciliation timestamp is in the future")
            raise ValueError("Future native reconciliation evidence")
        if order.last_snapshot and snapshot.observed_at < order.last_snapshot.observed_at:
            return order
        previous = order.last_snapshot
        if (previous and snapshot.source == previous.source and snapshot.revision is not None and
                previous.revision is not None and snapshot.revision < previous.revision):
            return order
        accounting = NativeAccounting(order.intent, order.venue_order_id)
        try:
            if order.last_snapshot:
                accounting.apply_cumulative(order.last_snapshot)
            accounting.apply_cumulative(snapshot)
            self._validate_fill_snapshot(order, snapshot)
            if report.status != snapshot.status:
                raise ValueError("Native status and financial snapshot disagree")
            if report.terminal != (snapshot.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}):
                raise ValueError("Native terminal status is ambiguous")
            if report.terminal and report.remaining_quantity != 0:
                raise ValueError("Native terminal order retains open quantity")
            if order.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED} and not report.terminal:
                raise ValueError("A terminal order cannot return to an open state")
        except ValueError:
            self.risk.halt("Contradictory native financial evidence requires review")
            # Preserve a conservative monetary liability while keeping the
            # exact disputed evidence for reconciliation; never free a lock.
            updated = order.model_copy(update={"status": OrderStatus.UNKNOWN,
                "reconciliation_complete": False, "reason": "Contradictory native financial evidence",
                "unverified_exposure_usd": max(order.reserved_cost,
                    snapshot.cumulative_cost_usd + snapshot.cumulative_fee_usd)})
            statements = [
                ("INSERT INTO native_reconciliations VALUES(NULL,?,?)", (order.id, report.model_dump_json())),
                ("UPDATE native_orders SET status=?,data=? WHERE id=?", (updated.status.value, updated.model_dump_json(), order.id)),
            ]
            if order.id in self.risk.settled:
                statements.append(("INSERT OR REPLACE INTO native_settlement_invalidations VALUES(?,?)",
                    (order.id, "Contradictory financial evidence after settlement")))
            await self.storage.write(statements, (), True)
            if order.id in self.risk.settled:
                self._invalidate_settlement_memory(order)
            self.risk.native_orders[order.id] = updated
            await self.bus.publish(Envelope(schema_name="native.order", source="native_oms", payload=updated))
            return updated
        totals = accounting.totals
        cancellation_pending = order.status == OrderStatus.CANCEL_PENDING and not report.terminal
        updated = order.model_copy(update={"status": OrderStatus.CANCEL_PENDING if cancellation_pending else snapshot.status,
            "filled_quantity": totals.quantity, "filled_cost_usd": totals.cost_usd,
            "gross_payout_usd": totals.gross_payout_usd, "fees": totals.fee_usd,
            "reconciled_at": snapshot.observed_at, "last_snapshot": snapshot,
            "reconciliation_complete": not cancellation_pending,
            "reason": "Cancellation outcome still pending; financial totals reconciled" if cancellation_pending else "Authoritative native financial state reconciled"})
        invalidates_settlement = order.id in self.risk.settled and (updated.filled_quantity, updated.filled_cost_usd, updated.fees) != (order.filled_quantity, order.filled_cost_usd, order.fees)
        statements = []
        if invalidates_settlement:
            statements.append(("INSERT OR REPLACE INTO native_settlement_invalidations VALUES(?,?)",
                (order.id, "Native financial state changed after settlement")))
        try:
            await self.storage.write(statements+[
                ("INSERT INTO native_reconciliations VALUES(NULL,?,?)", (order.id, report.model_dump_json())),
                ("UPDATE native_orders SET status=?,data=? WHERE id=?", (updated.status.value, updated.model_dump_json(), order.id)),
            ], (), True)
        except BaseException:
            self.risk.halt("Native reconciliation persistence failed")
            raise
        self.risk.native_orders[order.id] = updated
        if invalidates_settlement:
            self._invalidate_settlement_memory(order)
            self.risk.halt("Native fill changed after settlement; accounting review required")
        try:
            self.risk.check_position_conflicts(updated, updated.market_snapshot, exclude_order_id=updated.id)
        except RiskRejected:
            self.risk.halt("Native late fill conflicts with an existing position")
        await self.bus.publish(Envelope(schema_name="native.order", source="native_oms", payload=updated))
        return updated

    def _invalidate_settlement_memory(self, order):
        self.invalidated_settlements.add(order.id)
        self.risk.settled.discard(order.id)
        old_settlement = self.settlements.get(order.id)
        if old_settlement and old_settlement.settled_at.date() == self.risk.daily_date:
            self.risk.daily_realized -= self._pnl(order, old_settlement)

    @staticmethod
    def _pnl(order, result):
        return result.payout_usd + result.fee_refund_usd - order.filled_cost_usd - order.fees - result.additional_fee_usd

    def realized_today(self):
        return sum((self._pnl(self.risk.native_orders[oid], result)
                    for oid,result in self.settlements.items()
                    if oid not in self.invalidated_settlements and oid in self.risk.native_orders
                    and result.settled_at.date() == self.risk.daily_date), Decimal(0))

    async def invalidate_settlement(self, order_id, reason):
        """Persist loss of settlement evidence before releasing any cash trust."""
        async with self.lock:
            if order_id in self.invalidated_settlements:
                return
            order = self.risk.native_orders[order_id]
            self.risk.halt("Native settlement changed; audited accounting repair required")
            await self.storage.write("INSERT OR REPLACE INTO native_settlement_invalidations VALUES(?,?)",
                                     (order_id, reason), True)
            self._invalidate_settlement_memory(order)
            await self.bus.publish(Envelope(schema_name="native.order", source="native_oms", payload=order))

    async def settle(self, result: NativeSettlement):
        async with self.lock:
            order = self.risk.native_orders[result.order_id]
            if order.id in self.invalidated_settlements:
                raise ValueError("Invalidated native settlement requires audited accounting repair")
            old = self.settlements.get(order.id)
            if old:
                if old != result: raise ValueError("Conflicting native settlement")
                return
            if (result.venue != order.venue or result.account_fingerprint != order.account_fingerprint or
                    result.environment != order.mode or result.market_fingerprint != order.market_fingerprint or
                    not order.reconciliation_complete or order.status in UNRESOLVED or
                    result.settled_at.tzinfo is None or result.settled_at > utcnow() or
                    result.payout_usd > max(order.gross_payout_usd, order.filled_cost_usd) or
                    result.fee_refund_usd > order.fees):
                raise ValueError("Native settlement requires final matching account evidence")
            await self.storage.write("INSERT INTO native_settlements VALUES(?,?)", (order.id, result.model_dump_json()), True)
            self.settlements[order.id] = result
            self.risk.settled.add(order.id)
            self.risk.roll_day()
            if result.settled_at.date() == self.risk.daily_date:
                self.risk.daily_realized += self._pnl(order, result)
                self._accounted_settlements.add((order.id, self.risk.daily_date.isoformat()))
            await self.bus.publish(Envelope(schema_name="native.settlement", source="native_oms", payload=result))
