"""Core ExecutionVenue bridges with verified mappings and single-use authority.

Factories are intentionally not auto-enabled. Native execution additionally
requires the application's risk engine/OMS to support native reconciliation.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import Order
from ..core.plugins import ExecutionVenue, Manifest
from .common import NativeTradingGate, PreflightRequired, UnknownOrderOutcome, VenueError, decimal_value
from .kalshi import KalshiClient
from .novig import NovigClient
from .prophetx import ProphetXClient


class VerifiedVenueMapping(BaseModel):
    """Explicit operator-reviewed contract mapping, never fuzzy matching."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    canonical_market_id: str
    canonical_outcome: str
    native_instrument_id: str
    native_outcome: str
    settlement_fingerprint: str = Field(min_length=1)
    mapping_evidence: str = Field(min_length=1)
    verified: bool = False
    fee_schedule_verified: bool = False
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
                      "core_quantity_support": "whole USD 1 binary contracts only",
                      "credential_environment_variables": metadata["credentials"]},
        rate_limits=metadata["rate_limits"], permissions=metadata["permissions"],
        documentation=[metadata["docs"]],
    )


class NativeExecutionVenue(ExecutionVenue):
    """Risk-approved bridge; future quantity models need their own bridge contract."""

    config_model = None  # Secrets and credentials must never enter PluginRegistry.public().

    def __init__(self, client, authority, *, mappings: list[VerifiedVenueMapping] | None = None,
                 preflight: NativePreflight | None = None):
        self.client, self.authority = client, authority
        self.preflight = preflight or NativePreflight()
        self.manifest = bridge_manifest(client)
        self.mappings = {(m.canonical_market_id, m.canonical_outcome): m for m in (mappings or [])}
        self._lock = asyncio.Lock()
        self._transmitted: set[str] = set()

    def _mapping(self, order: Order) -> VerifiedVenueMapping:
        mapping = self.mappings.get((order.market_id, order.outcome))
        if not mapping or not mapping.verified or not mapping.fee_schedule_verified:
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
            self.client.settings.gate = NativeTradingGate(
                enabled=True, preflight_valid_until=self.preflight.valid_until,
                account_authorized=self.preflight.account_authorized,
                geographic_eligibility_verified=self.preflight.geographic_eligibility_verified,
                risk_approval_id=authorization, durable_intent_id=order.client_order_id,
            )
            # Remember even an unknown outcome. Durable deduplication/restart
            # remains the OMS's responsibility; this is a second local guard.
            self._transmitted.add(order.client_order_id)
            try:
                response = await self.client.submit_order(body)
            finally:
                self.client.settings.gate = NativeTradingGate()
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

    async def health_check(self):
        return {"status": "blocked", "message": "Native transport available; verified mapping and completed OMS preflight required",
                "transport": self.client.health_check()}

    async def stop(self):
        await self.client.disconnect()


def native_execution_factories(authority) -> dict:
    """Return dependency-injected factories without opening connections."""
    return {
        "kalshi": lambda **kwargs: NativeExecutionVenue(KalshiClient(), authority, **kwargs),
        "novig": lambda **kwargs: NativeExecutionVenue(NovigClient(), authority, **kwargs),
        "prophetx": lambda **kwargs: NativeExecutionVenue(ProphetXClient(), authority, **kwargs),
    }
