"""Core ExecutionVenue bridges with verified mappings and single-use authority.

Factories are intentionally not auto-enabled. Native execution additionally
requires the application's risk engine/OMS to support native reconciliation.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import Order
from ..core.native_accounting import NativeOrderIntent
from ..core.plugins import ExecutionVenue, Manifest
from .common import NativeTradingGate, PreflightRequired, SequenceTracker, UnknownOrderOutcome, VenueError, VenueHTTPError, decimal_value
from .kalshi import KalshiClient
from .novig import NovigClient
from .prophetx import ProphetXClient
from .reconciliation import (
    NativeCancelReport, NativeOrderReport, ReconciliationError, match_client_order, normalize_ack,
    normalize_kalshi_order, normalize_novig_fill, normalize_novig_order,
    normalize_prophetx_cancel, normalize_prophetx_order,
)


class VerifiedVenueMapping(BaseModel):
    """Explicit operator-reviewed contract mapping, never fuzzy matching."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    canonical_market_id: str
    canonical_outcome: str
    native_instrument_id: str
    native_exchange_index: int | None = Field(default=None, ge=0)
    native_outcome: str
    settlement_fingerprint: str = Field(min_length=1)
    mapping_evidence: str = Field(min_length=1)
    verified: bool = False
    fee_schedule_verified: bool = False
    fee_assumption_approved: bool = False
    canonical_payout_usd: Decimal = Decimal("1")
    native_payout_usd: Decimal | None = None
    native_quantity_unit: str = "unknown"


class NativePreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    environment: Literal["sandbox", "production"] = "sandbox"
    account_authorized: bool = False
    geographic_eligibility_verified: bool = False
    valid_until: float = 0
    evidence: str = ""
    native_trading_enabled: bool = False


def bridge_manifest(client) -> Manifest:
    metadata = client.get_manifest()
    return Manifest(
        name=f"{metadata['id']}_execution", kind="execution",
        description=f"{metadata['name']}; native protocol available, routing requires verified mapping/preflight",
        sports=metadata["sports"], markets=metadata["markets"],
        input_schemas=["trade.approved@1"], output_schemas=["trade.order@1", "trade.fill@1"],
        transports=metadata["transports"], authentication=[metadata["authentication"]],
        capabilities={**metadata["capabilities"], "auto_enabled": False, "acknowledgement_is_fill": False,
                      "core_quantity_support": "legacy Order schema: whole USD 1 binary contracts only",
                      "native_quantity_support": {"kalshi": "fixed-point USD 1 contracts",
                          "novig": "whole USD 0.01 contracts; IOC taker orders",
                          "prophetx": "USD stake at native American odds; actual-fee reconciliation required"}[client.venue_id],
                      "credential_environment_variables": metadata["credentials"]},
        rate_limits=metadata["rate_limits"], permissions=metadata["permissions"],
        documentation=[metadata["docs"]],
    )


class NativeExecutionVenue(ExecutionVenue):
    """Risk-approved legacy and exact-native interfaces, with separate units."""

    config_model = None  # Secrets and credentials must never enter PluginRegistry.public().

    def __init__(self, client, authority, *, mappings: list[VerifiedVenueMapping] | None = None,
                 preflight: NativePreflight | None = None):
        self.client, self.authority = client, authority
        self.preflight = preflight or NativePreflight()
        self.manifest = bridge_manifest(client)
        self.mappings = {(m.canonical_market_id, m.canonical_outcome): m for m in (mappings or [])}
        self._lock = asyncio.Lock()
        self._transmitted: set[str] = set()
        self._native_intents: dict[str, NativeOrderIntent] = {}
        self._native_client_intents: dict[str, NativeOrderIntent] = {}
        self._private_sequences = SequenceTracker()
        self._confirmed_filled: dict[str, Decimal] = {}
        self._private_cancelled: set[str] = set()
        self.bound_exchange_index: int | None = None
        # Runtime injects the current risk, lease, readiness and pricing checks.
        # This runs synchronously after transport pacing, for placements only.
        self.dispatch_guard: Callable[[NativeOrderIntent], None] | None = None

    def _mapping(self, order: Order) -> VerifiedVenueMapping:
        mapping = self.mappings.get((order.market_id, order.outcome))
        if not mapping or not mapping.verified or not (mapping.fee_schedule_verified or mapping.fee_assumption_approved):
            raise PreflightRequired("Verified event/outcome/settlement mapping and fee schedule required")
        if order.venue != self.client.venue_id:
            raise PreflightRequired("Order venue does not match the execution adapter")
        native = self.client.get_capabilities()
        # Existing Order and Fill quantities are whole $1 contracts. A cent-
        # contract partial or variable-payout USD stake cannot be rounded into
        # this type safely. Retain the native schema until the core is extended.
        if (mapping.canonical_payout_usd != 1 or mapping.native_payout_usd != 1
                or mapping.native_quantity_unit != "contracts"
                or native.get("payout_usd") != "1.00"):
            raise PreflightRequired("Unsupported payout/quantity transform for the core USD 1 contract schema")
        return mapping

    def _preflight(self, order: Order):
        p = self.preflight
        if order.synthetic or order.mode not in {"sandbox", "production"}:
            raise PreflightRequired("Native orders require explicit non-synthetic sandbox/production mode")
        if p.environment != order.mode or self.client.settings.environment != order.mode:
            raise PreflightRequired("Order, client, and preflight environments differ")
        if not p.native_trading_enabled or not p.account_authorized or not p.evidence or p.valid_until <= time.time():
            raise PreflightRequired("Current successful native account preflight and explicit enablement required")
        if order.mode == "production" and not p.geographic_eligibility_verified:
            raise PreflightRequired("Official geographic eligibility verification is required")

    async def submit(self, order: Order, authorization):
        async with self._lock:
            if not self.authority.consume(authorization, order.id):
                raise PermissionError("One-use central risk authorization required")
            mapping = self._mapping(order)
            self._preflight(order)
            if order.client_order_id in self._transmitted:
                raise UnknownOrderOutcome(order.client_order_id)
            if not isinstance(self.client, KalshiClient):
                raise PreflightRequired("Core native quantity normalization is not yet supported for this venue")
            body = self.client.order_body(mapping.native_instrument_id, mapping.native_outcome,
                                          order.price, Decimal(order.quantity), order.client_order_id)
            client, settings, authority = self.client, self.client.settings, self.authority
            settings.gate = NativeTradingGate(
                enabled=True, preflight_valid_until=self.preflight.valid_until,
                account_authorized=self.preflight.account_authorized,
                geographic_eligibility_verified=self.preflight.geographic_eligibility_verified,
                risk_approval_id=authorization, durable_intent_id=order.client_order_id,
            )
            # Remember even an unknown outcome. Durable deduplication/restart
            # remains the OMS's responsibility; this is a second local guard.
            self._transmitted.add(order.client_order_id)

            def before_send():
                if self.client is not client or self.authority is not authority or self._mapping(order) != mapping:
                    raise PreflightRequired("Native adapter or reviewed mapping changed before placement")
                self._preflight(order)

            try:
                response = await client.submit_order(body, before_send=before_send)
            finally:
                settings.gate = NativeTradingGate()
            native_id = response.get("order_id")
            if not native_id:
                raise UnknownOrderOutcome(order.client_order_id)
            return {"venue_order_id": native_id, "status": "acknowledged",
                    "requires_reconciliation": True, "synthetic": False}

    async def cancel(self, order: Order):
        mapping = self._mapping(order)
        if not order.venue_order_id:
            return {"status": "unknown", "requires_reconciliation": True}
        if isinstance(self.client, KalshiClient):
            await self.client.cancel_order(order.venue_order_id, market_ticker=mapping.native_instrument_id)
        elif isinstance(self.client, ProphetXClient):
            await self.client.cancel_order(order.venue_order_id, external_id=order.client_order_id)
        else:
            await self.client.cancel_order(order.venue_order_id)
        # A cancel acknowledgement does not disclose all racing fills.
        return {"status": "cancel_pending", "requires_reconciliation": True}

    async def reconcile(self, order: Order):
        try:
            self._mapping(order)
            if not order.venue_order_id:
                return {"status": "unknown", "requires_reconciliation": True}
            raw = await self.client.reconcile_order(order.venue_order_id)
            if not isinstance(self.client, KalshiClient):
                return {"status": "unknown", "requires_reconciliation": True}
            data = raw["order"]
            filled = decimal_value(data["fill_count_fp"])
            remaining = decimal_value(data["remaining_count_fp"])
            if (filled != filled.to_integral_value() or remaining != remaining.to_integral_value()
                    or filled < 0 or remaining < 0 or filled > order.quantity):
                return {"status": "unknown", "reason": "Native fractional/invalid quantities require extended fill schema"}
            cost = decimal_value(data["taker_fill_cost_dollars"]) + decimal_value(data["maker_fill_cost_dollars"])
            fees = decimal_value(data["taker_fees_dollars"]) + decimal_value(data["maker_fees_dollars"])
            if filled == order.quantity and remaining == 0:
                state = "filled"
            elif data["status"] == "canceled" and remaining == 0:
                state = "cancelled"
            elif data["status"] == "resting" and remaining > 0:
                state = "partial" if filled else "open"
            else:
                state = "unknown"
            return {"status": state, "filled_quantity": int(filled), "remaining_quantity": int(remaining),
                    "average_fill_price": str(cost / filled if filled else Decimal(0)),
                    "fees": str(fees), "requires_fill_reconciliation": True, "synthetic": False}
        except (VenueError, KeyError, ValueError, TypeError):
            return {"status": "unknown", "requires_reconciliation": True}

    def _native_mapping(self, intent: NativeOrderIntent) -> VerifiedVenueMapping:
        instrument = intent.instrument
        mapping = self.mappings.get((instrument.canonical_market_id, instrument.outcome))
        if not mapping or not mapping.verified or not (mapping.fee_schedule_verified or mapping.fee_assumption_approved):
            raise PreflightRequired("Reviewed native mapping and fee evidence required")
        if (instrument.venue != self.client.venue_id
                or mapping.native_instrument_id != instrument.instrument_id
                or mapping.native_outcome != instrument.native_outcome
                or mapping.native_quantity_unit != instrument.quantity_unit
                or mapping.native_payout_usd != instrument.payout_per_unit_usd
                or mapping.settlement_fingerprint != instrument.settlement_rules_fingerprint):
            raise PreflightRequired("Native intent differs from the reviewed venue mapping")
        if isinstance(self.client, KalshiClient) and (
                mapping.native_exchange_index != instrument.exchange_index
                or (intent.mode == "production" and instrument.exchange_index is None)):
            raise PreflightRequired("Reviewed Kalshi exchange shard is missing or differs from durable intent")
        return mapping

    def _native_preflight(self, intent: NativeOrderIntent):
        p = self.preflight
        if p.environment != intent.mode or self.client.settings.environment != intent.mode:
            raise PreflightRequired("Native intent, client and preflight environments differ")
        if not p.native_trading_enabled or not p.account_authorized or not p.evidence or p.valid_until <= time.time():
            raise PreflightRequired("Current successful native account preflight and explicit enablement required")
        if intent.mode == "production" and not p.geographic_eligibility_verified:
            raise PreflightRequired("Official geographic eligibility verification is required")
        if isinstance(self.client, KalshiClient) and intent.mode == "production" and (
                intent.instrument.exchange_index is None
                or intent.instrument.exchange_index != self.bound_exchange_index):
            raise PreflightRequired("Kalshi account and durable order shard are not bound")

    def build_native_request(self, intent: NativeOrderIntent) -> dict:
        """Reviewable exact native request; performs no I/O or authorization."""
        self._native_mapping(intent)
        instrument = intent.instrument
        if isinstance(self.client, KalshiClient):
            return self.client.order_body(instrument.instrument_id, instrument.native_outcome,
                                          intent.limit_price, intent.quantity, intent.client_order_id)
        if isinstance(self.client, NovigClient):
            if intent.quantity != intent.quantity.to_integral_value():
                raise PreflightRequired("Novig contract quantity must be whole")
            return self.client.order_body(instrument.native_outcome, intent.limit_price,
                                          int(intent.quantity), intent.client_order_id, tif="IOC")
        if isinstance(self.client, ProphetXClient):
            if intent.limit_price != intent.limit_price.to_integral_value():
                raise PreflightRequired("ProphetX price must be integral American odds")
            if int(intent.limit_price) not in self.client.price_ladder:
                raise PreflightRequired("ProphetX requires a current verified native price ladder")
            body = self.client.order_body(instrument.instrument_id, int(intent.limit_price),
                                          intent.quantity, intent.client_order_id, strategy="fillOrKill")
            # The documented JSON number is generated by the existing transport;
            # refuse a conversion that would alter the exact cents at this bound.
            if Decimal(str(body["quantity"])) != intent.quantity:
                raise PreflightRequired("Native JSON quantity would lose stake precision")
            return body
        raise PreflightRequired("No native request encoder is registered for this client")

    async def submit_intent(self, intent: NativeOrderIntent, authorization) -> NativeOrderReport:
        """Quantity-aware entry point; legacy $1 submit remains unchanged."""
        async with self._lock:
            if not self.authority.consume(authorization, intent.id):
                raise PermissionError("One-use central risk authorization required")
            self._native_preflight(intent)
            body = self.build_native_request(intent)
            if intent.client_order_id in self._transmitted:
                raise UnknownOrderOutcome(intent.client_order_id)
            client, settings, authority = self.client, self.client.settings, self.authority
            dispatch_guard = self.dispatch_guard
            settings.gate = NativeTradingGate(
                enabled=True, preflight_valid_until=self.preflight.valid_until,
                account_authorized=self.preflight.account_authorized,
                geographic_eligibility_verified=self.preflight.geographic_eligibility_verified,
                risk_approval_id=authorization, durable_intent_id=intent.client_order_id,
            )
            self._transmitted.add(intent.client_order_id)
            self._native_client_intents[intent.client_order_id] = intent

            def before_send():
                if (self.client is not client or self.authority is not authority
                        or self.dispatch_guard is not dispatch_guard
                        or self._native_client_intents.get(intent.client_order_id) != intent):
                    raise PreflightRequired("Native adapter or durable intent binding changed before placement")
                self._native_preflight(intent)
                if self.build_native_request(intent) != body:
                    raise PreflightRequired("Reviewed native request changed before placement")
                if dispatch_guard is not None:
                    dispatch_guard(intent)

            try:
                response = await client.submit_order(body, before_send=before_send)
                report = normalize_ack(client.venue_id, response, intent)
                self.bind_native_intent(report.native_order_id, intent)
                return report
            finally:
                settings.gate = NativeTradingGate()

    async def cancel_native(self, native_order_id: str, intent: NativeOrderIntent) -> NativeCancelReport:
        self._native_mapping(intent)
        if not native_order_id:
            return NativeCancelReport(native_order_id="", client_order_id=intent.client_order_id,
                                      status="unknown", reason="Resolve native identity before cancellation")
        try:
            if isinstance(self.client, KalshiClient):
                await self.client.cancel_order(native_order_id, market_ticker=intent.instrument.instrument_id,
                    exchange_index=intent.instrument.exchange_index)
            elif isinstance(self.client, ProphetXClient):
                await self.client.cancel_order(native_order_id, external_id=intent.client_order_id)
                return normalize_prophetx_cancel(native_order_id, intent)
            else:
                await self.client.cancel_order(native_order_id)
        except VenueHTTPError as exc:
            if isinstance(self.client, ProphetXClient):
                return normalize_prophetx_cancel(native_order_id, intent, status_code=exc.status_code, error_code=exc.code)
            return NativeCancelReport(native_order_id=native_order_id, client_order_id=intent.client_order_id,
                                      status="unknown", reason="Cancellation requires current order/fill reconciliation")
        except VenueError:
            return NativeCancelReport(native_order_id=native_order_id, client_order_id=intent.client_order_id,
                                      status="unknown", reason="Cancellation transmission outcome is unknown")
        return NativeCancelReport(native_order_id=native_order_id, client_order_id=intent.client_order_id)

    async def reconcile_native(self, native_order_id: str, intent: NativeOrderIntent) -> NativeOrderReport:
        """Read-only reconciliation. A 404 or incomplete history keeps exposure."""
        from ..core.models import utcnow
        self._native_mapping(intent)
        self.bind_native_intent(native_order_id, intent)
        try:
            if isinstance(self.client, ProphetXClient):
                from .prophetx_financial import reconcile_prophetx_financial
                return await reconcile_prophetx_financial(self.client, intent, native_order_id)
            raw = await self.client.reconcile_order(native_order_id)
            if isinstance(self.client, KalshiClient):
                return normalize_kalshi_order(intent, raw, expected_order_id=native_order_id, observed_at=utcnow())
            if isinstance(self.client, NovigClient):
                rows, complete = await self._fill_pages(native_order_id)
                fills = tuple(normalize_novig_fill(intent, row, expected_order_id=native_order_id) for row in rows)
                return normalize_novig_order(intent, raw, expected_order_id=native_order_id,
                                             observed_at=utcnow(), fills=fills, history_complete=complete,
                                             confirmed_filled_quantity=self._confirmed_filled.get(native_order_id)
                                             if native_order_id in self._private_cancelled else None)
        except (VenueError, ValueError, KeyError, TypeError):
            pass
        return NativeOrderReport(native_order_id=native_order_id, client_order_id=intent.client_order_id,
                                 status="unknown", reason="Native evidence is unavailable or inconsistent; retain exposure")

    async def _fill_pages(self, native_order_id: str) -> tuple[list[dict], bool]:
        rows, seen, after = [], set(), None
        for _ in range(20):
            params = {"order": native_order_id, "limit": 500}
            if after:
                params["after"] = after
            page = await self.client.fetch_fills(**params)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ReconciliationError("Expected a subaccount fill-history page")
            rows.extend(page["items"])
            if "next" not in page:
                return rows, True
            after = page["next"]
            if not isinstance(after, str) or not after or after in seen:
                raise ReconciliationError("Native fill cursor loop")
            seen.add(after)
        return rows, False

    async def resolve_client_order(self, intent: NativeOrderIntent) -> str | None:
        """Bounded, exhaustive account-history scan; never submits or retries."""
        self._native_mapping(intent)
        rows, seen, cursor = [], set(), None
        for _ in range(20):
            if isinstance(self.client, KalshiClient):
                params = {"limit": 200, "ticker": intent.instrument.instrument_id, "subaccount": 0,
                          **({"exchange_index": intent.instrument.exchange_index}
                             if intent.instrument.exchange_index is not None else {}),
                          **({"cursor": cursor} if cursor else {})}
                page = await self.client.reconcile_account(**params)
                entries, cursor = page.get("orders"), page.get("cursor")
                finished = cursor == ""
            elif isinstance(self.client, NovigClient):
                params = {"limit": 500, **({"after": cursor} if cursor else {})}
                page = await self.client.reconcile_account(**params)
                entries, cursor = page.get("items"), page.get("next")
                finished = "next" not in page
            else:
                # Current ProphetX history requires a separate account schema
                # verifier before identity can be resolved from its envelope.
                return None
            if not isinstance(entries, list):
                raise ReconciliationError("Expected a native account history page")
            rows.extend(entries)
            if finished:
                native_id = match_client_order(self.client.venue_id, intent, rows)
                if native_id:
                    # Identity still must be checked against instrument/outcome
                    # by reconcile_native before any exposure is released.
                    self._transmitted.add(intent.client_order_id)
                    self.bind_native_intent(native_id, intent)
                return native_id
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ReconciliationError("Native account cursor loop")
            seen.add(cursor)
        return None

    def bind_native_intent(self, native_order_id: str, intent: NativeOrderIntent):
        """Restore ownership only; this never restores stream synchronization."""
        if not native_order_id:
            raise ReconciliationError("A native order ID is required")
        old = self._native_intents.get(native_order_id)
        if old is not None and old != intent:
            raise ReconciliationError("Native order ID is already bound to a different intent")
        self._native_intents[native_order_id] = intent
        self._native_client_intents[intent.client_order_id] = intent

    def observe_private_frame(self, frame: dict) -> None:
        """Retain Novig cumulative quantities only across a contiguous stream.

        Call once per authenticated frame, including subscription snapshots and
        invalidations. Private fills contain no fees and never settle money;
        this watermark merely verifies a complete subsequent REST fill history.
        """
        if not isinstance(self.client, NovigClient):
            return
        if frame.get("type") in {"transport_invalidated", "snapshot_required"}:
            self._private_sequences.reset()
            self._confirmed_filled.clear()
            self._private_cancelled.clear()
            return
        if "orders" not in frame:
            return
        data = frame["orders"]
        try:
            seq = data["seq"]
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise ReconciliationError("Invalid private order sequence")
            snapshot = "snapshot" in frame
            if snapshot:
                self._private_sequences.snapshot("orders", seq)
                filled, cancelled = {}, set()
                deltas = [{**row, "kind": "open"} for row in data["open"]]
            else:
                if not self._private_sequences.delta("orders", seq):
                    return
                filled, cancelled = dict(self._confirmed_filled), set(self._private_cancelled)
                deltas = data["deltas"]
            for event in deltas:
                native_id = event["orderId"]
                intent = self._native_intents.get(native_id) or self._native_client_intents.get(event.get("clientId"))
                if intent is None:
                    continue  # account inventory reconciliation blocks unmanaged positions
                if event.get("clientId", intent.client_order_id) != intent.client_order_id:
                    raise ReconciliationError("Private client ID changed")
                kind = event["kind"]
                if kind in {"open", "fill"}:
                    if event.get("outcomeId") != intent.instrument.native_outcome:
                        raise ReconciliationError("Private outcome changed")
                    raw_remaining = event["qty"] if kind == "open" else event["remaining"]
                    if isinstance(raw_remaining, bool) or not isinstance(raw_remaining, int):
                        raise ReconciliationError("Invalid private remaining contracts")
                    cumulative = intent.quantity - raw_remaining
                    if not 0 <= cumulative <= intent.quantity or cumulative < filled.get(native_id, Decimal(0)):
                        raise ReconciliationError("Private cumulative quantity regressed")
                    if kind == "fill":
                        quantity = event["qty"]
                        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0 or quantity > cumulative:
                            raise ReconciliationError("Invalid private fill contracts")
                        if native_id in filled and filled[native_id] + quantity != cumulative:
                            raise ReconciliationError("Private fill differs from contiguous quantity")
                    filled[native_id] = cumulative
                    cancelled.discard(native_id)
                elif kind == "cancel":
                    if native_id in filled:
                        cancelled.add(native_id)
                elif kind == "reject":
                    if filled.get(native_id, Decimal(0)):
                        raise ReconciliationError("Private rejected order already has fills")
                    filled[native_id] = Decimal(0)
                else:
                    raise ReconciliationError("Unsupported private order event")
                self.bind_native_intent(native_id, intent)
            self._confirmed_filled, self._private_cancelled = filled, cancelled
        except (ValueError, KeyError, TypeError, VenueError):
            self._private_sequences.reset()
            self._confirmed_filled.clear()
            self._private_cancelled.clear()
            raise

    async def health_check(self):
        return {"status": "blocked", "message": "Native transport available; verified mapping and completed OMS preflight required",
                "transport": self.client.health_check()}

    async def stop(self):
        await self.client.disconnect()


def native_execution_factories(authority) -> dict:
    """Return dependency-injected factories without opening connections."""
    def polymarket_factory(**kwargs):
        # The executor imports the shared reviewed mapping/preflight contracts.
        # Defer this import until requested to avoid a circular module import.
        from .polymarket_us import PolymarketUSClient
        from .polymarket_us_execution import PolymarketUSExecutionVenue
        client = kwargs.pop("client", None) or PolymarketUSClient()
        return PolymarketUSExecutionVenue(client, authority, **kwargs)

    return {
        "kalshi": lambda **kwargs: NativeExecutionVenue(KalshiClient(), authority, **kwargs),
        "novig": lambda **kwargs: NativeExecutionVenue(NovigClient(), authority, **kwargs),
        "prophetx": lambda **kwargs: NativeExecutionVenue(ProphetXClient(), authority, **kwargs),
        "polymarket_us": polymarket_factory,
    }
