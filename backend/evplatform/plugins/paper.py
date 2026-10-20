from decimal import Decimal

from ..core.models import Fill, Order, utcnow
from ..core.plugins import ExecutionVenue, Manifest


class PaperVenue(ExecutionVenue):
    manifest = Manifest(
        name="paper_execution", kind="execution", description="Deterministic local simulator; every fill is simulated",
        transports=["local"], input_schemas=["trade.approved@1"], output_schemas=["trade.fill@1"],
        capabilities={"order_types": ["limit"], "cancel": True, "replace": "cancel_then_new",
                      "partial_fills": True, "sandbox": True, "production": False,
                      "fees": "book supplied, explicitly synthetic", "payout": "USD 1 per winning contract"},
        permissions=["paper_orders"],
    )

    def __init__(self, authority):
        self.authority = authority
        self.live_orders = {}
        self.consumed = {}

    async def submit(self, order: Order, authorization):
        if not self.authority.consume(authorization, order.id):
            raise PermissionError("Execution requires a one-use central risk authorization")
        if not order.synthetic or order.mode not in {"paper", "demo"}:
            raise PermissionError("Paper venue requires explicitly simulated execution")
        if order.client_order_id in self.live_orders:
            return self.live_orders[order.client_order_id]
        self.live_orders[order.client_order_id] = {"venue_order_id": f"paper-{order.id}", "status": "open"}
        return self.live_orders[order.client_order_id]

    def fill(self, order, book):
        if book is None or not book.synchronized or book.status != "active":
            return None
        key = (book.market_id, book.venue, book.outcome, book.sequence)
        available = sum(l.quantity for l in book.asks if l.price <= order.price)
        available -= self.consumed.get(key, 0)
        remaining = order.quantity - order.filled_quantity
        # Deliberate first partial fill, then finish only on later simulator ticks.
        quantity = min(available, remaining, max(1, order.quantity // 2))
        if quantity <= 0:
            return None
        self.consumed[key] = self.consumed.get(key, 0) + quantity
        if len(self.consumed) > 4096:
            self.consumed.pop(next(iter(self.consumed)))
        from ..core.pricing import expected_fee
        # Fill at worst allowed price conservatively, no invented price improvement.
        fee = expected_fee(book.fee, order.price, quantity, Decimal(1))
        return Fill(id=f"{order.id}:{order.filled_quantity + quantity}", order_id=order.id,
                    venue=order.venue, quantity=quantity, price=order.price,
                    fee=fee, confirmed_at=utcnow(), synthetic=True)

    async def cancel(self, order):
        if order.client_order_id in self.live_orders:
            self.live_orders[order.client_order_id]["status"] = "cancelled"
        return {"status": "cancelled"}

    async def reconcile(self, order):
        return self.live_orders.get(order.client_order_id, {"status": "unknown"})

