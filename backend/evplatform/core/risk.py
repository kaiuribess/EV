import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from .config import RiskConfig
from .conflicts import UNRESOLVED, metadata_consistent, reserves_position, reviewed_correlations, risk_family
from .models import Candidate, Market, Order, OrderBook, OrderStatus, utcnow
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
        # Native permission is process-local and time-bounded. A restart never
        # rehydrates this lease from the durable account or order ledger.
        self.native_authorization_expires_at = None
        self.daily_realized = Decimal(0)
        self.daily_date = utcnow().date()
        self.orders: dict[str, Order] = {}
        # Native orders retain their own exact quantity and payout contracts.
        # They still participate in every central USD exposure/position guard.
        self.native_orders: dict = {}
        # Reviewed external positions are never inserted into the OMS ledger.
        # Authenticated cash already reflects their debit; they contribute only
        # to exposure and conservative position-conflict checks.
        self.external_positions = ()
        # Other venues can be disabled independently, but their unknown
        # exposure still vetoes every real-money reservation until bounded.
        self.pending_external_orders = ()
        self.duplicates = {}
        self.settled: set[str] = set()
        self.authority = RiskAuthority()
        self.on_halt = None
        self._manual_order_id: ContextVar[str | None] = ContextVar("native_manual_order_id", default=None)

    @contextmanager
    def manual_order(self, order_id: str):
        """Authorize only this request's one reviewed native intent, not automation."""
        token = self._manual_order_id.set(order_id)
        try:
            yield
        finally:
            self._manual_order_id.reset(token)

    def manual_order_active(self, order_id: str) -> bool:
        return self._manual_order_id.get() == order_id

    def halt(self, reason):
        self.kill_switch, self.auto_trade, self.kill_reason = True, False, reason
        self.native_authorization_expires_at = None
        if self.on_halt:
            self.on_halt(reason)

    def expire_native_authorization(self):
        deadline = self.native_authorization_expires_at
        if not self.kill_switch and (deadline is None or utcnow() >= deadline):
            self.halt("Native trading authorization expired or missing")
            return True
        return False

    def cost(self, order):
        if order.id in self.settled:
            return Decimal(0)
        native_cost = getattr(order, "filled_cost_usd", None)
        filled = (native_cost if native_cost is not None else
                  order.average_fill_price * order.filled_quantity) + order.fees
        if order.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FILLED}:
            return filled
        return max(order.reserved_cost, filled)

    def exposures(self):
        return (sum((self.cost(o) for o in self.all_orders()), Decimal(0))
                + sum((o.cost_usd for o in self.external_positions), Decimal(0)))

    def all_orders(self):
        return (*self.orders.values(), *self.native_orders.values())

    def check_enabled(self, manual_order_id: str | None = None):
        if self.kill_switch:
            raise RiskRejected("Emergency kill switch is active")
        if not self.auto_trade and not (manual_order_id and self.manual_order_active(manual_order_id)):
            raise RiskRejected("Automatic trading is disabled")
        self.roll_day()
        if self.config.daily_loss_limit is not None and self.daily_realized <= -self.config.daily_loss_limit:
            raise RiskRejected("Daily realized loss limit reached")

    def check_native_reservation(self, order, *, reserved=False, preview_only=False):
        """Shared limits for an exact-unit order; caller holds the shared OMS lock."""
        if order.mode == "production" and self.pending_external_orders:
            raise RiskRejected("Unbounded external account exposure blocks native execution")
        if preview_only:
            # Dry-run inspects every monetary/position limit while disarmed.
            # The OMS and dispatch guard always call the default armed path.
            if getattr(order, "execution_origin", "automatic") != "manual":
                raise RiskRejected("Only a manual order may be previewed while disarmed")
            self.roll_day()
            if self.config.daily_loss_limit is not None and self.daily_realized <= -self.config.daily_loss_limit:
                raise RiskRejected("Daily realized loss limit reached")
        else:
            if self.expire_native_authorization():
                raise RiskRejected("Native trading authorization expired or missing")
            self.check_enabled(order.id if getattr(order, "execution_origin", "automatic") == "manual" else None)
        if (not reserved and self.config.max_native_order_attempts is not None
                and len(self.native_orders) >= self.config.max_native_order_attempts):
            raise RiskRejected("Native order attempt limit reached")
        self.check_native_daily_budget(order)
        if order.synthetic or order.mode not in {"sandbox", "production"}:
            raise RiskRejected("Native execution requires an explicit native environment")
        if not metadata_consistent(order):
            raise RiskRejected("Native order has unverified canonical position metadata")
        if self.config.live_max_order_stake is None and not self.config.live_stake_authorized:
            raise RiskRejected("An explicit native maximum order stake is required")
        cost = order.reserved_cost
        caps = [v for v in (self.config.max_order_stake, self.config.live_max_order_stake) if v is not None]
        if not cost.is_finite() or cost <= 0 or any(cost > cap for cap in caps):
            raise RiskRejected("Native order exceeds its explicit stake limit")
        if reserved and self.native_orders.get(order.id) != order:
            raise RiskRejected("Native durable reservation is missing or changed")
        self.check_order_limits(exclude_order_id=order.id if reserved else None)
        self.check_position_conflicts(order, order.market_snapshot, exclude_order_id=order.id if reserved else None)
        others = [o for o in self.all_orders() if o.id != order.id]
        if not reserved and any(o.client_order_id == order.client_order_id for o in self.all_orders()):
            raise RiskRejected("Client order ID already reserved")
        if not reserved and any(
            o.market_fingerprint == order.market_fingerprint and o.outcome == order.outcome and
            (o.created_at.tzinfo is None or (utcnow() - o.created_at).total_seconds() < self.config.duplicate_window_seconds)
            for o in others
        ):
            raise RiskRejected("Duplicate native candidate cooldown")
        limits = [
            (others, self.config.max_total_exposure, "total"),
            ([o for o in others if o.event_id == order.event_id], self.config.max_event_exposure, "event"),
            ([o for o in others if o.venue == order.venue], self.config.max_venue_exposure, "venue"),
            ([o for o in others if o.correlation_id == order.correlation_id], self.config.max_correlated_exposure, "correlated"),
        ]
        if order.player_id:
            limits.append(([o for o in others if o.player_id == order.player_id], self.config.max_player_exposure, "player"))
        for orders, limit, label in limits:
            external = sum((p.cost_usd for p in self.external_positions
                if p.environment == order.mode and (label == "total" or
                    label == "venue" and p.venue == order.venue)), Decimal(0))
            if limit is not None and sum((self.cost(o) for o in orders), Decimal(0)) + external + cost > limit:
                raise RiskRejected(f"Native {label} exposure limit reached")

    def check_native_daily_budget(self, order):
        """Count durable native intents across venues on the UTC submission day.

        Rejected, unfilled and uncertain intents still consume an attempt. Their
        worst-case reserved debit remains charged to the daily budget; this is
        deliberately conservative and survives process restart.
        """
        attempts_cap = self.config.max_native_daily_attempts
        debit_cap = self.config.max_native_daily_debit
        if attempts_cap is None and debit_cap is None:
            return
        today = utcnow().date()
        prior = []
        for existing in self.native_orders.values():
            if existing.id == order.id or existing.mode != order.mode:
                continue
            stamp = existing.created_at
            # Naive or future clocks cannot grant a fresh budget.
            if stamp.tzinfo is None or stamp.astimezone(timezone.utc).date() >= today:
                prior.append(existing)
        if attempts_cap is not None and len(prior) + 1 > attempts_cap:
            raise RiskRejected("Daily native order attempt limit reached")
        if debit_cap is not None:
            prior_debit = sum((max(item.reserved_cost,
                item.filled_cost_usd + item.fees) for item in prior), Decimal(0))
            # Reviewed raw fills are outside the OMS, but they still consumed
            # this account's daily debit. Their cost is already reflected in
            # available cash; this addition applies only to the daily budget.
            prior_debit += sum((item.cost_usd for item in self.external_positions
                if item.environment == order.mode and (item.recorded_at.tzinfo is None
                    or item.recorded_at.astimezone(timezone.utc).date() >= today)), Decimal(0))
            if prior_debit + order.reserved_cost > debit_cap:
                raise RiskRejected("Daily native upfront debit limit reached")

    def roll_day(self):
        if self.daily_date != utcnow().date():
            self.daily_realized, self.daily_date = Decimal(0), utcnow().date()

    def check_order_limits(self, exclude_order_id: str | None = None):
        """Check submission caps under the OMS lock, including durable intents.

        All reserved attempts count toward the rolling burst limit, including
        rejected/cancelled intents. Rehydrated audit orders provide the same
        accounting after restart without a volatile counter reset.
        """
        others = [order for order in self.all_orders() if order.id != exclude_order_id]
        if sum(order.status in UNRESOLVED and order.id not in self.settled
               for order in others) >= self.config.max_open_orders:
            raise RiskRejected("Maximum open order count reached")
        now = utcnow()
        recent = 0
        for order in others:
            # Missing clock zone is legacy uncertainty, not permission to reset
            # a burst budget. Future clocks likewise conservatively count.
            if order.created_at.tzinfo is None or (now - order.created_at).total_seconds() < 60:
                recent += 1
        if recent >= self.config.max_orders_per_minute:
            raise RiskRejected("Rolling orders per minute limit reached")

    def check_reserved_limits(self, order: Order, current_book: OrderBook | None):
        """Recheck current budgets and fee bounds after every durable barrier.

        Reservations cannot silently expand when fees change. Actual fills stay
        accounted; rejecting this check prevents transmission or cancels only
        the remaining standing quantity. Caller owns the OMS lock.
        """
        if order.id not in self.orders:
            raise RiskRejected("Order has no central risk reservation")
        if (current_book is None or current_book.market_id != order.market_id or
                current_book.venue != order.venue or current_book.outcome != order.outcome):
            raise RiskRejected("Reserved order fee book does not match its identity")
        try:
            per_fee = worst_fee(current_book.fee, order.price, 1)
        except ValueError as exc:
            raise RiskRejected("Current fee schedule is not verified") from exc
        filled_cost = order.average_fill_price * order.filled_quantity + order.fees
        residual = max(0, order.quantity - order.filled_quantity)
        required = filled_cost + residual * (order.price + per_fee)
        if required > order.reserved_cost:
            raise RiskRejected("Current fees or fills exceed the approved order reservation")
        if self.config.max_order_stake is None or max(required, order.reserved_cost) > self.config.max_order_stake:
            raise RiskRejected("Current order stake limit is below its reservation")
        limits = [
            (self.exposures(), self.config.max_total_exposure, "total"),
            (sum(self.cost(o) for o in self.all_orders() if o.event_id == order.event_id),
             self.config.max_event_exposure, "event"),
            (sum(self.cost(o) for o in self.all_orders() if o.venue == order.venue),
             self.config.max_venue_exposure, "venue"),
            (sum(self.cost(o) for o in self.all_orders() if o.correlation_id == order.correlation_id),
             self.config.max_correlated_exposure, "correlated"),
        ]
        if order.player_id:
            limits.append((sum(self.cost(o) for o in self.all_orders() if o.player_id == order.player_id),
                           self.config.max_player_exposure, "player"))
        for exposure, limit, label in limits:
            if limit is not None and exposure > limit:
                raise RiskRejected(f"Current {label} exposure limit is below reserved exposure")

    def check_position_conflicts(self, candidate, market: Market, exclude_order_id: str | None = None):
        """Block conflicting positions across every strategy and execution venue.

        This synchronous check must be called inside the OMS lock before intent
        reservation and after each awaitable durability barrier. Family blocks
        are deliberately conservative; they do not net different settlement
        terms, alternates, or player props into a fabricated hedge.
        """
        try:
            groups = set(reviewed_correlations(market))
        except ValueError as exc:
            raise RiskRejected(str(exc)) from exc
        family = risk_family(market)
        fingerprint = market.identity()
        for external in self.external_positions:
            # A raw external receipt has no reviewed canonical event/family ID.
            # Block the entire sport until such identity is established or the
            # actual position settles; never guess that another market is safe.
            if external.sport == market.sport:
                raise RiskRejected("Reviewed external native position blocks this sport until settlement")
        own = (self.orders.get(exclude_order_id) or self.native_orders.get(exclude_order_id)) if exclude_order_id else None
        if own and (not metadata_consistent(own) or own.market_fingerprint != fingerprint or
                    (own.market_id, own.venue, own.outcome, own.strategy) !=
                    (candidate.market_id, candidate.venue, candidate.outcome, candidate.strategy)):
            raise RiskRejected("Reserved canonical market identity changed or is unverified")
        for order in self.all_orders():
            if order.id == exclude_order_id or not reserves_position(order, self.settled):
                continue
            same_event = order.event_id == market.event_id
            same_market = order.market_id == market.id
            same_player = bool(market.player_id and order.player_id == market.player_id)
            if self.config.block_overlapping_events and same_event:
                raise RiskRejected("Overlapping event already reserved across accounts and venues")
            if self.config.block_overlapping_players and same_player:
                raise RiskRejected("Overlapping player already reserved across accounts and venues")
            # All built-in and reviewed families are event scoped. Avoid
            # repeatedly hashing unrelated event snapshots in the hot path.
            if not same_event and not same_market:
                continue
            if not metadata_consistent(order):
                if same_event or same_market:
                    raise RiskRejected("Unverified legacy position metadata blocks this event")
                continue
            if same_market and order.market_fingerprint != fingerprint:
                raise RiskRejected("Canonical market ID conflicts with an existing position")
            if order.market_fingerprint == fingerprint:
                if order.outcome != candidate.outcome:
                    raise RiskRejected("Opposing position already reserved across strategies or venues")
                raise RiskRejected("Duplicate position already reserved across strategies or venues")
            if order.risk_family_id == family:
                raise RiskRejected("Market family already reserved; alternate-line stacking is blocked")
            if groups.intersection(order.reviewed_correlation_groups):
                raise RiskRejected("Reviewed correlated position already reserved")

    def reserve(self, candidate: Candidate, market: Market, book, mode: str) -> Order:
        self.check_enabled()
        if not candidate.execution_eligible:
            raise RiskRejected("Research-only reference candidate cannot reserve exposure")
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
        if self.config.max_order_stake is None or self.config.max_venue_exposure is None or self.config.max_total_exposure is None:
            raise RiskRejected("Paper trading requires explicit simulated exposure caps")
        if (market.settlement.contract_type != "binary" or market.settlement.payout != 1 or
                market.settlement.push_rule != "not_applicable" or market.settlement.currency != "USD"):
            raise RiskRejected("Current paper sizing requires a verified USD 1 binary payout without pushes")
        if market.status in {"closed", "suspended"}:
            raise RiskRejected("Market is not tradable")
        age = (utcnow() - candidate.calculated_at).total_seconds() * 1000
        if age < 0 or age > self.config.max_candidate_age_ms:
            raise RiskRejected("Expired candidate")
        self.check_order_limits()
        fingerprint = market.identity()
        key = (fingerprint, candidate.outcome)
        last = self.duplicates.get(key)
        # Audit records preserve the global cooldown across a process restart.
        matching_times = [o.created_at for o in self.all_orders()
                          if o.market_fingerprint == fingerprint and o.outcome == candidate.outcome]
        if last:
            matching_times.append(last)
        if any(t.tzinfo is None or (utcnow() - t).total_seconds() < self.config.duplicate_window_seconds
               for t in matching_times):
            raise RiskRejected("Duplicate candidate cooldown")
        self.check_position_conflicts(candidate, market)
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
            (sum(self.cost(o) for o in self.all_orders() if o.event_id == market.event_id),
             self.config.max_event_exposure, "event"),
            (sum(self.cost(o) for o in self.all_orders() if o.venue == candidate.venue),
             self.config.max_venue_exposure, "venue"),
            (sum(self.cost(o) for o in self.all_orders() if o.correlation_id == market.event_id),
             self.config.max_correlated_exposure, "correlated"),
        ]
        if market.player_id:
            limits.append((sum(self.cost(o) for o in self.all_orders() if o.player_id == market.player_id),
                           self.config.max_player_exposure, "player"))
        for exposure, limit, label in limits:
            if limit is not None and exposure + cost > limit:
                raise RiskRejected(f"{label.title()} exposure limit reached")
        order_id = str(uuid4())
        order = Order(
            id=order_id, client_order_id=f"ev-{order_id}", candidate_id=candidate.id,
            strategy=candidate.strategy,
            market_id=market.id, event_id=market.event_id, player_id=market.player_id,
            correlation_id=market.event_id, venue=candidate.venue, outcome=candidate.outcome,
            price=candidate.price, quantity=quantity, reserved_cost=cost,
            synthetic=True, mode=mode,
            market_snapshot=market, market_fingerprint=fingerprint,
            risk_family_id=risk_family(market),
            reviewed_correlation_groups=reviewed_correlations(market),
        )
        self.orders[order.id] = order
        self.duplicates[key] = utcnow()
        return order

