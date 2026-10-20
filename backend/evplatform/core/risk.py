import secrets
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from .config import RiskConfig
from .models import Candidate, Market, Order, OrderStatus, utcnow
from .pricing import worst_fee


class RiskRejected(ValueError):
    pass


class RiskAuthority:
    def __init__(self):
        self.tokens = {}

    def issue(self, order_id):
        token = secrets.token_hex(32)
        self.tokens[token] = (order_id, utcnow() + timedelta(seconds=5))
        return token

    def consume(self, token, order_id):
        value = self.tokens.pop(token, None)
        return value is not None and value[0] == order_id and utcnow() <= value[1]

    def revoke(self, token):
        self.tokens.pop(token, None)


class RiskEngine:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.auto_trade = False
        self.kill_switch = False
        self.kill_reason = ""
        self.daily_realized = Decimal(0)
        self.daily_date = utcnow().date()
        self.orders: dict[str, Order] = {}
        self.duplicates = {}
        self.settled: set[str] = set()
        self.authority = RiskAuthority()
        self.on_halt = None

    def halt(self, reason):
        self.kill_switch, self.auto_trade, self.kill_reason = True, False, reason
        if self.on_halt:
            self.on_halt(reason)

    def cost(self, order):
        if order.id in self.settled:
            return Decimal(0)
        filled = order.average_fill_price * order.filled_quantity + order.fees
        if order.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FILLED}:
            return filled
        return max(order.reserved_cost, filled)

    def exposures(self):
        return sum((self.cost(o) for o in self.orders.values()), Decimal(0))

    def check_enabled(self):
        if self.kill_switch:
            raise RiskRejected("Emergency kill switch is active")
        if not self.auto_trade:
            raise RiskRejected("Automatic trading is disabled")
        self.roll_day()
        if self.daily_realized <= -self.config.daily_loss_limit:
            raise RiskRejected("Daily realized loss limit reached")

    def roll_day(self):
        if self.daily_date != utcnow().date():
            self.daily_realized, self.daily_date = Decimal(0), utcnow().date()

    def reserve(self, candidate: Candidate, market: Market, book, mode: str) -> Order:
        self.check_enabled()
        if (book is None or candidate.market_id != market.id or book.market_id != market.id or
                candidate.venue != book.venue or candidate.outcome != book.outcome or
                candidate.outcome not in market.outcomes or not book.synchronized or book.status != "active"):
            raise RiskRejected("Candidate and executable market identity do not match")
        if not book.asks or book.asks[0].price > candidate.price or not book.fresh(2500):
            raise RiskRejected("No fresh executable liquidity at the approved limit")
        if candidate.price <= 0 or candidate.price >= 1 or not 0 <= candidate.fair_probability <= 1:
            raise RiskRejected("Invalid candidate economics")
        if mode not in {"demo", "paper"}:
            raise RiskRejected("Native routing requires a verified venue mapping and live preflight")
        if (market.settlement.contract_type != "binary" or market.settlement.payout != 1 or
                market.settlement.push_rule != "not_applicable" or market.settlement.currency != "USD"):
            raise RiskRejected("Current paper sizing requires a verified USD 1 binary payout without pushes")
        if market.status in {"closed", "suspended"}:
            raise RiskRejected("Market is not tradable")
        age = (utcnow() - candidate.calculated_at).total_seconds() * 1000
        if age < 0 or age > self.config.max_candidate_age_ms:
            raise RiskRejected("Expired candidate")
        key = (candidate.strategy, candidate.market_id, candidate.venue, candidate.outcome)
        last = self.duplicates.get(key)
        if last and (utcnow() - last).total_seconds() < self.config.duplicate_window_seconds:
            raise RiskRejected("Duplicate candidate cooldown")
        if any(o.market_id == candidate.market_id and o.venue == candidate.venue and
               o.outcome == candidate.outcome and o.status in {
                   OrderStatus.INTENT, OrderStatus.SUBMITTING, OrderStatus.OPEN,
                   OrderStatus.PARTIAL, OrderStatus.CANCEL_PENDING, OrderStatus.UNKNOWN}
               for o in self.orders.values()):
            raise RiskRejected("An unresolved order already exists")
        executable_depth = sum(level.quantity for level in book.asks if level.price <= candidate.price)
        quantity = min(candidate.max_quantity, executable_depth, int(self.config.max_order_stake / candidate.price))
        # Reserve for maximum potential fees including per-partial rounding.
        per_fee = worst_fee(book.fee, candidate.price, 1)
        quantity = min(quantity, int(self.config.max_order_stake / (candidate.price + per_fee)))
        if quantity < 1:
            raise RiskRejected("Insufficient order stake budget")
        cost = quantity * (candidate.price + per_fee)
        limits = [
            (self.exposures(), self.config.max_total_exposure, "total"),
            (sum(self.cost(o) for o in self.orders.values() if o.event_id == market.event_id),
             self.config.max_event_exposure, "event"),
            (sum(self.cost(o) for o in self.orders.values() if o.venue == candidate.venue),
             self.config.max_venue_exposure, "venue"),
            (sum(self.cost(o) for o in self.orders.values() if o.correlation_id == market.event_id),
             self.config.max_correlated_exposure, "correlated"),
        ]
        if market.player_id:
            limits.append((sum(self.cost(o) for o in self.orders.values() if o.player_id == market.player_id),
                           self.config.max_player_exposure, "player"))
        for exposure, limit, label in limits:
            if exposure + cost > limit:
                raise RiskRejected(f"{label.title()} exposure limit reached")
        order_id = str(uuid4())
        order = Order(
            id=order_id, client_order_id=f"ev-{order_id}", candidate_id=candidate.id,
            strategy=candidate.strategy,
            market_id=market.id, event_id=market.event_id, player_id=market.player_id,
            correlation_id=market.event_id, venue=candidate.venue, outcome=candidate.outcome,
            price=candidate.price, quantity=quantity, reserved_cost=cost,
            synthetic=True, mode=mode,
        )
        self.orders[order.id] = order
        self.duplicates[key] = utcnow()
        return order

