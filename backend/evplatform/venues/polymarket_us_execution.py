"""Risk-gated Polymarket US retail IOC purchases and exact fill evidence.

Official retail sources (not international CLOB/institutional APIs):
https://docs.polymarket.us/api-reference/orders/create-order
https://docs.polymarket.us/api-reference/orders/get-order
https://docs.polymarket.us/api-reference/orders/cancel-order
https://docs.polymarket.us/api-reference/websocket/private
https://docs.polymarket.us/concepts/orders
https://docs.polymarket.us/fees

The retail API has no documented client order ID/idempotent placement or
per-order execution replay. Unknown sends without a returned native ID cannot
be safely resolved by matching similar orders. Average price is never treated
as exact cumulative cash. Keep the monitor's execution launch gates closed
until account, mapping, fee, settlement, and central risk evidence are valid.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_EVEN, localcontext

from cryptography.hazmat.primitives import serialization

from ..core.models import utcnow
from ..core.native_accounting import NativeAccounting, NativeCumulativeSnapshot, NativeFill, NativeOrderIntent, merge_native_fill_metadata
from ..core.plugins import Manifest, UnsupportedOperation
from .bridge import NativePreflight
from .common import NativeTradingGate, PreflightRequired, UnknownOrderOutcome, VenueError, VenueHTTPError
from .polymarket_us import alias, validated_slugs
from .reconciliation import NativeCancelReport, NativeOrderReport, ReconciliationError

D = Decimal
IOC = "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
FEE_SOURCE = "https://docs.polymarket.us/fees"


def number(value, name, *, positive=False):
    if isinstance(value, (float, bool)) or value is None:
        raise ReconciliationError(f"Missing or inexact {name}")
    try:
        result = D(value)
    except (TypeError, ValueError, ArithmeticError):
        raise ReconciliationError(f"Invalid {name}") from None
    if not result.is_finite() or result < 0 or (positive and not result):
        raise ReconciliationError(f"Invalid {name}")
    return result


def amount(value, name):
    if not isinstance(value, dict) or value.get("currency") != "USD":
        raise ReconciliationError(f"Missing USD {name}")
    return number(value.get("value"), name)


def source_time(value, observed_at):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result > observed_at:
            raise ValueError("Invalid source clock")
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise ReconciliationError("Missing or future Polymarket US execution clock") from None


def native_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ReconciliationError("Invalid Polymarket US native order identity")
    return value


def check_instrument(intent):
    i = intent.instrument
    if (i.venue != "polymarket_us" or i.native_outcome not in {"yes", "no"} or
            i.quantity_unit != "contracts" or i.price_format != "probability" or i.payout_per_unit_usd != 1 or
            i.quantity_step not in {D("0.01"), D(1)} or intent.mode != "production"):
        raise PreflightRequired("Unsupported Polymarket US retail contract")
    validated_slugs([i.instrument_id])
    if i.instrument_id.startswith("caoc-"):
        raise PreflightRequired("Combo/RFQ execution needs separate fee and settlement contracts")


def yes_price(intent, price=None):
    value = intent.limit_price if price is None else price
    return value if intent.instrument.native_outcome == "yes" else D(1) - value


def order_body(intent: NativeOrderIntent) -> dict:
    check_instrument(intent)
    return {"marketSlug": intent.instrument.instrument_id,
            "intent": "ORDER_INTENT_BUY_LONG" if intent.instrument.native_outcome == "yes" else "ORDER_INTENT_BUY_SHORT",
            "type": "ORDER_TYPE_LIMIT", "price": {"value": format(yes_price(intent), "f"), "currency": "USD"},
            "quantity": intent.quantity, "tif": IOC, "participateDontInitiate": False,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC", "synchronousExecution": False}


def sdk_preview_request(intent: NativeOrderIntent) -> dict:
    """Adapt exact local decimals to SDK 2.4.1's numeric quantity field.

    The SDK uses JSON serialization and accepts int/float quantities, whereas
    the guarded placement transport emits Decimal as an exact JSON number.
    Refuse a preview if that conversion changes the decimal quantity.
    """
    body = order_body(intent)
    quantity = body["quantity"]
    sdk_quantity = int(quantity) if quantity == quantity.to_integral_value() else float(quantity)
    if D(str(sdk_quantity)) != quantity:
        raise PreflightRequired("Polymarket US SDK cannot preview this exact quantity")
    return {**body, "quantity": sdk_quantity}


def validate_preview(intent: NativeOrderIntent, response: dict) -> dict:
    """A venue preview validates parameters; it is never a fill or fee quote."""
    order = response.get("order") if isinstance(response, dict) else None
    if not isinstance(order, dict):
        raise PreflightRequired("Polymarket US preview omitted the order")
    expected_intent = "ORDER_INTENT_BUY_LONG" if intent.instrument.native_outcome == "yes" else "ORDER_INTENT_BUY_SHORT"
    expected_side = "ORDER_SIDE_BUY" if intent.instrument.native_outcome == "yes" else "ORDER_SIDE_SELL"
    try:
        quantity = D(str(order.get("quantity")))
        price = amount(order.get("price"), "preview limit")
    except (ArithmeticError, ReconciliationError, ValueError, TypeError):
        raise PreflightRequired("Polymarket US preview has invalid price or quantity") from None
    if (not quantity.is_finite() or quantity != intent.quantity or price != yes_price(intent)
            or order.get("marketSlug") != intent.instrument.instrument_id
            or order.get("intent") != expected_intent or order.get("side") != expected_side
            or order.get("outcomeSide") != f"OUTCOME_SIDE_{intent.instrument.native_outcome.upper()}"
            or order.get("type") != "ORDER_TYPE_LIMIT" or order.get("tif") != IOC
            or order.get("state") not in {"ORDER_STATE_PENDING_NEW", "ORDER_STATE_PENDING_RISK", "ORDER_STATE_NEW"}):
        raise PreflightRequired("Polymarket US preview differs from reviewed order terms")
    return {"market_slug": order["marketSlug"], "intent": expected_intent,
            "state": order.get("state"), "parameter_check_only": True}


def encode_exact_json(value) -> bytes:
    """Emit documented JSON numbers without converting native decimals to float."""
    if isinstance(value, D):
        if not value.is_finite():
            raise ValueError("Nonfinite JSON number")
        return format(value, "f").encode()
    if isinstance(value, dict):
        return b"{" + b",".join(json.dumps(key).encode() + b":" + encode_exact_json(item) for key, item in value.items()) + b"}"
    if isinstance(value, list):
        return b"[" + b",".join(encode_exact_json(item) for item in value) + b"]"
    if isinstance(value, float):
        raise ValueError("Native JSON cannot contain binary floats")
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode()


def standard_taker_fee(quantity, price, *, theta):
    """Current documented formula; callers must supply verified market theta."""
    q, p, coefficient = number(quantity, "quantity", positive=True), number(price, "price", positive=True), number(theta, "theta", positive=True)
    if p >= 1:
        raise ValueError("Fee price must be below one")
    with localcontext() as context:
        context.prec = 100
        return (coefficient * q * p * (1-p)).quantize(D("0.01"), rounding=ROUND_HALF_EVEN)


def conservative_taker_fee_bound(quantity, limit_price, *, theta):
    """Bound all price improvements and cumulative aggressive-fill rounding.

    For an outcome purchase <= limit, uncertainty is greatest at min(limit,.5).
    Weekly rebates are excluded. Market-specific theta and expiry belong to the
    reviewed NativeFeeBound; this helper cannot verify a fee schedule itself.
    """
    q, p, coefficient = number(quantity, "quantity", positive=True), number(limit_price, "price", positive=True), number(theta, "theta", positive=True)
    if p >= 1:
        raise ValueError("Fee price must be below one")
    with localcontext() as context:
        context.prec = 100
        peak = min(p, D("0.5"))
        return (coefficient * q * peak * (1-peak)).quantize(D("0.01"), rounding=ROUND_CEILING)


def validate_order_identity(intent, order, expected_order_id):
    check_instrument(intent)
    if not isinstance(order, dict) or native_id(order.get("id")) != native_id(expected_order_id):
        raise ReconciliationError("Polymarket US order identity mismatch")
    side = "ORDER_SIDE_BUY" if intent.instrument.native_outcome == "yes" else "ORDER_SIDE_SELL"
    expected_intent = "ORDER_INTENT_BUY_LONG" if intent.instrument.native_outcome == "yes" else "ORDER_INTENT_BUY_SHORT"
    if (order.get("marketSlug") != intent.instrument.instrument_id or order.get("side") != side or
            order.get("intent") != expected_intent or order.get("type") != "ORDER_TYPE_LIMIT" or order.get("tif") != IOC or
            amount(order.get("price"), "order limit") != yes_price(intent) or number(order.get("quantity"), "initial quantity") != intent.quantity):
        raise ReconciliationError("Polymarket US order differs from durable intent")
    if "outcomeSide" in order and order["outcomeSide"] != f"OUTCOME_SIDE_{intent.instrument.native_outcome.upper()}":
        raise ReconciliationError("Conflicting Polymarket US outcome")
    if "action" in order and order["action"] != "ORDER_ACTION_BUY":
        raise ReconciliationError("External order action is unsupported")
    return order


def normalize_execution(intent, execution, *, expected_order_id, observed_at) -> NativeFill:
    if not isinstance(execution, dict) or execution.get("type") not in {"EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"}:
        raise ReconciliationError("Expected actual Polymarket US execution")
    order = validate_order_identity(intent, execution.get("order"), expected_order_id)
    if execution.get("aggressor") is not True or execution.get("legPrices"):
        raise ReconciliationError("Only ordinary IOC taker executions are supported")
    quantity = number(execution.get("lastShares"), "fill quantity", positive=True)
    native_price = amount(execution.get("lastPx"), "execution price")
    price = native_price if intent.instrument.native_outcome == "yes" else D(1)-native_price
    intent.instrument.validate_quantity(quantity)
    if not intent.instrument.price_satisfies_limit(price, intent.limit_price):
        raise ReconciliationError("Execution violates the durable price limit")
    cumulative = number(order.get("cumQuantity"), "cumulative quantity", positive=True)
    if not quantity <= cumulative <= intent.quantity:
        raise ReconciliationError("Invalid execution quantity watermark")
    fill = NativeFill(fill_id=native_id(execution.get("id")), order_id=expected_order_id,
        trade_id=native_id(execution["tradeId"]) if "tradeId" in execution else None,
        client_order_id=intent.client_order_id, quantity=quantity, price=price,
        cost_usd=intent.instrument.cost_for(quantity, price), gross_payout_usd=quantity,
        fee_usd=amount(execution.get("commissionNotionalCollected"), "actual execution commission"),
        filled_at=source_time(execution.get("transactTime"), observed_at), observed_at=observed_at,
        source="polymarket_us.execution", cumulative_quantity_after=cumulative)
    return fill


def normalize_order(intent, payload, *, expected_order_id, observed_at, fills=()) -> NativeOrderReport:
    order = validate_order_identity(intent, payload.get("order") if isinstance(payload, dict) else None, expected_order_id)
    filled = number(order.get("cumQuantity"), "cumulative quantity")
    remaining = number(order.get("leavesQuantity"), "remaining quantity")
    if filled + remaining > intent.quantity:
        raise ReconciliationError("Order quantities exceed durable intent")
    state = order.get("state")
    if state == "ORDER_STATE_FILLED" and filled == intent.quantity and remaining == 0:
        status = "filled"
    elif state in {"ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED"} and remaining == 0:
        status = "cancelled"
    elif state == "ORDER_STATE_REJECTED" and filled == remaining == 0:
        status = "rejected"
    elif state in {"ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED"} and remaining > 0 and filled+remaining == intent.quantity:
        status = "partial" if filled else "open"
    elif state in {"ORDER_STATE_PENDING_NEW", "ORDER_STATE_PENDING_RISK", "ORDER_STATE_PENDING_CANCEL"}:
        status = "cancel_pending" if state == "ORDER_STATE_PENDING_CANCEL" else "unknown"
    else:
        raise ReconciliationError("Unsupported or inconsistent Polymarket US order state")
    journal = NativeAccounting(intent, expected_order_id)
    ordered = sorted(fills, key=lambda f: (f.cumulative_quantity_after or D(0), f.fill_id))
    seen = set()
    for fill in ordered:
        if fill.fill_id in seen:
            raise ReconciliationError("Duplicate execution in reconciliation history")
        seen.add(fill.fill_id)
        if fill.cumulative_quantity_after != journal.totals.quantity + fill.quantity:
            raise ReconciliationError("Execution history has a missing or conflicting quantity watermark")
        journal.apply_fill(fill)
    if journal.totals.quantity > filled:
        raise ReconciliationError("REST cumulative quantity regressed behind execution evidence")
    terminal = status in {"filled", "cancelled", "rejected"}
    commission = amount(order.get("commissionNotionalTotalCollected"), "actual cumulative commission")
    complete = journal.totals.quantity == filled and journal.totals.fee_usd == commission
    snapshot = None
    if complete:
        totals = journal.totals
        snapshot = NativeCumulativeSnapshot(order_id=expected_order_id, client_order_id=intent.client_order_id,
            cumulative_quantity=totals.quantity, cumulative_cost_usd=totals.cost_usd,
            cumulative_gross_payout_usd=totals.gross_payout_usd, cumulative_fee_usd=totals.fee_usd,
            observed_at=observed_at, status=status, authoritative=True, source="polymarket_us.rest_order_with_executions")
    return NativeOrderReport(native_order_id=expected_order_id, client_order_id=intent.client_order_id,
        status=status, remaining_quantity=remaining, terminal=terminal, financial_complete=complete,
        snapshot=snapshot, fills=tuple(ordered),
        reason="" if complete else "Exact executions or actual cumulative commissions are incomplete; retain exposure")


class PolymarketUSExecutionVenue:
    """Central-OMS-only execution adapter; no legacy/synthetic order pathway."""

    def __init__(self, client, authority, *, mappings=(), preflight=None):
        self.client, self.authority = client, authority
        self.preflight = preflight or NativePreflight(environment="production")
        self.mappings = {(m.canonical_market_id, m.canonical_outcome): m for m in mappings}
        self._lock = asyncio.Lock()
        self._transmitted = set()
        self._native_intents = {}
        self._fills = {}
        self._unbound_executions = {}
        self._scope = self._credential_scope()
        self.dispatch_guard = None
        self.private_generation = 0
        self.private_revision = 0
        self.private_snapshot_complete = False
        self.private_orders = {}
        self.manifest = Manifest(name="polymarket_us_execution", kind="execution",
            description="Polymarket US IOC YES/NO purchases; exact execution reconciliation required",
            transports=["rest", "websocket"], authentication=["Ed25519 personal US retail API"],
            capabilities={"limit_orders": True, "ioc": True, "cancel": True, "client_order_id": False,
                          "fill_replay": False, "settlement_reconciliation": False, "auto_enabled": False},
            permissions=["network", "secret:polymarket_us", "orders:conditional"],
            documentation=["https://docs.polymarket.us/api-reference/orders/create-order"])

    def _credential_scope(self):
        public = self.client._load_key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return hashlib.sha256(self.client.settings.environment.encode()+b":"+self.client.settings.api_key_id.encode()+b":"+public
                              +b":"+self.client.settings.secret_key.encode()).hexdigest()

    def _scope_check(self):
        if not self.client.authenticated or self._credential_scope() != self._scope:
            raise PreflightRequired("Polymarket US authenticated credential scope changed or is unavailable")

    def _preflight_check(self, intent):
        p = self.preflight
        if (not p.native_trading_enabled or not p.account_authorized or not p.geographic_eligibility_verified or
                not p.evidence or p.valid_until <= time.time() or p.environment != intent.mode):
            raise PreflightRequired("Current native account, geographic and trading preflight required")

    def _mapping(self, intent):
        check_instrument(intent)
        i = intent.instrument
        mapping = self.mappings.get((i.canonical_market_id, i.outcome))
        if (mapping is None or not mapping.verified or not mapping.fee_schedule_verified or
                mapping.native_instrument_id != i.instrument_id or mapping.native_outcome != i.native_outcome or
                mapping.native_quantity_unit != i.quantity_unit or mapping.native_payout_usd != i.payout_per_unit_usd or
                mapping.settlement_fingerprint != i.settlement_rules_fingerprint):
            raise PreflightRequired("Reviewed Polymarket US mapping and fee evidence required")

    def build_native_request(self, intent):
        self._mapping(intent)
        return order_body(intent)

    async def preview_native_request(self, intent):
        self._scope_check()
        self._mapping(intent)
        return validate_preview(intent, await self.client.preview_order(sdk_preview_request(intent)))

    def bind_native_intent(self, order_id, intent):
        self._mapping(intent)
        native_id(order_id)
        existing = self._native_intents.get(order_id)
        if existing is not None and existing != intent:
            raise ReconciliationError("Native order is already bound to another durable intent")
        if any(known.client_order_id == intent.client_order_id and key != order_id for key, known in self._native_intents.items()):
            raise ReconciliationError("Local intent is already bound to another native order")
        self._native_intents[order_id] = intent
        self._transmitted.add(intent.client_order_id)
        pending = self._unbound_executions.get(order_id, {})
        for execution, observed_at in pending.values():
            self._remember_fill(order_id, normalize_execution(intent, execution,
                expected_order_id=order_id, observed_at=observed_at))
        self._unbound_executions.pop(order_id, None)

    def seed_fills(self, order_id, intent, fills):
        """Restore exact previously journaled evidence, never inferred totals."""
        self.bind_native_intent(order_id, intent)
        for fill in fills:
            if fill.order_id != order_id or fill.client_order_id != intent.client_order_id or fill.source != "polymarket_us.execution":
                raise ReconciliationError("Restored execution identity/source mismatch")
            self._remember_fill(order_id, fill)

    def execution_fills(self, order_id, intent):
        """Validated evidence for immediate durable journaling by the OMS."""
        if self._native_intents.get(order_id) != intent:
            raise PreflightRequired("Exact native intent binding required for execution evidence")
        return tuple(self._fills.get(order_id, {}).values())

    def _remember_fill(self, order_id, fill):
        old = self._fills.get(order_id, {}).get(fill.fill_id)
        if old is not None:
            try:
                fill = merge_native_fill_metadata(old, fill)
            except ValueError:
                raise ReconciliationError("Conflicting Polymarket US execution identity") from None
        self._fills.setdefault(order_id, {})[fill.fill_id] = fill

    async def _post(self, path, body, intent, *, placement):
        self._scope_check()
        if placement:
            self.client.settings.gate.require(intent.client_order_id, intent.mode)
            if path != "/v1/orders":
                raise PreflightRequired("Unsupported Polymarket US order route")
        elif not re.fullmatch(r"/v1/order/[A-Za-z0-9_-]{1,128}/cancel", path):
            raise PreflightRequired("Unsupported Polymarket US cancellation route")
        stamp = str(time.time_ns()//1_000_000)
        signature = self.client._load_key().sign(f"{stamp}POST{path}".encode())
        headers = {"X-PM-Access-Key": self.client.settings.api_key_id, "X-PM-Timestamp": stamp,
                   "X-PM-Signature": base64.b64encode(signature).decode(), "Content-Type": "application/json"}
        request = self.client.http.build_request("POST", self.client.PRIVATE_HOST+path, headers=headers, content=encode_exact_json(body))
        def before_send():
            self._scope_check()
            if placement:
                self._preflight_check(intent)
                self.client.settings.gate.require(intent.client_order_id, intent.mode)
                if self.dispatch_guard is not None:
                    self.dispatch_guard(intent)
            # Sign after transport pacing; a queued request must not use an
            # expired 30-second retail authentication timestamp.
            now = str(time.time_ns()//1_000_000)
            request.headers["X-PM-Timestamp"] = now
            request.headers["X-PM-Signature"] = base64.b64encode(self.client._load_key().sign(f"{now}POST{path}".encode())).decode()
        try:
            return await self.client._request(request, mutation_id=intent.client_order_id, before_send=before_send)
        except asyncio.CancelledError:
            raise UnknownOrderOutcome(intent.client_order_id) from None

    async def submit_intent(self, intent, authorization):
        async with self._lock:
            if not self.authority.consume(authorization, intent.id):
                raise PermissionError("One-use central risk authorization required")
            self._scope_check()
            body = self.build_native_request(intent)
            p = self.preflight
            self._preflight_check(intent)
            if intent.client_order_id in self._transmitted:
                raise UnknownOrderOutcome(intent.client_order_id)
            self._transmitted.add(intent.client_order_id)
            self.client.settings.gate = NativeTradingGate(enabled=True, preflight_valid_until=p.valid_until,
                account_authorized=True, geographic_eligibility_verified=True, risk_approval_id=authorization,
                durable_intent_id=intent.client_order_id)
            try:
                response = await self._post("/v1/orders", body, intent, placement=True)
                try:
                    order_id = native_id(response.get("id"))
                except (ReconciliationError, AttributeError):
                    raise UnknownOrderOutcome(intent.client_order_id) from None
                self.bind_native_intent(order_id, intent)
                # Preserve exact immediate executions for subsequent GET-order
                # reconciliation, but never report an acknowledgement as a fill.
                for execution in response.get("executions", []):
                    if execution.get("type") in {"EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"}:
                        self._remember_fill(order_id, normalize_execution(intent, execution,
                            expected_order_id=order_id, observed_at=utcnow()))
                return NativeOrderReport(native_order_id=order_id, client_order_id=intent.client_order_id,
                                         status="acknowledged", financial_complete=False)
            finally:
                self.client.settings.gate = NativeTradingGate()

    async def cancel_native(self, order_id, intent):
        self._scope_check()
        if self._native_intents.get(order_id) != intent:
            raise PreflightRequired("Cancel only a bound durable native order")
        try:
            await self._post(f"/v1/order/{native_id(order_id)}/cancel", {"marketSlug": intent.instrument.instrument_id}, intent, placement=False)
        except VenueError:
            return NativeCancelReport(native_order_id=order_id, client_order_id=intent.client_order_id,
                                      status="unknown", reason="Cancellation outcome requires authoritative order reconciliation")
        return NativeCancelReport(native_order_id=order_id, client_order_id=intent.client_order_id)

    async def resolve_client_order(self, intent):
        # Retail has no documented client identity field or client-ID search.
        # Similar market/price/size is never sufficient to bind an unknown send.
        return next((key for key, known in self._native_intents.items() if known == intent), None)

    async def reconcile_native(self, order_id, intent):
        self._scope_check()
        if self._native_intents.get(order_id) != intent:
            raise PreflightRequired("Restore the durable native order binding before reconciliation")
        try:
            payload = await self.client.reconcile_order(order_id)
            return normalize_order(intent, payload, expected_order_id=order_id, observed_at=utcnow(),
                                   fills=tuple(self._fills.get(order_id, {}).values()))
        except (VenueError, ValueError, TypeError, KeyError):
            return NativeOrderReport(native_order_id=order_id, client_order_id=intent.client_order_id,
                status="unknown", reason="Polymarket US order or execution evidence is incomplete; retain exposure")

    def observe_private_frame(self, frame):
        try:
            self._observe_private_frame(frame)
        except (ReconciliationError, VenueError, ValueError, TypeError, AttributeError, KeyError):
            self.private_snapshot_complete = False
            raise

    def _observe_private_frame(self, frame):
        if frame.get("type") == "transport_invalidated":
            self.private_generation += 1
            self.private_revision += 1
            self.private_snapshot_complete = False
            self.private_orders = {}
            return
        if frame.get("type") != "polymarket_us_monitor" or frame.get("channel") != "private" or frame.get("kind") != "data":
            return
        data = frame.get("payload", {})
        self.private_revision += 1
        snapshot = alias(data, "orderSubscriptionSnapshot", "order_subscription_snapshot")
        if snapshot is not None:
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("orders"), list) or not isinstance(snapshot.get("eof"), bool):
                raise ReconciliationError("Invalid private order snapshot")
            if self.private_snapshot_complete:
                raise ReconciliationError("Unexpected second private snapshot boundary")
            for order in snapshot["orders"]:
                order_id = native_id(order.get("id"))
                if order_id in self.private_orders:
                    raise ReconciliationError("Duplicate private snapshot order")
                self.private_orders[order_id] = order
            self.private_snapshot_complete = snapshot["eof"]
        update = alias(data, "orderSubscriptionUpdate", "order_subscription_update")
        if update is not None:
            if not isinstance(update, dict) or not isinstance(update.get("execution"), dict):
                raise ReconciliationError("Invalid private order execution")
            execution = update["execution"]
            order = execution.get("order", {})
            order_id = native_id(order.get("id"))
            self.private_orders[order_id] = order
            intent = self._native_intents.get(order_id)
            if execution.get("type") in {"EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"}:
                observed = utcnow()
                if intent:
                    self._remember_fill(order_id, normalize_execution(intent, execution,
                        expected_order_id=order_id, observed_at=observed))
                else:
                    # A private execution can arrive before the placement HTTP
                    # response gives us its native ID. Retain bounded evidence,
                    # but bind it ONLY when the acknowledged ID is known. No
                    # market/price/size heuristics and no replay claim.
                    fill_id = native_id(execution.get("id"))
                    buffered = self._unbound_executions.get(order_id, {})
                    old = buffered.get(fill_id)
                    if old is not None and old[0] != execution:
                        raise ReconciliationError("Conflicting unbound Polymarket US execution")
                    if old is None:
                        if sum(len(values) for values in self._unbound_executions.values()) >= 512:
                            raise ReconciliationError("Unbound execution evidence limit reached; account review required")
                        self._unbound_executions.setdefault(order_id, {})[fill_id] = (deepcopy(execution), observed)

    async def submit(self, *args, **kwargs):
        raise UnsupportedOperation("Use exact native intents through the central OMS")

    async def cancel(self, *args, **kwargs):
        raise UnsupportedOperation("Use bound native order cancellation through the central OMS")

    async def reconcile(self, *args, **kwargs):
        raise UnsupportedOperation("Use exact native reconciliation")
