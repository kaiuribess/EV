"""Full-history, funded-cash ProphetX inventory verification (read-only)."""
from datetime import timedelta
from decimal import Decimal

from ..core.models import OrderStatus, utcnow
from .prophetx_financial import (
    ProphetXAccountReview, authenticated_identity, complete_order_history,
    source_time,
)
from .reconciliation import ReconciliationError, _number, _object, _text
from .common import decimal_value


def balance_facts(raw, *, observed_at, max_age=timedelta(seconds=10)):
    data = _object(_object(raw, "balance").get("data"), "balance data")
    if data.get("unmatched_order_balance_status") != "succeed":
        raise ReconciliationError("ProphetX unmatched balance has not synchronized")
    # Live wallet reads have shown the provider's sync clock slightly ahead of
    # the local receipt clock. This narrow allowance applies only to the
    # wallet synchronization marker, never order/history/quote timestamps.
    source_time(data.get("unmatched_order_last_synced_at"), observed_at=observed_at,
                name="Unmatched wallet balance", max_age=max_age,
                max_future_skew=timedelta(seconds=2))
    cash = _number(data.get("balance"), "available balance")
    matched = _number(data.get("matched_order_balance"), "matched balance")
    unmatched = _number(data.get("unmatched_order_balance"), "unmatched balance")
    if _number(data.get("gec_balance"), "global exposure credit") != 0:
        raise ReconciliationError("ProphetX exposure-credit accounting is outside funded-cash scope")
    return cash, matched, unmatched


async def probe_prophetx_account(client, managed_orders=(), *, settled_order_ids=frozenset(),
                                review: ProphetXAccountReview | None = None, observed_at=None):
    """A complete scan is account-scoped and never creates authorization.

    No short lookback is called complete. Missing inception/funding evidence
    returns an actionable unreconciled snapshot. Resting and filled inventory
    must agree with both durable orders and authenticated wallet aggregates.
    """
    # Local import permits the shared dispatcher to import this adapter.
    from .account_probe import NativeAccountSnapshot
    now = observed_at or utcnow()
    user, fingerprint = authenticated_identity(client)
    base = dict(venue="prophetx", environment=client.settings.environment,
                account_fingerprint=fingerprint, identity_verified=True, observed_at=now)
    private = getattr(client, "private", None)
    captured_fence = private.begin_probe(client=client) if private is not None else None
    before = balance_facts(await client.fetch_balance(), observed_at=observed_at or utcnow())
    if review is None:
        return NativeAccountSnapshot(**base, balance_usd=before[0], reason=
            "Account inception and funded-cash-only review is required for complete ProphetX inventory")
    history = await complete_order_history(client, review, observed_at=observed_at)
    relevant = [o for o in managed_orders if o.venue == "prophetx" and o.mode == client.settings.environment
                and o.id not in settled_order_ids and o.status != OrderStatus.REJECTED]
    owned = {o.venue_order_id: o for o in relevant if o.venue_order_id}
    reasons = []
    if len(owned) != len(relevant) or any(o.account_fingerprint != fingerprint or not o.reconciliation_complete
                                        or o.status == OrderStatus.UNKNOWN for o in relevant):
        reasons.append("Managed ProphetX order identity/financial reconciliation is incomplete")
    resting, positions, seen_owned = {}, {}, set()
    matched_stake, unmatched_stake = Decimal(0), Decimal(0)
    for row in history.rows:
        if row.get("user_id") != user:
            raise ReconciliationError("ProphetX history contains a different participant")
        oid = _text(row.get("order_id"), "native order identity")
        filled = _number(row.get("filled_quantity"), "filled stake")
        remaining = _number(row.get("open_quantity"), "remaining stake")
        state = row.get("status")
        updated = source_time(row.get("updated_at"), observed_at=observed_at or utcnow(), name="Order update")
        if updated > history.last_synced_at:
            raise ReconciliationError("Order history exceeds reporting watermark")
        order = owned.get(oid)
        if order:
            seen_owned.add(oid)
            if (row.get("external_id") != order.client_order_id or
                    row.get("strike_id") != order.intent.instrument.instrument_id or
                    decimal_value(row.get("price")) != order.intent.limit_price or
                    _number(row.get("original_quantity"), "original stake") != order.quantity or
                    filled != order.filled_quantity or
                    _number(row.get("fee"), "actual order fee") != order.fees):
                reasons.append("ProphetX order differs from durable financial inventory")
        if state == "open":
            if order is None:
                reasons.append("Unmanaged ProphetX order or unsettled position exists")
            if remaining:
                resting[oid] = remaining
                unmatched_stake += remaining
                if order and (order.status not in {OrderStatus.OPEN, OrderStatus.PARTIAL} or
                              remaining != order.quantity-order.filled_quantity):
                    reasons.append("ProphetX remaining stake differs from durable order")
            if filled:
                strike = _text(row.get("strike_id"), "native strike identity")
                positions[strike] = positions.get(strike, Decimal(0))+filled
                matched_stake += filled
        elif state in {"canceled", "invalid"} and filled == remaining == 0:
            if order and order.status not in {OrderStatus.CANCELLED, OrderStatus.REJECTED}:
                reasons.append("ProphetX cancellation has not reconciled locally")
        elif state == "closed" and remaining == 0:
            if order and filled:
                reasons.append("ProphetX cash settlement has not reconciled locally")
        else:
            reasons.append("Pending, manual, void or unsupported ProphetX order state requires review")
    if seen_owned != set(owned):
        reasons.append("Managed ProphetX order is missing from complete history")
    expected_resting = {oid for oid, o in owned.items() if o.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}}
    if set(resting) != expected_resting:
        reasons.append("ProphetX resting order inventory differs")
    after = balance_facts(await client.fetch_balance(), observed_at=observed_at or utcnow())
    if before != after or before[1:] != (matched_stake, unmatched_stake):
        reasons.append("ProphetX wallet exposure differs or changed during inventory reads")
    if (authenticated_identity(client) != (user, fingerprint) or
            (private is not None and private.fence(client=client) != captured_fence)):
        reasons.append("ProphetX private account changed during inventory reads")
    private_as_of = None
    if private is None or captured_fence is None:
        reasons.append("Confirmed private orders binding and recent private heartbeat are required")
    elif not reasons:
        try:
            private_as_of = private.reconcile_history(history, started_at=now,
                captured_fence=captured_fence, client=client, observed_at=observed_at or utcnow())
        except (ReconciliationError, ValueError, ArithmeticError):
            reasons.append("ProphetX private history watermark, order facts, or account fence did not reconcile")
    return NativeAccountSnapshot(**{**base, "observed_at": observed_at or utcnow()}, balance_usd=after[0],
        open_order_count=len(resting), position_count=len(positions), reconciled=not reasons,
        cash_available_verified=not reasons,
        private_connection_generation=captured_fence[0] if private_as_of is not None else None,
        private_revision=captured_fence[1] if private_as_of is not None else None,
        private_as_of=private_as_of,
        reason="; ".join(dict.fromkeys(reasons)))
