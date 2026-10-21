"""Read-only, narrowly attributable native settlement reconciliation.

Novig v3 position history exposes actual payout/cost/P&L and settlement time.
We accept only ordinary WIN/LOSS grades for one taker-only managed order in a
market, after comparing the complete personal fill history. Maker credits,
netted positions, pushes, FMV, remediation and shared positions need a separate
cash-allocation contract and remain blocked. No public score produces money.

Protocol sources (checked 2026-10-21):
https://docs.novig.com/api-reference/history/get-a-position-from-history
https://docs.novig.com/api-reference/history/get-a-market-from-history
https://docs.novig.com/api-reference/execution/list-fills
https://docs.novig.com/api/concepts/fees
"""
from __future__ import annotations

from decimal import Decimal
import json
from urllib.parse import quote

from ..core.models import OrderStatus, utcnow
from .account_probe import novig_wallet_fingerprint
from .common import decimal_value
from .novig import NovigClient
from .reconciliation import ReconciliationError, _number, _object, _text, _time, normalize_novig_fill


def supports_settlement_scope(profile, adapter, mappings, managed_orders) -> bool:
    """Admit only explicitly authorized narrow Novig settlement capability.

    This is not account verification, successful settlement evidence, permission
    to trade or a promise that a future market cannot push/void. The explicit
    policy accepts an automation halt with retained exposure for every outcome
    outside ordinary WIN/LOSS, including remediation and netting. Those cases
    require audited cash repair. Callers checking a new intent must include it
    in managed_orders before transmission. Never omit settled historical orders.
    """
    if getattr(profile, "venue", None) == "kalshi":
        from .kalshi_settlement import supports_kalshi_settlement_scope
        return supports_kalshi_settlement_scope(profile, adapter, mappings, managed_orders)
    if getattr(profile, "venue", None) == "prophetx":
        from .prophetx_settlement import supports_prophetx_settlement_scope
        return supports_prophetx_settlement_scope(profile, adapter, mappings, managed_orders)
    # Local imports avoid the adapter/accounting module graph at import time.
    from ..core.readiness import LaunchProfile
    from .book_normalization import NativeBookMapper, NativeMarketMapping
    from .bridge import NativeExecutionVenue

    if (not isinstance(profile, LaunchProfile)
            or profile.settlement_policy != "novig_ioc_win_loss_halt_on_exception"
            or profile.venue != "novig" or not isinstance(adapter, NativeExecutionVenue)
            or not isinstance(adapter.client, NovigClient)
            or adapter.client.settings.environment != profile.environment):
        return False
    try:
        # Revalidate nested records rather than trusting model_copy mutations.
        reviewed = NativeBookMapper(tuple(NativeMarketMapping.model_validate(item.model_dump()) for item in mappings))
        requested = [item for item in reviewed.mappings
                     if item.instrument.venue == "novig" and item.market.id in profile.market_ids]
        if {item.market.id for item in requested} != set(profile.market_ids):
            return False
        now = utcnow()
        for item in requested:
            instrument = item.instrument
            execution = adapter.mappings.get((item.market.id, instrument.outcome))
            if (instrument.quantity_unit != "whole_cent_contracts"
                    or instrument.payout_per_unit_usd != Decimal(".01")
                    or instrument.quantity_step != 1 or instrument.price_format != "probability"
                    or not item.fee_bound.observed_at <= now < item.fee_bound.valid_until
                    or execution is None or not execution.verified or not execution.fee_schedule_verified
                    or execution.canonical_market_id != item.market.id
                    or execution.canonical_outcome != instrument.outcome
                    or execution.native_instrument_id != instrument.instrument_id
                    or execution.native_outcome != instrument.native_outcome
                    or execution.settlement_fingerprint != item.market.identity()
                    or execution.native_quantity_unit != instrument.quantity_unit
                    or execution.native_payout_usd != instrument.payout_per_unit_usd):
                return False
        # Position history settles an entire native market. More than one order
        # in that market, even after the first has settled or on another outcome,
        # requires a cash allocation algorithm that this policy does not claim.
        history = set()
        for order in managed_orders:
            if (order.venue != "novig" or order.mode != profile.environment
                    or order.account_fingerprint != profile.account_fingerprint):
                continue
            key = (order.account_fingerprint, order.intent.instrument.instrument_id)
            if key in history:
                return False
            history.add(key)
        return True
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


async def _pages(client, path, **filters):
    rows, cursor, seen = [], None, set()
    for _ in range(20):
        params = {**filters, "limit": 500, **({"after": cursor} if cursor else {})}
        page = _object(await client.request("GET", path, params=params), "settlement history page")
        if not isinstance(page.get("items"), list):
            raise ReconciliationError("Complete personal settlement history is required")
        rows.extend(page["items"])
        if "next" not in page:
            return rows
        cursor = page["next"]
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ReconciliationError("Invalid settlement history cursor")
        seen.add(cursor)
    raise ReconciliationError("Settlement history exceeds bounded reconciliation pages")


async def reconcile_novig_settlement(client, order, managed_orders):
    """Return a verified NativeSettlement, None while ungraded, or fail closed.

    This performs authenticated GETs only. Supply the entire durable native
    order inventory, including already settled orders. Identity, cash and
    source timestamps are checked anew; a saved launch profile is no proof.
    Recheck accepted settlements to catch venue remediation. Exceptions must
    retain exposure (and invalidate an existing settlement), never mark paid.
    """
    from ..core.native_oms import NativeSettlement

    if not isinstance(client, NovigClient) or order.venue != "novig":
        raise ReconciliationError("Only the Novig v3 settlement adapter is supported")
    instrument = order.intent.instrument
    if (order.mode != client.settings.environment or not order.venue_order_id or
            not order.reconciliation_complete or order.filled_quantity <= 0 or
            order.status not in {OrderStatus.FILLED, OrderStatus.CANCELLED} or
            instrument.quantity_unit != "whole_cent_contracts" or
            instrument.payout_per_unit_usd != Decimal(".01")):
        raise ReconciliationError("A terminal, fully reconciled native filled order is required")
    related = [o for o in managed_orders if o.venue == order.venue and o.mode == order.mode
               and o.account_fingerprint == order.account_fingerprint
               and o.intent.instrument.instrument_id == instrument.instrument_id]
    if len(related) != 1 or related[0].id != order.id:
        raise ReconciliationError("Shared native market positions cannot be allocated to one order")

    key = client.settings.api_key
    balance = _object(await client.request("GET", f"/v3/account/subaccounts/{quote(key, safe='')}/balance"), "balance")
    if balance.get("keyId") != key or novig_wallet_fingerprint(order.mode, key) != order.account_fingerprint:
        raise ReconciliationError("Settlement belongs to a different authenticated wallet")
    _number(balance.get("balance"), "authenticated balance")

    market = _object(await client.request("GET", f"/v3/history/markets/{quote(instrument.instrument_id, safe='')}"), "market history")
    if market.get("marketId") != instrument.instrument_id:
        raise ReconciliationError("Settlement market identity mismatch")
    status = market.get("status")
    if status in {"OPEN", "CLOSED"}:
        return None
    if status != "SETTLED":
        raise ReconciliationError("Unknown native settlement state")
    outcomes = market.get("outcomes")
    if not isinstance(outcomes, list):
        raise ReconciliationError("Settlement outcome evidence is missing")
    matched = [r for r in outcomes if _object(r, "settlement outcome").get("outcomeId") == instrument.native_outcome]
    if len(matched) != 1 or matched[0].get("status") not in {"WIN", "LOSS"}:
        raise ReconciliationError("Push, FMV or ambiguous grades require cash reconciliation")
    result = matched[0]["status"]
    settled_at = _time(market.get("settledTs"), milliseconds=True)
    if not order.created_at <= settled_at <= utcnow():
        raise ReconciliationError("Settlement source timestamp is outside the order lifetime")

    positions = await _pages(client, "/v3/history/positions", market=instrument.instrument_id)
    if len(positions) != 1:
        raise ReconciliationError("A unique full-market position history is required")
    position = _object(positions[0], "position history")
    position_id = _text(position.get("positionId"), "position ID")
    if (position.get("marketId") != instrument.instrument_id or
            position.get("outcomeId") != instrument.native_outcome or position.get("status") != "SETTLED" or
            position.get("result") != result or position.get("resettled") is not False or
            _time(position.get("settledTs"), milliseconds=True) != settled_at):
        raise ReconciliationError("Position grade changed or is not attributable to this order")
    quantity = _number(position.get("qty"), "settled quantity", positive=True, whole=True)
    cost = _number(position.get("cost"), "settled position cost", positive=True)
    payout = _number(position.get("payout"), "settled payout")
    # pnl is signed, but it is still required decimal evidence, never a model.
    if position.get("pnl") is None:
        raise ReconciliationError("Settled position P&L is missing")
    pnl = decimal_value(position["pnl"])
    if (quantity != order.filled_quantity or cost != order.filled_cost_usd or pnl != payout-cost or
            order.gross_payout_usd != quantity*Decimal(".01") or
            payout != (order.gross_payout_usd if result == "WIN" else Decimal(0))):
        raise ReconciliationError("Settled cash/quantity differs from the durable filled position")

    raw_fills = await _pages(client, "/v3/portfolio/fills", market=instrument.instrument_id)
    fills = {}
    for raw in raw_fills:
        row = _object(raw, "settled fill")
        if row.get("taker") is not True:
            raise ReconciliationError("Maker credits require an explicit cash-allocation adapter")
        fill = normalize_novig_fill(order.intent, row, expected_order_id=order.venue_order_id)
        if not order.created_at <= fill.filled_at <= settled_at:
            raise ReconciliationError("Settlement fill timestamp is outside the order lifetime")
        existing = fills.get(fill.fill_id)
        if (existing is not None and existing.model_dump(exclude={"observed_at"}) !=
                fill.model_dump(exclude={"observed_at"})):
            raise ReconciliationError("Conflicting settled fill history")
        fills[fill.fill_id] = fill
    zero = Decimal(0)
    if (not fills or sum((f.quantity for f in fills.values()), zero) != order.filled_quantity or
            sum((f.cost_usd for f in fills.values()), zero) != order.filled_cost_usd or
            sum((f.fee_usd for f in fills.values()), zero) != order.fees):
        raise ReconciliationError("Complete settled fills and actual execution fees must match the durable order")
    # Novig's documented trading fees are charged per fill. WIN/LOSS payout is
    # the contract cash value; no extra settlement fee/refund is inferred for
    # voids or remediation because those cases are deliberately unsupported.
    return NativeSettlement(order_id=order.id, venue="novig", environment=order.mode,
        account_fingerprint=order.account_fingerprint, market_fingerprint=order.market_fingerprint,
        settlement_id=f"novig:{position_id}:{market['settledTs']}:{result}", payout_usd=payout,
        additional_fee_usd=zero, fee_refund_usd=zero, settled_at=settled_at,
        evidence=json.dumps({"source": "novig.v3.personal.position_history", "position_id": position_id,
            "result": result, "quantity": str(quantity), "cost_usd": str(cost), "payout_usd": str(payout),
            "execution_fees_usd": str(order.fees), "fill_ids": sorted(fills)}, sort_keys=True))
