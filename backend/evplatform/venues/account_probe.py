"""Read-only authenticated account facts, never a placement authorization.

Novig's active trading key addresses one wallet. Its balance response echoes
that key; account orders and positions are complete snapshots with independent
sequences. We compare the inventory to our durable managed orders. Balance
alone is insufficient, and unknown external positions always block trading.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ..core.models import OrderStatus, utcnow
from .novig import NovigClient
from .kalshi import KalshiClient
from .prophetx import ProphetXClient
from .common import decimal_value
from .reconciliation import ReconciliationError, _number, _object, _text


class NativeAccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    venue: str
    environment: str
    account_fingerprint: str = ""
    identity_verified: bool = False
    observed_at: datetime
    balance_usd: Decimal | None = None
    open_order_count: int | None = None
    position_count: int | None = None
    reconciled: bool = False
    order_sequence: int | None = None
    position_sequence: int | None = None
    reason: str = ""
    # Only explicit authenticated key metadata can establish these facts; a
    # successful balance read alone proves neither write scope nor eligibility.
    api_key_scopes: tuple[str, ...] = ()
    trading_permission_verified: bool = False
    geographic_attestation_expires_at: datetime | None = None


def novig_wallet_fingerprint(environment: str, server_key_id: str) -> str:
    UUID(server_key_id)
    return hashlib.sha256(f"novig:{environment}:wallet-key:{server_key_id}".encode()).hexdigest()


def _sequence(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReconciliationError("Invalid account snapshot sequence")
    return value


async def probe_native_account(client, managed_orders=(), *, settled_order_ids=frozenset()) -> NativeAccountSnapshot:
    """No connection/login management; use an already authenticated client.

    Managed orders are the native OMS's durable records. Account snapshots are
    not atomic with each other: a mismatch is retried read-only, never accepted
    as zero. A returned reconciled=True is only current inventory agreement.
    """
    venue, environment = client.venue_id, client.settings.environment
    if isinstance(client, KalshiClient):
        return await _probe_kalshi(client, managed_orders, settled_order_ids)
    if isinstance(client, ProphetXClient):
        # The server-signed registration identity is available only after the
        # private sign-in succeeds; balance alone carries no identity field.
        user_id = getattr(client, "_authenticated_user_id", "")
        if not client._signin_confirmed or not user_id:
            return NativeAccountSnapshot(venue=venue, environment=environment, observed_at=utcnow(),
                reason="ProphetX confirmed private account identity is required")
        data = _object(_object(await client.request("GET", "/v4/mm/get_balance"), "balance").get("data"), "balance data")
        return NativeAccountSnapshot(venue=venue, environment=environment, observed_at=utcnow(),
            account_fingerprint=hashlib.sha256(f"prophetx:{environment}:user:{user_id}".encode()).hexdigest(),
            identity_verified=True, balance_usd=_number(data.get("balance"), "account balance"),
            reconciled=False, reason="Complete ProphetX account position and actual-fee inventory reconciliation is required")
    if not isinstance(client, NovigClient):
        return NativeAccountSnapshot(venue=venue, environment=environment, observed_at=utcnow(),
            reason="Authenticated account identity/inventory probe is not implemented for this venue")
    key_id = client.settings.api_key
    UUID(key_id)
    # Personal subaccount reads intentionally omit keyId selectors: a
    # management key's multi-wallet envelope must not masquerade as one wallet.
    balance = _object(await client.request("GET", f"/v3/account/subaccounts/{quote(key_id, safe='')}/balance"), "balance")
    if balance.get("keyId") != key_id:
        raise ReconciliationError("Authenticated wallet key does not match configured client")
    fingerprint = novig_wallet_fingerprint(environment, balance["keyId"])
    cash = _number(balance.get("balance"), "account balance")
    raw_orders = _object(await client.request("GET", "/v3/account/orders"), "orders")
    raw_positions = _object(await client.request("GET", "/v3/account/positions"), "positions")
    orders, positions = raw_orders.get("open"), raw_positions.get("positions")
    if not isinstance(orders, list) or not isinstance(positions, list):
        raise ReconciliationError("Expected complete personal account snapshots")
    order_seq, position_seq = _sequence(raw_orders.get("seq")), _sequence(raw_positions.get("seq"))
    reasons = []
    owned = {o.venue_order_id: o for o in managed_orders
             if o.venue == venue and o.mode == environment and o.id not in settled_order_ids}
    if any(o.account_fingerprint != fingerprint for o in owned.values()):
        reasons.append("Managed native order belongs to a different wallet")
    if any(not o.reconciliation_complete or o.status == OrderStatus.UNKNOWN for o in owned.values()):
        reasons.append("Managed order reconciliation is incomplete")
    seen = set()
    for raw in orders:
        row = _object(raw, "resting order")
        oid = _text(row.get("orderId"), "native order ID")
        if oid in seen:
            raise ReconciliationError("Duplicate native resting order")
        seen.add(oid)
        order = owned.get(oid)
        qty = _number(row.get("qty"), "resting quantity", positive=True, whole=True)
        price = _number(row.get("price"), "resting price", positive=True)
        if order is None:
            reasons.append("Unmanaged native resting order exists")
            continue
        instrument = order.intent.instrument
        if (row.get("clientId") != order.client_order_id or row.get("marketId") != instrument.instrument_id
                or row.get("outcomeId") != instrument.native_outcome or price != order.intent.limit_price
                or qty != order.quantity-order.filled_quantity
                or order.status not in {OrderStatus.OPEN, OrderStatus.PARTIAL}):
            reasons.append("Native resting order differs from durable intent")
    expected_resting = {oid for oid, o in owned.items() if o.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}}
    if seen != expected_resting:
        reasons.append("Native and local resting order inventories differ")
    expected_positions = defaultdict(Decimal)
    expected_costs = defaultdict(Decimal)
    for order in owned.values():
        if order.filled_quantity:
            i = order.intent.instrument
            expected_positions[(i.instrument_id, i.native_outcome)] += order.filled_quantity
            expected_costs[(i.instrument_id, i.native_outcome)] += order.filled_cost_usd
    actual_positions = {}
    for raw in positions:
        row = _object(raw, "position")
        identity = (_text(row.get("marketId"), "market ID"), _text(row.get("outcomeId"), "outcome ID"))
        qty = _number(row.get("qty"), "position quantity", whole=True)
        cost = _number(row.get("cost"), "position cost")
        if identity in actual_positions:
            raise ReconciliationError("Duplicate native account position")
        if qty:
            actual_positions[identity] = qty
            if cost != expected_costs.get(identity):
                reasons.append("Native position cost differs from the reconciled fills")
        elif cost:
            reasons.append("Zero-quantity position retains unexplained cash cost")
    if actual_positions != dict(expected_positions):
        reasons.append("Unmanaged, missing or netted native positions require review")
    return NativeAccountSnapshot(venue=venue, environment=environment, account_fingerprint=fingerprint,
        identity_verified=True, observed_at=utcnow(), balance_usd=cash,
        open_order_count=len(orders), position_count=len(actual_positions), reconciled=not reasons,
        order_sequence=order_seq, position_sequence=position_seq,
        reason="; ".join(dict.fromkeys(reasons)))


async def _kalshi_pages(client, path, collection, **params):
    rows, cursor, seen = [], None, set()
    for _ in range(20):
        request_params = {**params, "limit": 200, **({"cursor": cursor} if cursor else {})}
        page = _object(await client.request("GET", path, params=request_params), "account page")
        if not isinstance(page.get(collection), list):
            raise ReconciliationError("Incomplete Kalshi account inventory")
        rows.extend(page[collection])
        cursor = page.get("cursor")
        if cursor == "":
            return rows
        if not isinstance(cursor, str) or cursor in seen:
            raise ReconciliationError("Invalid Kalshi account pagination")
        seen.add(cursor)
    raise ReconciliationError("Kalshi account inventory exceeds reconciliation page bound")


async def _probe_kalshi(client, managed_orders, settled_order_ids):
    environment = client.settings.environment
    response = _object(await client.request("GET", "/api_keys"), "authenticated keys")
    keys = response.get("api_keys")
    if not isinstance(keys, list):
        raise ReconciliationError("Authenticated key listing is unavailable")
    matching = [row for row in keys if _object(row, "key").get("api_key_id") == client.settings.api_key_id]
    if len(matching) != 1:
        raise ReconciliationError("Authenticated key identity was not confirmed")
    key = matching[0]
    scopes = key.get("scopes", [])
    if (not isinstance(scopes, list) or
            any(not isinstance(scope, str) or not scope.strip() for scope in scopes)):
        raise ReconciliationError("Invalid authenticated Kalshi key scopes")
    scopes = tuple(sorted(set(scopes)))
    # Official GET /api_keys returns Unix seconds, not milliseconds. Preserve
    # expired timestamps as expired evidence; absent/null is not verification.
    expiry = response.get("api_key_region_expiration_ts")
    if expiry is not None:
        if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < 0:
            raise ReconciliationError("Invalid Kalshi location attestation expiry")
        try:
            expiry = datetime.fromtimestamp(expiry, timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise ReconciliationError("Invalid Kalshi location attestation expiry") from None
    trading_scope = "write::trade" in scopes or {"read", "write"}.issubset(scopes)
    subaccount = key.get("subaccount")
    # Unrestricted keys route to the primary account unless an explicit native
    # account option is supplied. This bridge currently routes primary only.
    if subaccount is None:
        subaccount = 0
    if isinstance(subaccount, bool) or not isinstance(subaccount, int) or subaccount != 0 or key.get("fcm_subtrader_id"):
        return NativeAccountSnapshot(venue="kalshi", environment=environment, observed_at=utcnow(),
            reason="Kalshi non-primary or FCM execution account routing is not implemented")
    fingerprint = hashlib.sha256(f"kalshi:{environment}:api-key:{key['api_key_id']}:subaccount:0".encode()).hexdigest()
    balance = _object(await client.request("GET", "/portfolio/balance", params={"subaccount": 0}), "balance")
    cash = _number(balance.get("balance_dollars"), "fixed-point balance")
    orders = await _kalshi_pages(client, "/portfolio/orders", "orders", subaccount=0, status="resting")
    positions = await _kalshi_pages(client, "/portfolio/positions", "market_positions", subaccount=0,
                                     settlement_status="unsettled", count_filter="position")
    owned = {o.venue_order_id: o for o in managed_orders
             if o.venue == "kalshi" and o.mode == environment and o.id not in settled_order_ids}
    reasons = []
    if any(o.account_fingerprint != fingerprint or not o.reconciliation_complete for o in owned.values()):
        reasons.append("Managed order account/financial reconciliation is incomplete")
    seen = set()
    for raw in orders:
        row = _object(raw, "order")
        oid = _text(row.get("order_id"), "order ID")
        if oid in seen:
            raise ReconciliationError("Duplicate Kalshi account order")
        seen.add(oid)
        order = owned.get(oid)
        if order is None:
            reasons.append("Unmanaged Kalshi resting order exists")
            continue
        instrument = order.intent.instrument
        if (row.get("status") != "resting" or row.get("action") != "buy"
                or row.get("ticker") != instrument.instrument_id
                or row.get("outcome_side", row.get("side")) != instrument.native_outcome
                or row.get("client_order_id") != order.client_order_id
                or row.get("subaccount_number") != 0
                or _number(row.get("remaining_count_fp"), "remaining contracts") != order.quantity-order.filled_quantity):
            reasons.append("Kalshi resting order differs from durable intent")
    if seen != {oid for oid, o in owned.items() if o.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}}:
        reasons.append("Native and local Kalshi resting inventory differs")
    expected = defaultdict(Decimal)
    for order in owned.values():
        if order.filled_quantity:
            expected[order.intent.instrument.instrument_id] += order.filled_quantity * (1 if order.intent.instrument.native_outcome == "yes" else -1)
    actual = defaultdict(Decimal)
    seen_positions = set()
    for raw in positions:
        row = _object(raw, "position")
        ticker = _text(row.get("ticker"), "position ticker")
        identity = ticker, row.get("exchange_index")
        if identity in seen_positions:
            raise ReconciliationError("Duplicate Kalshi account position")
        seen_positions.add(identity)
        if isinstance(row.get("position_fp"), bool):
            raise ReconciliationError("Invalid signed Kalshi position")
        quantity = decimal_value(row.get("position_fp"))
        if quantity:
            actual[ticker] += quantity
    if dict(actual) != dict(expected):
        reasons.append("Unmanaged or netted Kalshi positions require review")
    return NativeAccountSnapshot(venue="kalshi", environment=environment, account_fingerprint=fingerprint,
        identity_verified=True, observed_at=utcnow(), balance_usd=cash, open_order_count=len(orders),
        position_count=len(actual), reconciled=not reasons, reason="; ".join(dict.fromkeys(reasons)),
        api_key_scopes=scopes, trading_permission_verified=trading_scope,
        geographic_attestation_expires_at=expiry)
