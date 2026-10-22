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

from cryptography.hazmat.primitives import serialization

from pydantic import BaseModel, ConfigDict

from ..core.models import OrderStatus, utcnow
from .novig import NovigClient
from .kalshi import KalshiClient
from .prophetx import ProphetXClient
from .common import VenueError, decimal_value
from .reconciliation import ReconciliationError, _number, _object, _text, _time, normalize_kalshi_order


class NativeAccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    venue: str
    environment: str
    account_fingerprint: str = ""
    identity_verified: bool = False
    observed_at: datetime
    balance_usd: Decimal | None = None
    cash_available_verified: bool = False
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
    # Kalshi private messages do not expose exchange sequence numbers. These
    # local invalidation fences must never be presented as source sequences.
    private_connection_generation: int | None = None
    private_revision: int | None = None
    private_as_of: datetime | None = None


def novig_wallet_fingerprint(environment: str, server_key_id: str) -> str:
    UUID(server_key_id)
    return hashlib.sha256(f"novig:{environment}:wallet-key:{server_key_id}".encode()).hexdigest()


def kalshi_account_fingerprint(environment: str, api_key_id: str) -> str:
    """Key-bound primary account identity; NOT stable across key rotation."""
    return hashlib.sha256(f"kalshi:{environment}:api-key:{api_key_id}:subaccount:0".encode()).hexdigest()


def _sequence(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReconciliationError("Invalid account snapshot sequence")
    return value


def _novig_trading_scope(client: NovigClient, metadata: dict) -> bool:
    """Match management-read key metadata to the actual local signing key."""
    if not isinstance(metadata, dict) or metadata.get("keyId") != client.settings.api_key:
        return False
    if metadata.get("scope") != "trading" or not isinstance(metadata.get("restriction"), str) or not metadata["restriction"]:
        return False
    expiry = metadata.get("expiresAt")
    if expiry is not None:
        try:
            if _time(expiry) <= utcnow():
                return False
        except ReconciliationError:
            return False
    public = client._load_key().public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return metadata.get("fingerprint") == "sha256:" + hashlib.sha256(public).hexdigest()


async def probe_native_account(client, managed_orders=(), *, settled_order_ids=frozenset(),
                               adapter=None, management_client=None) -> NativeAccountSnapshot:
    """No connection/login management; use an already authenticated client.

    Managed orders are the native OMS's durable records. Account snapshots are
    not atomic with each other: a mismatch is retried read-only, never accepted
    as zero. A returned reconciled=True is only current inventory agreement.
    """
    venue, environment = client.venue_id, client.settings.environment
    if isinstance(client, KalshiClient):
        return await _probe_kalshi(client, managed_orders, settled_order_ids)
    if isinstance(client, ProphetXClient):
        if not client._signin_confirmed or not client.health_check().get("authenticated"):
            return NativeAccountSnapshot(venue=venue, environment=environment, observed_at=utcnow(),
                reason="ProphetX confirmed authenticated private identity is required")
        from .prophetx_account import probe_prophetx_account
        return await probe_prophetx_account(client, managed_orders, settled_order_ids=settled_order_ids,
                                             review=getattr(client, "account_review", None))
    if client.venue_id == "polymarket_us":
        from .polymarket_us_account import probe_polymarket_us_account
        return await probe_polymarket_us_account(client, managed_orders,
            settled_order_ids=settled_order_ids, adapter=adapter)
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
    scope_verified = False
    if (management_client is not None and isinstance(management_client, NovigClient)
            and management_client.settings.environment == environment
            and management_client.settings.api_key != key_id):
        try:
            metadata = await management_client.request("GET", f"/v3/keys/{quote(key_id, safe='')}")
            scope_verified = _novig_trading_scope(client, metadata)
        except (ValueError, OSError, VenueError):
            pass
    permission_reason = "Authenticated Novig trading-key scope and signer are not verified" if not scope_verified else ""
    return NativeAccountSnapshot(venue=venue, environment=environment, account_fingerprint=fingerprint,
        identity_verified=True, observed_at=utcnow(), balance_usd=cash,
        open_order_count=len(orders), position_count=len(actual_positions), reconciled=not reasons,
        order_sequence=order_seq, position_sequence=position_seq,
        reason="; ".join(dict.fromkeys([*reasons, *([permission_reason] if permission_reason else [])])),
        api_key_scopes=("trading",) if scope_verified else (),
        trading_permission_verified=scope_verified,
        cash_available_verified=scope_verified and not reasons)


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
    private_fence = client.private.fence()
    private_barrier = client.private.barrier
    private_as_of = None
    if private_fence is not None:
        stamp = _object(await client.request("GET", "/exchange/user_data_timestamp"), "user data timestamp")
        private_as_of = _time(stamp.get("as_of_time"))
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
    fingerprint = kalshi_account_fingerprint(environment, key["api_key_id"])
    balance = _object(await client.request("GET", "/portfolio/balance", params={"subaccount": 0}), "balance")
    cash = _number(balance.get("balance_dollars"), "fixed-point balance")
    orders = await _kalshi_pages(client, "/portfolio/orders", "orders", subaccount=0, status="resting")
    positions = await _kalshi_pages(client, "/portfolio/positions", "market_positions", subaccount=0,
                                     settlement_status="unsettled", count_filter="position,total_traded")
    inventory = [o for o in managed_orders
                 if o.venue == "kalshi" and o.mode == environment and o.id not in settled_order_ids]
    owned = {o.venue_order_id: o for o in inventory if o.venue_order_id}
    reasons = []
    if any(o.account_fingerprint != fingerprint or not o.reconciliation_complete for o in inventory):
        reasons.append("Managed order account/financial reconciliation is incomplete")
    if len(owned) != sum(bool(o.venue_order_id) for o in inventory):
        reasons.append("Duplicate managed Kalshi native order identity")
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
        report = normalize_kalshi_order(order.intent, {"order": row}, expected_order_id=oid, observed_at=utcnow())
        financial = report.snapshot
        if (financial.cumulative_quantity != order.filled_quantity
                or financial.cumulative_cost_usd != order.filled_cost_usd
                or financial.cumulative_fee_usd != order.fees):
            reasons.append("Kalshi resting order financials differ from durable fills")
    if seen != {oid for oid, o in owned.items() if o.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}}:
        reasons.append("Native and local Kalshi resting inventory differs")
    expected = defaultdict(Decimal)
    expected_cost, expected_fees = defaultdict(Decimal), defaultdict(Decimal)
    expected_sides = defaultdict(set)
    for order in owned.values():
        if order.filled_quantity:
            ticker = order.intent.instrument.instrument_id
            expected[ticker] += order.filled_quantity * (1 if order.intent.instrument.native_outcome == "yes" else -1)
            expected_cost[ticker] += order.filled_cost_usd
            expected_fees[ticker] += order.fees
            expected_sides[ticker].add(order.intent.instrument.native_outcome)
    if any(len(sides) != 1 for sides in expected_sides.values()):
        reasons.append("Netted Kalshi exposure requires explicit cash allocation")
    actual = defaultdict(Decimal)
    seen_positions = set()
    for raw in positions:
        row = _object(raw, "position")
        ticker = _text(row.get("ticker"), "position ticker")
        identity = ticker, row.get("exchange_index")
        if identity in seen_positions or ticker in actual:
            raise ReconciliationError("Duplicate Kalshi account position")
        seen_positions.add(identity)
        if isinstance(row.get("position_fp"), bool):
            raise ReconciliationError("Invalid signed Kalshi position")
        quantity = decimal_value(row.get("position_fp"))
        if type(row.get("exchange_index")) is not int or row["exchange_index"] < 0:
            raise ReconciliationError("Invalid Kalshi exchange shard")
        actual[ticker] = quantity
        cost = _number(row.get("market_exposure_dollars"), "position cost")
        traded = _number(row.get("total_traded_dollars"), "position total traded")
        fees = _number(row.get("fees_paid_dollars"), "position fees")
        pnl = decimal_value(row.get("realized_pnl_dollars"))
        updated = _time(row.get("last_updated_ts"))
        if (cost != expected_cost[ticker] or traded != expected_cost[ticker]
                or fees != expected_fees[ticker] or pnl != 0 or updated > utcnow()):
            reasons.append("Kalshi position cash/fees or realized activity differ from managed buys")
    if dict(actual) != dict(expected):
        reasons.append("Unmanaged or netted Kalshi positions require review")
    private_reason = ""
    if private_fence is not None and (private_fence != client.private.fence()
            or private_barrier is None or private_as_of < private_barrier or private_as_of > utcnow()):
        # Inventory agreement and private freshness are separate facts. Normal
        # read-model lag must block placement via private.matches(), without
        # falsely classifying a coherent inventory as unexplained external cash.
        private_reason = "Kalshi private state changed or REST data has not reached the account barrier"
        private_fence, private_as_of = None, None
    return NativeAccountSnapshot(venue="kalshi", environment=environment, account_fingerprint=fingerprint,
        identity_verified=True, observed_at=utcnow(), balance_usd=cash, open_order_count=len(orders),
        position_count=len(actual), reconciled=not reasons,
        reason="; ".join(dict.fromkeys([*reasons, *([private_reason] if private_reason else [])])),
        # Official balance_dollars is available cash; portfolio_value is a
        # different field and is never funding capacity. Runtime still requires
        # a fresh synchronized snapshot and reserves concurrent order intents.
        cash_available_verified=not reasons,
        api_key_scopes=scopes, trading_permission_verified=trading_scope,
        geographic_attestation_expires_at=expiry,
        private_connection_generation=private_fence[0] if private_fence else None,
        private_revision=private_fence[1] if private_fence else None,
        private_as_of=private_as_of)
