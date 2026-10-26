"""Reviewed external positions are inventory, never OMS orders or cash debits."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExternalPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    venue: Literal["novig"]
    environment: Literal["production"]
    account_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_order_id: str = Field(min_length=1)
    native_event_id: str = Field(min_length=1)
    native_market_id: str = Field(min_length=1)
    native_outcome_id: str = Field(min_length=1)
    sport: Literal["basketball", "football", "baseball", "hockey"]
    quantity: Decimal = Field(gt=0)
    cost_usd: Decimal = Field(gt=0)
    recorded_at: datetime
    source_artifact: str = Field(pattern=r"^[a-zA-Z0-9_-]+\.json$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_artifact: str = Field(pattern=r"^[a-zA-Z0-9_-]+\.json$")
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_money_and_clock(self):
        if (not self.quantity.is_finite() or self.quantity != self.quantity.to_integral_value()
                or not self.cost_usd.is_finite() or self.recorded_at.tzinfo is None):
            raise ValueError("External inventory needs whole contracts, exact cost and zoned time")
        return self


class OwnerTrackedProphetXPosition(BaseModel):
    """Owner-tracked raw fill; conservatively reserved until separately released.

    The receipt proves the original fill, not current account inventory or
    settlement. This record never grants trading or becomes an OMS order.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    venue: Literal["prophetx"] = "prophetx"
    environment: Literal["production"] = "production"
    account_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_order_id: str = Field(min_length=1)
    native_event_id: str = Field(min_length=1)
    native_market_id: str = Field(min_length=1)
    native_outcome_id: str = Field(min_length=1)
    sport: Literal["basketball", "football", "baseball", "hockey"]
    quantity: Decimal = Field(gt=0)  # ProphetX USD stake, not contracts.
    cost_usd: Decimal = Field(gt=0)
    recorded_at: datetime
    source_artifact: str = Field(pattern=r"^prophetx_[a-zA-Z0-9_-]+\.json$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tracking_basis: Literal["owner_attested_raw_fill"] = "owner_attested_raw_fill"

    @model_validator(mode="after")
    def exact_stake(self):
        if (not self.quantity.is_finite() or not self.cost_usd.is_finite()
                or self.quantity != self.cost_usd or self.recorded_at.tzinfo is None):
            raise ValueError("Owner-tracked ProphetX stake requires exact positive USD cash risk and zoned time")
        return self


class PendingExternalOrder(BaseModel):
    """Known raw placement whose current exposure cannot yet be certified."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    venue: Literal["kalshi", "prophetx", "polymarket_us"]
    native_order_id: str = Field(min_length=1)
    source_artifact: str = Field(pattern=r"^[a-zA-Z0-9_-]+\.json$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExternalInventoryFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    positions: tuple[ExternalPosition | OwnerTrackedProphetXPosition, ...] = ()
    pending_orders: tuple[PendingExternalOrder, ...] = ()

    @model_validator(mode="after")
    def distinct_native_positions(self):
        keys = [(row.venue, row.account_fingerprint, row.native_market_id,
                 row.native_outcome_id) for row in self.positions]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate external position identity")
        pending_keys = [(row.venue, row.native_order_id) for row in self.pending_orders]
        if len(pending_keys) != len(set(pending_keys)):
            raise ValueError("Duplicate pending external order identity")
        if set(pending_keys).intersection((row.venue, row.native_order_id) for row in self.positions):
            raise ValueError("External order cannot be both pending and tracked")
        return self


def load_external_inventory_review(path: Path) -> ExternalInventoryFile:
    if not path.exists():
        return ExternalInventoryFile()
    inventory = ExternalInventoryFile.model_validate_json(path.read_text(encoding="utf-8"))
    for row in inventory.pending_orders:
        artifact = path.parent / row.source_artifact
        if (not row.source_artifact.startswith(row.venue + "_") or not artifact.is_file()
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != row.source_sha256):
            raise ValueError("Pending external order artifact changed or is missing")
        receipt = json.loads(artifact.read_text(encoding="utf-8"))
        if receipt.get("order_id") != row.native_order_id or receipt.get("status") != "filled_unsettled":
            raise ValueError("Pending external order differs from raw placement receipt")
    for row in inventory.positions:
        def verified_json(filename: str, digest: str):
            artifact = path.parent / filename
            if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
                raise ValueError("External position evidence artifact changed or is missing")
            return json.loads(artifact.read_text(encoding="utf-8"))

        if isinstance(row, OwnerTrackedProphetXPosition):
            receipt = verified_json(row.source_artifact, row.source_sha256)
            try:
                matches = (receipt.get("venue") == "prophetx"
                    and receipt.get("environment") == row.environment
                    and receipt.get("order_id") == row.native_order_id
                    and receipt.get("event_id") == row.native_event_id
                    and receipt.get("market_id") == row.native_market_id
                    and receipt.get("strike_id") == row.native_outcome_id
                    and receipt.get("status") == "filled_unsettled"
                    and receipt.get("matching_status") == "fully_matched"
                    and receipt.get("open_stake_usd") == "0"
                    and Decimal(str(receipt.get("filled_stake_usd"))) == row.quantity
                    and Decimal(str(receipt.get("maximum_loss_usd"))) == row.cost_usd
                    and datetime.fromisoformat(receipt["recorded_at"]) == row.recorded_at)
            except (KeyError, TypeError, InvalidOperation) as exc:
                raise ValueError("Owner-tracked ProphetX receipt is malformed") from exc
            if not matches:
                raise ValueError("Owner-tracked ProphetX position differs from its raw fill receipt")
            continue
        try:
            receipt = verified_json(row.source_artifact, row.source_sha256)
            catalog = verified_json(row.catalog_artifact, row.catalog_sha256)
            event, market = catalog["event"], catalog["market"]
            matches = (receipt.get("venue") == row.venue
                and receipt.get("account_fingerprint") == row.account_fingerprint
                and receipt.get("order_id") == row.native_order_id
                and receipt.get("market_id") == row.native_market_id
                and receipt.get("outcome_id") == row.native_outcome_id
                and receipt.get("native_status") == "FILLED"
                and Decimal(str(receipt.get("filled_qty"))) == row.quantity
                and Decimal(str(receipt.get("filled_cost_usd"))) == row.cost_usd
                and datetime.fromisoformat(receipt["recorded_at"]) == row.recorded_at
                and event.get("eventId") == row.native_event_id
                and str(event.get("sport", "")).lower() == row.sport
                and market.get("eventId") == row.native_event_id
                and market.get("marketId") == row.native_market_id
                and any(outcome.get("outcomeId") == row.native_outcome_id
                        for outcome in market.get("outcomes", [])))
        except (AttributeError, KeyError, TypeError, InvalidOperation) as exc:
            raise ValueError("External position evidence is malformed") from exc
        if not matches:
            raise ValueError("External position identity differs from reviewed receipt or catalog")
    return inventory


def load_external_inventory(path: Path) -> tuple[ExternalPosition | OwnerTrackedProphetXPosition, ...]:
    return load_external_inventory_review(path).positions
