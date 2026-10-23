"""Wallet-backed normal win/loss settlement for reviewed funded-cash accounts.

No maker rebates, exposure credits, push/void, manual grades or resettlement.
An explicit account review and actual order/fee/cash evidence are mandatory.
"""
from decimal import Decimal
from datetime import timedelta
import hashlib
import json

from ..core.models import OrderStatus, utcnow
from ..core.native_oms import NativeSettlement
from .prophetx_financial import (
    ProphetXAccountReview, authenticated_identity, normalize_financial_order,
    reporting_pages, source_time, validate_wallet_rows, owned_financial_order,
)
from .reconciliation import ReconciliationError, _number, _object
from .prophetx_account import balance_facts


def supports_prophetx_settlement_scope(profile, adapter, mappings, managed_orders):
    """Explicit narrow capability; never account access or lifecycle proof."""
    from ..core.readiness import LaunchProfile
    from .bridge import NativeExecutionVenue
    from .prophetx import ProphetXClient
    from .book_normalization import NativeBookMapper, NativeMarketMapping
    if (not isinstance(profile, LaunchProfile)
            or profile.settlement_policy != "prophetx_funded_cash_win_loss_halt_on_exception"
            or profile.venue != "prophetx" or not isinstance(adapter, NativeExecutionVenue)
            or not isinstance(adapter.client, ProphetXClient)
            or adapter.client.settings.environment != profile.environment):
        return False
    try:
        review = ProphetXAccountReview.model_validate(adapter.client.account_review)
        now = utcnow()
        baseline = review.history_created_from or review.current_inventory_reviewed_at
        if (not review.funded_cash_only or review.account_fingerprint != profile.account_fingerprint
                or review.valid_until <= now or baseline is None or baseline >= now
                or (review.current_inventory_reviewed_at is not None
                    and now-baseline > timedelta(days=2))):
            return False
        reviewed = NativeBookMapper(tuple(NativeMarketMapping.model_validate(m.model_dump()) for m in mappings))
        scoped = [m for m in reviewed.mappings if m.instrument.venue == profile.venue and m.market.id in profile.market_ids]
        if {m.market.id for m in scoped} != set(profile.market_ids):
            return False
        for m in scoped:
            i = m.instrument
            execution = adapter.mappings.get((m.market.id, i.outcome))
            if (i.quantity_unit != "usd_stake" or i.price_format != "american"
                    or not m.fee_bound.observed_at <= now < m.fee_bound.valid_until
                    or execution is None or not execution.verified or not execution.fee_schedule_verified
                    or execution.native_instrument_id != i.instrument_id or execution.native_outcome != i.native_outcome
                    or execution.settlement_fingerprint != m.market.identity()
                    or execution.native_quantity_unit != i.quantity_unit or execution.native_payout_usd is not None):
                return False
        strikes = set()
        for o in managed_orders:
            if o.venue != profile.venue or o.mode != profile.environment or o.account_fingerprint != profile.account_fingerprint:
                continue
            if o.intent.instrument.instrument_id in strikes or o.created_at <= baseline:
                return False
            strikes.add(o.intent.instrument.instrument_id)
        return True
    except (AttributeError, TypeError, ValueError):
        return False


async def reconcile_prophetx_settlement(client, order, managed_orders, *,
                                        review: ProphetXAccountReview | None = None,
                                        observed_at=None):
    now = observed_at or utcnow()
    user, fingerprint = authenticated_identity(client)
    baseline = (review.history_created_from or review.current_inventory_reviewed_at) if review else None
    if (review is None or review.account_fingerprint != fingerprint or not review.funded_cash_only
            or review.valid_until <= now or baseline is None or baseline >= order.created_at):
        raise ReconciliationError("Current funded-cash account review is required for ProphetX settlement")
    if (order.venue != "prophetx" or order.mode != client.settings.environment or
            order.account_fingerprint != fingerprint or not order.venue_order_id or
            order.status not in {OrderStatus.FILLED, OrderStatus.CANCELLED} or
            not order.reconciliation_complete or not order.filled_quantity):
        raise ReconciliationError("Owned terminal financially reconciled ProphetX order required")
    # This deliberately narrow scope avoids allocating credits or cash across
    # multiple orders in a shared position while account semantics are untested.
    related = [o for o in managed_orders if o.venue == order.venue and o.mode == order.mode
               and o.account_fingerprint == fingerprint and o.intent.instrument.instrument_id == order.intent.instrument.instrument_id]
    if len(related) != 1 or related[0].id != order.id:
        raise ReconciliationError("Shared ProphetX position settlement is unsupported")
    balance_facts(await client.fetch_balance(), observed_at=observed_at or utcnow(), funded_cash_only=True)
    raw = _object(await client.reconcile_order(order.venue_order_id), "order response")
    row = _object(raw.get("data"), "owned order")
    if row.get("status") != "closed":
        if row.get("status") in {"open", "settled"}:
            return None  # `settled` can precede the actual cash payment.
        raise ReconciliationError("Manual, void or corrected ProphetX settlement requires review")
    grade = row.get("winning_status")
    if grade not in {"won", "lost", "profit", "loss"}:
        raise ReconciliationError("Unsupported ProphetX settlement grade")
    row = await owned_financial_order(client, order.intent, order.venue_order_id,
                                    user_id=user, observed_at=observed_at, row=row)
    settled = source_time(row.get("settled_at"), observed_at=now, name="Actual settlement")
    if settled < order.created_at:
        raise ReconciliationError("Settlement precedes owned order")
    matched = await reporting_pages(client, "/v4/mm/get_order_matched_detail", "matching_details",
        params={"order_id": order.venue_order_id}, observed_at=observed_at)
    winning = grade in {"won", "profit"}
    for fill in matched.rows:
        if "winning_status" in fill and fill["winning_status"] not in ({"won", "profit"} if winning else {"lost", "loss"}):
            raise ReconciliationError("Matched fill grade contradicts closed order settlement")
    wallet_history = await reporting_pages(client, "/v4/mm/get_transactions", "transactions",
        params={"trade_id": order.venue_order_id}, observed_at=observed_at, require_clock=False)
    at = observed_at or utcnow()
    report = normalize_financial_order(order.intent, row, matched.rows, wallet_history.rows, user_id=user,
        expected_order_id=order.venue_order_id, observed_at=at, reporting_watermark=matched.last_synced_at)
    snap = report.snapshot
    if (snap.cumulative_quantity != order.filled_quantity or snap.cumulative_cost_usd != order.filled_cost_usd
            or snap.cumulative_gross_payout_usd != order.gross_payout_usd
            or not order.fees <= snap.cumulative_fee_usd <= order.intent.max_fee_usd
            or matched.last_synced_at < settled):
        raise ReconciliationError("ProphetX settlement differs from reconciled fill accounting")
    settlement_fee = snap.cumulative_fee_usd - order.fees
    wallet = validate_wallet_rows(wallet_history.rows, user_id=user, order_id=order.venue_order_id,
                                  market_id=row.get("market_id"), observed_at=at)
    pre_settlement_fee = sum((_number(x["amount"], "pre-settlement commission") for x in wallet
        if x["transaction_type"] == "COMMISSION" and
        source_time(x["created_at"], observed_at=at, name="Commission") < settled), Decimal(0))
    # A reporting refresh may already have folded the settlement commission
    # into the durable cumulative fee before this settlement read. Accept only
    # the earlier total or the final total, never an unsupported intermediate.
    if order.fees not in {pre_settlement_fee, snap.cumulative_fee_usd}:
        raise ReconciliationError("ProphetX commission timing differs from reconciled fees")
    post_settlement_fee = sum((_number(x["amount"], "settlement commission") for x in wallet
        if x["transaction_type"] == "COMMISSION" and
        source_time(x["created_at"], observed_at=at, name="Commission") >= settled), Decimal(0))
    if post_settlement_fee != snap.cumulative_fee_usd - pre_settlement_fee:
        raise ReconciliationError("ProphetX settlement commission lacks matching wallet evidence")
    if not winning and order.intent.fee_payment_timing == "winnings_only" and post_settlement_fee:
        raise ReconciliationError("A losing ProphetX order has an unexpected winnings fee")
    pays = [row for row in wallet if row["transaction_type"] == "PAY"]
    payout = sum((_number(row["amount"], "settlement credit") for row in pays), Decimal(0))
    if grade in {"won", "profit"}:
        if not pays or payout != order.gross_payout_usd:
            raise ReconciliationError("Winning ProphetX order lacks exact actual cash payout")
        if any(source_time(x["created_at"], observed_at=at, name="Payout credit") < settled for x in pays):
            raise ReconciliationError("Cash payout precedes settlement evidence")
    elif pays or payout:
        raise ReconciliationError("Losing ProphetX order has unexplained payout credit")
    if authenticated_identity(client) != (user, fingerprint):
        raise ReconciliationError("ProphetX account changed during settlement reads")
    evidence = json.dumps({"source": "prophetx.closed_order+matched_details+wallet",
        "native_order_id": order.venue_order_id, "grade": grade,
        "settled_at": settled.isoformat(), "matched_watermark": matched.last_synced_at.isoformat(),
        "payout_usd": str(payout), "execution_fees_usd": str(order.fees),
        "settlement_fee_usd": str(settlement_fee),
        "transactions": [{k: x[k] for k in ("details", "transaction_type", "amount", "change", "created_at")}
                         for x in wallet]}, sort_keys=True, default=str)
    # Stable revision changes on a real financial correction; clocks from a
    # later reporting sync alone must not create a duplicate settlement.
    revision = json.dumps([order.venue_order_id, grade, settled.isoformat(), str(payout),
                           str(order.fees), str(settlement_fee),
                           sorted(str(x["details"]) for x in wallet)], separators=(",", ":"))
    return NativeSettlement(order_id=order.id, venue="prophetx", account_fingerprint=fingerprint,
        environment=order.mode, market_fingerprint=order.market_fingerprint,
        settlement_id="prophetx:"+hashlib.sha256(revision.encode()).hexdigest(),
        payout_usd=payout, additional_fee_usd=settlement_fee, settled_at=settled, evidence=evidence)
