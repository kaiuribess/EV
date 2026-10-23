"""Bounded GET-only retail inventory evidence, never an account attestation.

Retail balances/positions/orders have no documented stable account ID or API
key write-scope metadata. RFQ user ID identifies an authenticated *participant*,
not necessarily a cash account. Neither a configured key nor RFQ identity is
therefore promoted to a verified account fingerprint. These diagnostics remain
unreconciled until that contract is documented and complete inventory agrees.

Sources: docs.polymarket.us/api-reference/portfolio/overview,
api-reference/portfolio/get-user-positions, api-reference/orders/get-open-orders,
api-reference/account/get-account-balances, api-reference/rfqs/get-rfq-user-id.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from decimal import Decimal

from ..core.models import OrderStatus, utcnow
from .account_probe import NativeAccountSnapshot
from .polymarket_us import validated_slugs
from .polymarket_us_execution import amount, native_id, number, source_time, validate_order_identity
from .reconciliation import ReconciliationError


class PolymarketUSAccountSnapshot(NativeAccountSnapshot):
    # Diagnostic evidence only. These cannot replace identity_verified,
    # trading_permission_verified, or reconciled in the central launch gate.
    participant_fingerprint: str = ""
    inventory_quantities_agree: bool = False
    inventory_cash_agrees: bool = False


def _object(value, name):
    if not isinstance(value, dict):
        raise ReconciliationError(f"Invalid Polymarket US {name}")
    return value


def signed_quantity(value):
    if not isinstance(value, str):
        raise ReconciliationError("Exact netPositionDecimal string is required")
    try:
        result = Decimal(value)
    except ArithmeticError:
        raise ReconciliationError("Invalid Polymarket US net position") from None
    if not result.is_finite():
        raise ReconciliationError("Invalid Polymarket US net position")
    return result


async def read_all_positions(client):
    positions, cursors, cursor = {}, set(), None
    for _ in range(20):
        response = _object(await client.positions(limit=100, **({"cursor": cursor} if cursor else {})), "positions response")
        page = _object(response.get("positions"), "position map")
        if not isinstance(response.get("eof"), bool):
            raise ReconciliationError("Position snapshot has no explicit end-of-pages marker")
        for slug, position in page.items():
            validated_slugs([slug])
            if slug in positions:
                raise ReconciliationError("Repeated market in account position pages")
            positions[slug] = _object(position, "position")
        if response["eof"]:
            return positions
        cursor = response.get("nextCursor")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ReconciliationError("Missing or repeated account position cursor")
        cursors.add(cursor)
    raise ReconciliationError("Account position snapshot exceeds bounded page budget")


async def probe_polymarket_us_account(client, managed_orders=(), *, settled_order_ids=frozenset(),
                                     adapter=None, include_participant=False):
    """Return only documented account facts from an already connected client.

    Reading never creates a key, changes subscriptions, or submits an order.
    include_participant is optional because RFQ permission is independent. It
    records a hashed participant identity only; it cannot unlock trading.
    """
    started = utcnow()
    managed_orders = tuple(managed_orders)
    base = dict(venue="polymarket_us", environment=client.settings.environment, observed_at=started)
    if client.venue_id != "polymarket_us" or client.settings.environment != "production" or not client.authenticated:
        return PolymarketUSAccountSnapshot(**base, reason="Authenticated Polymarket US retail client required")
    fence = (getattr(adapter, "private_generation", None), getattr(adapter, "private_revision", None))
    reasons = ["Retail API does not attest a stable cash account identity, trading scope, or geographic eligibility"]
    balances = _object(await client.account_balances(), "balances").get("balances")
    if not isinstance(balances, list):
        raise ReconciliationError("Missing account balance collection")
    usd = [row for row in balances if _object(row, "currency balance").get("currency") == "USD"]
    if len(usd) != 1:
        raise ReconciliationError("Exactly one USD balance is required")
    number(usd[0].get("currentBalance"), "current USD balance")
    # The retail contract calls buyingPower capital available for trading;
    # currentBalance is only the fiat balance. Keep this reported capacity
    # unverified until account identity, clock and inventory can be certified.
    buying_power = number(usd[0].get("buyingPower"), "USD buying power")
    try:
        source_time(usd[0].get("lastUpdated"), utcnow())
        balance_clock_valid = True
    except ReconciliationError:
        # A real account response may omit the documented timestamp. Keep the
        # inventory diagnostic available, but never certify its cash agreement.
        balance_clock_valid = False
    raw_orders = _object(await client.open_orders(), "open orders").get("orders")
    if not isinstance(raw_orders, list):
        raise ReconciliationError("Missing complete open-order collection")
    positions = await read_all_positions(client)
    observed = utcnow()
    owned = {order.venue_order_id: order for order in managed_orders
             if order.venue == "polymarket_us" and order.mode == "production" and order.id not in settled_order_ids}
    inventory_reasons = [] if balance_clock_valid else ["Balance source clock is missing or invalid"]
    if any(not order.venue_order_id or not order.reconciliation_complete or order.status == OrderStatus.UNKNOWN for order in owned.values()):
        inventory_reasons.append("Managed order reconciliation is incomplete")
    if len(owned) != sum(1 for order in managed_orders
                        if order.venue == "polymarket_us" and order.mode == "production" and order.id not in settled_order_ids):
        inventory_reasons.append("Managed native order identities are duplicated")
    seen = set()
    for raw in raw_orders:
        row = _object(raw, "open order")
        oid = native_id(row.get("id"))
        if oid in seen:
            raise ReconciliationError("Duplicate Polymarket US open order")
        seen.add(oid)
        order = owned.get(oid)
        if order is None:
            inventory_reasons.append("Unmanaged native open order exists")
            continue
        validate_order_identity(order.intent, row, oid)
        if (number(row.get("cumQuantity"), "open order filled quantity") != order.filled_quantity
                or number(row.get("leavesQuantity"), "open order remaining quantity") != order.quantity-order.filled_quantity
                or row.get("state") not in {"ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED"}
                or order.status not in {OrderStatus.OPEN, OrderStatus.PARTIAL}):
            inventory_reasons.append("Open order differs from durable reconciled inventory")
    expected_open = {oid for oid, order in owned.items() if order.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}}
    if seen != expected_open:
        inventory_reasons.append("Native and local open-order inventories differ")
    expected_positions, expected_costs, outcomes = defaultdict(Decimal), defaultdict(Decimal), defaultdict(set)
    for order in owned.values():
        if order.filled_quantity:
            instrument = order.intent.instrument
            if instrument.native_outcome not in {"yes", "no"}:
                raise ReconciliationError("Unsupported managed native outcome")
            slug = instrument.instrument_id
            expected_positions[slug] += order.filled_quantity * (1 if instrument.native_outcome == "yes" else -1)
            expected_costs[slug] += order.filled_cost_usd
            outcomes[slug].add(instrument.native_outcome)
    if any(len(sides) > 1 for sides in outcomes.values()):
        inventory_reasons.append("Opposing managed positions cannot be certified by net quantities")
    actual_positions = {}
    for slug, position in positions.items():
        net = signed_quantity(position.get("netPositionDecimal"))
        # Deprecated rounded integer fields are deliberately ignored.
        cost = amount(position.get("cost"), "position cost basis")
        source_time(position.get("updateTime"), observed)
        if not isinstance(position.get("expired"), bool):
            raise ReconciliationError("Missing position expiration state")
        if net:
            actual_positions[slug] = net
            if position["expired"]:
                inventory_reasons.append("Expired position requires authoritative cash settlement reconciliation")
            # This mismatch is useful negative evidence. Even equality cannot
            # certify fee inclusion, short collateral accounting, or ownership.
            if cost != expected_costs.get(slug):
                inventory_reasons.append("Position cost basis differs from durable execution cost")
        elif cost:
            inventory_reasons.append("Zero-quantity position retains unexplained cost basis")
    if actual_positions != dict(expected_positions):
        inventory_reasons.append("Unmanaged, missing, or netted positions require review")
    private_current = bool(adapter and adapter.private_snapshot_complete and client._ready.get("private")
                           and fence == (adapter.private_generation, adapter.private_revision))
    if not private_current:
        inventory_reasons.append("Complete unchanged private order baseline is required")
    elif seen != {oid for oid, row in adapter.private_orders.items()
                   if row.get("state") in {"ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED", "ORDER_STATE_PENDING_CANCEL"}}:
        inventory_reasons.append("Private and REST open-order inventories differ")
    else:
        for row in raw_orders:
            private = adapter.private_orders.get(row["id"], {})
            fields = ("marketSlug", "side", "intent", "type", "tif", "price", "quantity", "cumQuantity", "leavesQuantity", "state",
                      "commissionNotionalTotalCollected")
            if any(private.get(field) != row.get(field) for field in fields):
                inventory_reasons.append("Private and REST order details differ")
    cash_agrees = not inventory_reasons and not actual_positions and not any(order.filled_quantity for order in owned.values())
    if not cash_agrees:
        reasons.append("Nonempty position cost basis and settlement cash semantics need verified accounting")
    participant = ""
    if include_participant:
        identity = _object(await client.authenticated_participant(), "participant").get("rfqUserId")
        if not isinstance(identity, str) or not identity.strip():
            raise ReconciliationError("Missing authenticated RFQ participant identity")
        participant = hashlib.sha256(f"polymarket_us:production:rfq-participant:{identity}".encode()).hexdigest()
        if adapter and fence != (adapter.private_generation, adapter.private_revision):
            private_current = False
            cash_agrees = False
            inventory_reasons.append("Private inventory changed during participant lookup")
    return PolymarketUSAccountSnapshot(**{**base, "observed_at": observed}, balance_usd=buying_power,
        open_order_count=len(raw_orders), position_count=len(actual_positions),
        participant_fingerprint=participant, inventory_quantities_agree=not inventory_reasons,
        inventory_cash_agrees=cash_agrees, identity_verified=False, cash_available_verified=False,
        reconciled=False, trading_permission_verified=False,
        private_connection_generation=fence[0], private_revision=fence[1],
        private_as_of=started if private_current else None,
        reason="; ".join(dict.fromkeys(reasons+inventory_reasons)))
