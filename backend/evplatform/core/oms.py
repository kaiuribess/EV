import asyncio
import time
from decimal import Decimal

from .models import Envelope, Fill, Order, OrderStatus, utcnow
from .risk import RiskRejected


ACTIVE = {OrderStatus.INTENT, OrderStatus.SUBMITTING, OrderStatus.OPEN, OrderStatus.PARTIAL,
          OrderStatus.CANCEL_PENDING, OrderStatus.UNKNOWN}


class OrderManager:
    def __init__(self, risk, storage, bus, venue, book_lookup, revalidate, metrics):
        self.risk, self.storage, self.bus, self.venue = risk, storage, bus, venue
        self.book_lookup, self.revalidate, self.metrics = book_lookup, revalidate, metrics
        self.lock = asyncio.Lock()
        self.fill_ids = set()

    async def recover(self):
        self.fill_ids = await self.storage.load_fill_ids()
        for order in await self.storage.load_orders():
            if order.status in ACTIVE:
                if order.mode in {"demo", "paper"}:
                    # Simulator orders never exist externally after restart.
                    order = order.model_copy(update={"status": OrderStatus.CANCELLED,
                                                     "reason": "Paper order cancelled during restart recovery"})
                else:
                    order = order.model_copy(update={"status": OrderStatus.UNKNOWN,
                                                     "reason": "Reconciliation required after restart"})
                    self.risk.halt("Unknown native orders require reconciliation")
                await self.storage.save_order(order)
            self.risk.orders[order.id] = order
            self.risk.duplicates[(order.strategy, order.market_id, order.venue, order.outcome)] = order.created_at

    async def _save(self, order):
        await self.storage.save_order(order)
        self.risk.orders[order.id] = order
        await self.bus.publish(Envelope(schema_name="trade.order", source="oms", payload=order))
        return order

    async def execute(self, candidate, market, book, mode):
        async with self.lock:
            candidate = self.revalidate(candidate)
            order = self.risk.reserve(candidate, market, book, mode)
            durable_start = time.perf_counter_ns()
            try:
                await self._save(order)  # durable intent must precede transmission
            except Exception:
                self.risk.halt("Order intent could not be persisted")
                raise
            self.metrics.observe("intent_persistence_ms", (time.perf_counter_ns() - durable_start) / 1e6)
            authorization = None
            try:
                self.risk.check_enabled()
                self.revalidate(candidate)
                self.revalidate(order)
                self.risk.check_order_limits(exclude_order_id=order.id)
                self.risk.check_reserved_limits(order, self.book_lookup(order.market_id, order.venue, order.outcome))
                order = await self._save(order.model_copy(update={
                    "status": OrderStatus.SUBMITTING, "submitted_at": utcnow()}))
                # Revalidate again after the final persistence barrier.
                self.risk.check_enabled()
                self.revalidate(candidate)
                self.revalidate(order)
                self.risk.check_order_limits(exclude_order_id=order.id)
                self.risk.check_reserved_limits(order, self.book_lookup(order.market_id, order.venue, order.outcome))
                authorization = self.risk.authority.issue(order.id)
                result = await self.venue.submit(order, authorization)
                order = await self._save(order.model_copy(update={
                    "status": OrderStatus.OPEN, "venue_order_id": result["venue_order_id"],
                    "acknowledged_at": utcnow()}))
                self.metrics.observe("order_ack_ms", (order.acknowledged_at - order.submitted_at).total_seconds() * 1000)
            except RiskRejected as exc:
                order = await self._save(order.model_copy(update={"status": OrderStatus.REJECTED, "reason": str(exc)}))
            except Exception as exc:
                # A network exception after transmission is not a rejection.
                order = await self._save(order.model_copy(update={
                    "status": OrderStatus.UNKNOWN, "reason": f"Outcome unknown: {type(exc).__name__}"}))
                self.risk.halt("An order outcome is unknown; reconcile before resuming")
            finally:
                if authorization:
                    self.risk.authority.revoke(authorization)
            return order

    async def apply_fill(self, fill: Fill):
        # Caller owns lock. Duplicate fill IDs are idempotent.
        if fill.id in self.fill_ids:
            return
        order = self.risk.orders[fill.order_id]
        if order.id in self.risk.settled:
            self.risk.halt("Fill after settlement requires accounting reconciliation")
            return
        if fill.venue != order.venue or fill.synthetic != order.synthetic or fill.price > order.price:
            self.risk.halt("Fill venue, execution mode, or limit price mismatch")
            return
        if order.status == OrderStatus.REJECTED:
            self.risk.halt("Fill received for rejected order")
            return
        filled = order.filled_quantity + fill.quantity
        if filled > order.quantity:
            self.risk.halt("Fill exceeds order quantity")
            return
        total = order.average_fill_price * order.filled_quantity + fill.price * fill.quantity
        status = OrderStatus.FILLED if filled == order.quantity else OrderStatus.PARTIAL
        # A late fill after cancel confirmation retains cancelled residual quantity.
        if order.status == OrderStatus.CANCELLED and filled < order.quantity:
            status = OrderStatus.CANCELLED
        updated = order.model_copy(update={
            "filled_quantity": filled, "average_fill_price": total / filled,
            "fees": order.fees + fill.fee, "filled_at": fill.confirmed_at, "status": status,
        })
        await self.storage.save_fill_and_order(fill, updated)
        self.fill_ids.add(fill.id)
        self.risk.orders[order.id] = updated
        # A valid late fill must be accounted even if it reveals a conflict with
        # a replacement position. Freeze routing rather than discarding that fill.
        if updated.market_snapshot is not None:
            try:
                self.risk.check_position_conflicts(updated, updated.market_snapshot, exclude_order_id=updated.id)
            except RiskRejected:
                self.risk.halt("Fill created a conflicting position; reconciliation required")
        else:
            self.risk.halt("Fill has unverified legacy market identity; reconciliation required")
        await self.bus.publish(Envelope(schema_name="trade.fill", source="paper_execution", payload=fill))
        await self.bus.publish(Envelope(schema_name="trade.order", source="oms", payload=updated))
        if order.submitted_at:
            self.metrics.observe("simulated_fill_ms", (fill.confirmed_at - order.submitted_at).total_seconds() * 1000)

    async def tick(self):
        async with self.lock:
            for order in list(self.risk.orders.values()):
                if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIAL}:
                    continue
                book = self.book_lookup(order.market_id, order.venue, order.outcome)
                # Standing simulated orders are cancelled when source references become invalid.
                try:
                    self.risk.check_enabled()
                    self.revalidate(order)
                except RiskRejected:
                    await self._cancel(order)
                    continue
                fill = self.venue.fill(order, book)
                if fill:
                    await self.apply_fill(fill)

    async def _cancel(self, order):
        if order.status not in ACTIVE:
            return order
        if order.status == OrderStatus.UNKNOWN:
            # No inferred cancellation; keep exposure reserved until reconciliation.
            return order
        order = await self._save(order.model_copy(update={"status": OrderStatus.CANCEL_PENDING}))
        try:
            result = await self.venue.cancel(order)
            current = self.risk.orders[order.id]
            if result["status"] == "cancelled" and current.status != OrderStatus.FILLED:
                return await self._save(current.model_copy(update={"status": OrderStatus.CANCELLED}))
        except Exception as exc:
            return await self._save(order.model_copy(update={
                "status": OrderStatus.UNKNOWN, "reason": f"Cancellation unknown: {type(exc).__name__}"}))
        return self.risk.orders[order.id]

    async def cancel(self, order_id):
        async with self.lock:
            return await self._cancel(self.risk.orders[order_id])

    async def cancel_all(self):
        async with self.lock:
            for order in list(self.risk.orders.values()):
                if order.status in ACTIVE:
                    await self._cancel(order)

