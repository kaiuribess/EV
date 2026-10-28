"""Authenticated ProphetX wallet cash and separate full-inventory verification."""
from datetime import timedelta
from decimal import Decimal
import math

from ..core.models import OrderStatus, utcnow
from .prophetx_financial import (
    ProphetXAccountReview, authenticated_identity, complete_order_history,
    reporting_pages, source_time,
)
from .reconciliation import ReconciliationError, _number, _object, _text
from .common import decimal_value


def balance_facts(raw, *, observed_at, max_age=timedelta(seconds=10), funded_cash_only=False):
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
    # The documented `balance` is available cash, distinct from GEC. Never
    # add credit to it; funded-cash-only inventory review still excludes GEC.
    credit = _number(data.get("gec_balance"), "global exposure credit")
    if funded_cash_only and credit != 0:
        raise ReconciliationError("ProphetX exposure-credit accounting is outside funded-cash scope")
    return cash, matched, unmatched


async def probe_prophetx_account(client, managed_orders=(), *, settled_order_ids=frozenset(),
                                review: ProphetXAccountReview | None = None, observed_at=None,
                                external_positions=()):
    """Verify current cash independently; complete inventory never comes from a balance alone."""
    # Local import permits the shared dispatcher to import this adapter.
    from .account_probe import NativeAccountSnapshot
    now = observed_at or utcnow()
    user, fingerprint = authenticated_identity(client)
    base = dict(venue="prophetx", environment=client.settings.environment,
                account_fingerprint=fingerprint, identity_verified=True, observed_at=now)
    private = getattr(client, "private", None)
    captured_fence = private.begin_probe(client=client) if private is not None else None
    raw_before = await client.fetch_balance()
    before = balance_facts(raw_before, observed_at=observed_at or utcnow(),
                           funded_cash_only=review is not None)
    if any(row.venue == "prophetx" and row.environment == client.settings.environment
           for row in external_positions) and review is not None:
        raise ReconciliationError("ProphetX external fills need an account-specific inventory review")
    if review is None:
        venue_external = [row for row in external_positions if row.venue == "prophetx"
            and row.environment == client.settings.environment]
        scoped_external = [row for row in venue_external if row.account_fingerprint == fingerprint]
        cash_verified = (authenticated_identity(client) == (user, fingerprint)
                         and (private is None or private.fence(client=client) == captured_fence))
        external_cost = sum((row.cost_usd for row in scoped_external), Decimal(0))
        if len(venue_external) != len(scoped_external):
            cash_verified = False
        current = None
        # A complete, current *empty* inventory can be established without
        # reconstructing lifetime history. This never converts a nonempty or
        # lagging account into a zero-exposure account.
        no_credit = _number(_object(raw_before.get("data"), "balance data").get("gec_balance"),
                            "global exposure credit") == 0
        relevant_managed = [row for row in managed_orders if row.venue == "prophetx"
            and row.mode == client.settings.environment and row.id not in settled_order_ids
            and row.status != OrderStatus.REJECTED]
        if (cash_verified and not relevant_managed and not scoped_external and no_credit
                and before[1:] == (Decimal(0), Decimal(0)) and private is not None
                and captured_fence is not None):
            try:
                # The Trading API enforces a 60-day created-at window. This
                # current-active check is paired with zero matched/unmatched
                # wallet exposure and the live private-stream fence; it does
                # not assert that the account has no older settled history.
                query_to = math.ceil(now.timestamp())
                query_from = query_to - 60 * 86400 + 1
                histories = [await reporting_pages(client, "/v4/mm/get_order_history", "orders",
                    params={"status": status, "from": query_from, "to": query_to},
                    observed_at=observed_at)
                    for status in ("open", "inactive")]
                after = balance_facts(await client.fetch_balance(),
                    observed_at=observed_at or utcnow(), funded_cash_only=True)
                if before == after and all(not history.rows for history in histories):
                    watermark = min(history.last_synced_at for history in histories)
                    current = private.reconcile_current_empty(started_at=now,
                        reporting_watermark=watermark, captured_fence=captured_fence,
                        client=client, observed_at=observed_at or utcnow())
            except (ReconciliationError, ValueError, TypeError, AttributeError):
                current = None
        return NativeAccountSnapshot(**base, balance_usd=before[0], cash_available_verified=cash_verified,
            open_order_count=0 if current is not None else None,
            position_count=0 if current is not None else None,
            reconciled=current is not None,
            private_connection_generation=captured_fence[0] if current is not None else None,
            private_revision=captured_fence[1] if current is not None else None,
            private_as_of=current,
            external_position_count=len(scoped_external), external_cost_usd=external_cost,
            reason="" if current is not None else
                ("Current cash is verified; active orders, wallet exposure or reporting lag prevent zero-inventory verification"
                 if cash_verified else "ProphetX account identity changed during wallet read"))
    if review.current_inventory_reviewed_at is not None:
        baseline = review.current_inventory_reviewed_at
        if (review.account_fingerprint != fingerprint or review.valid_until <= now
                or not review.funded_cash_only or baseline >= now
                or now-baseline > timedelta(days=2)):
            raise ReconciliationError("Current ProphetX inventory review is stale or belongs to another account")
        # The documented updated-at query spans at most two days. This route
        # is valid only when the reviewed baseline attests empty funded-cash
        # inventory *and* the separate funding evidence excludes LEC.
        history = await reporting_pages(client, "/v4/mm/get_order_history", "orders",
            params={"updated_at_from": math.floor(baseline.timestamp()),
                    "updated_at_to": math.ceil(now.timestamp())}, observed_at=observed_at)
    else:
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
    if review.current_inventory_reviewed_at is not None and before[1:] != (Decimal(0), Decimal(0)):
        reasons.append("Current-empty-inventory review conflicts with wallet exposure")
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
    after = balance_facts(await client.fetch_balance(), observed_at=observed_at or utcnow(),
                          funded_cash_only=True)
    if before != after or before[1:] != (matched_stake, unmatched_stake):
        reasons.append("ProphetX wallet exposure differs or changed during inventory reads")
    identity_stable = (authenticated_identity(client) == (user, fingerprint) and
                       (private is None or private.fence(client=client) == captured_fence))
    if not identity_stable:
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
        cash_available_verified=identity_stable and before == after,
        private_connection_generation=captured_fence[0] if private_as_of is not None else None,
        private_revision=captured_fence[1] if private_as_of is not None else None,
        private_as_of=private_as_of,
        reason="; ".join(dict.fromkeys(reasons)))
