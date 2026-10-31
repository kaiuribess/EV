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


class ReviewedKalshiPosition(BaseModel):
    """An external fill still present in authenticated Kalshi inventory."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    venue: Literal["kalshi"] = "kalshi"
    environment: Literal["production"] = "production"
    account_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_order_id: str = Field(min_length=1)
    native_market_id: str = Field(min_length=1)
    native_outcome_id: Literal["yes", "no"]
    sport: Literal["basketball", "football", "baseball", "hockey", "other"]
    exchange_index: int = Field(ge=0)
    quantity: Decimal = Field(gt=0)
    cost_usd: Decimal = Field(gt=0)
    fee_usd: Decimal = Field(ge=0)
    recorded_at: datetime
    review_artifact: str = Field(pattern=r"^kalshi_external_review_[a-zA-Z0-9_-]+\.json$")
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_position(self):
        if (not all(value.is_finite() for value in (self.quantity, self.cost_usd, self.fee_usd))
                or self.recorded_at.tzinfo is None):
            raise ValueError("Reviewed Kalshi position needs exact money and zoned time")
        return self


class ResolvedExternalOrder(BaseModel):
    """Historical raw order with a separately captured authoritative resolution."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    venue: Literal["kalshi", "prophetx", "polymarket_us"]
    account_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    native_order_id: str = Field(min_length=1)
    source_artifact: str = Field(pattern=r"^[a-zA-Z0-9_-]+\.json$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_artifact: str = Field(pattern=r"^[a-zA-Z0-9_-]+\.json$")
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UnresolvedAccountReservation(BaseModel):
    """Reported account reservation whose risk meaning is not documented."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    venue: Literal["polymarket_us"] = "polymarket_us"
    reported_usd: Decimal = Field(gt=0)
    review_artifact: str = Field(pattern=r"^polymarket_us_external_review_[a-zA-Z0-9_-]+\.json$")
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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
    positions: tuple[ExternalPosition | OwnerTrackedProphetXPosition | ReviewedKalshiPosition, ...] = ()
    pending_orders: tuple[PendingExternalOrder, ...] = ()
    resolved_orders: tuple[ResolvedExternalOrder, ...] = ()
    unresolved_accounts: tuple[UnresolvedAccountReservation, ...] = ()

    @model_validator(mode="after")
    def distinct_native_positions(self):
        keys = [(row.venue, row.account_fingerprint, row.native_market_id,
                 row.native_outcome_id) for row in self.positions]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate external position identity")
        pending_keys = [(row.venue, row.native_order_id) for row in self.pending_orders]
        if len(pending_keys) != len(set(pending_keys)):
            raise ValueError("Duplicate pending external order identity")
        resolved_keys = [(row.venue, row.native_order_id) for row in self.resolved_orders]
        if len(resolved_keys) != len(set(resolved_keys)):
            raise ValueError("Duplicate resolved external order identity")
        if set(pending_keys).intersection(resolved_keys):
            raise ValueError("External order cannot be both pending and resolved")
        if set(resolved_keys).intersection((row.venue, row.native_order_id) for row in self.positions):
            raise ValueError("External order cannot be both resolved and tracked")
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
    def verified_json(filename: str, digest: str):
        artifact = path.parent / filename
        if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
            raise ValueError("External position evidence artifact changed or is missing")
        return json.loads(artifact.read_text(encoding="utf-8"))

    for row in inventory.unresolved_accounts:
        review = verified_json(row.review_artifact, row.review_sha256)
        balance = review.get("usd_balance", {})
        if (review.get("venue") != row.venue or balance.get("currency") != "USD"
                or Decimal(str(balance.get("balanceReservation"))) != row.reported_usd):
            raise ValueError("Unresolved account reservation differs from authenticated read")

    for row in inventory.resolved_orders:
        receipt = verified_json(row.source_artifact, row.source_sha256)
        review = verified_json(row.review_artifact, row.review_sha256)
        if receipt.get("order_id") != row.native_order_id or receipt.get("status") != "filled_unsettled":
            raise ValueError("Resolved external order differs from raw receipt")
        if (row.venue != "polymarket_us" and
                (row.account_fingerprint is None or review.get("account_fingerprint") != row.account_fingerprint)):
            raise ValueError("Resolved external order belongs to another account")
        if row.venue == "kalshi":
            fills = [item for item in review.get("fills", []) if item.get("order_id") == row.native_order_id]
            settlements = [item for item in review.get("settlements", [])
                           if item.get("ticker") == receipt.get("ticker")]
            if (review.get("venue") != "kalshi" or len(fills) != 1 or len(settlements) != 1
                    or any(item.get("ticker") == receipt.get("ticker") for item in review.get("positions", []))
                    or fills[0].get("fill_id") != receipt.get("fill_id")
                    or settlements[0].get("market_result") != "no"
                    or settlements[0].get("exchange_index") != receipt.get("exchange_index")
                    or Decimal(str(settlements[0].get("yes_count_fp"))) != Decimal(str(receipt.get("filled_count_fp")))):
                raise ValueError("Kalshi external settlement does not resolve its original fill")
        elif row.venue == "prophetx":
            orders = [item for item in review.get("orders", []) if item.get("order_id") == row.native_order_id]
            wallet = review.get("wallet", {})
            transactions = [item for item in review.get("transactions", [])
                            if item.get("trade_id") == row.native_order_id]
            if (review.get("venue") != "prophetx" or len(orders) != 1
                    or orders[0].get("status") != "settled"
                    or orders[0].get("winning_status") != "loss"
                    or Decimal(str(orders[0].get("filled_quantity"))) != Decimal(str(receipt.get("filled_stake_usd")))
                    or Decimal(str(orders[0].get("open_quantity"))) != 0
                    or Decimal(str(wallet.get("matched_order_balance"))) != 0
                    or Decimal(str(wallet.get("unmatched_order_balance"))) != 0
                    or wallet.get("unmatched_order_balance_status") != "succeed"
                    or len(transactions) != 1 or transactions[0].get("transaction_type") != "TRADE"
                    or transactions[0].get("status") != "Completed"
                    or Decimal(str(transactions[0].get("change"))) != -Decimal(str(receipt.get("filled_stake_usd")))
                    or Decimal(str(transactions[0].get("balance"))) != Decimal(str(wallet.get("balance")))):
                raise ValueError("ProphetX external settlement or wallet inventory is incomplete")
        else:
            order = review.get("order", {})
            slug = receipt.get("market_slug")
            resolutions = [item.get("positionResolution", {}) for item in review.get("activities", [])
                           if item.get("type") == "ACTIVITY_TYPE_POSITION_RESOLUTION"
                           and item.get("positionResolution", {}).get("marketSlug") == slug]
            trades = [item.get("trade", {}) for item in review.get("activities", [])
                      if item.get("type") == "ACTIVITY_TYPE_TRADE"
                      and item.get("trade", {}).get("marketSlug") == slug]
            if (review.get("venue") != "polymarket_us" or len(resolutions) != 1
                    or len(trades) != 1 or trades[0].get("id") != receipt.get("trade_id")
                    or order.get("id") != row.native_order_id or order.get("marketSlug") != slug
                    or order.get("state") != "ORDER_STATE_FILLED"
                    or review.get("positions_eof") is not True
                    or review.get("activities_eof") is not True
                    or review.get("positions", {}).get(slug) is not None
                    or review.get("open_orders") or review.get("other_open_orders") != 0
                    or Decimal(str(order.get("cumQuantity"))) != Decimal(str(receipt.get("filled_quantity")))
                    or Decimal(str(order.get("leavesQuantity"))) != 0
                    or Decimal(str(resolutions[0].get("beforePosition", {}).get("netPositionDecimal")))
                        != Decimal(str(receipt.get("filled_quantity")))
                    or Decimal(str(resolutions[0].get("afterPosition", {}).get("netPositionDecimal"))) != 0
                    or Decimal(str(resolutions[0].get("beforePosition", {}).get("cost", {}).get("value")))
                        != Decimal(str(receipt.get("actual_debit_usd")))
                    or Decimal(str(resolutions[0].get("beforePosition", {}).get("baseCost", {}).get("value")))
                        != Decimal(str(receipt.get("position_cost_usd")))
                    or Decimal(str(resolutions[0].get("beforePosition", {}).get("fees", {}).get("value")))
                        != Decimal(str(receipt.get("actual_fee_usd")))
                    or Decimal(str(resolutions[0].get("afterPosition", {}).get("realized", {}).get("value")))
                        != -Decimal(str(receipt.get("position_cost_usd")))):
                raise ValueError("Polymarket US external position resolution is incomplete")

    for row in inventory.positions:
        if isinstance(row, ReviewedKalshiPosition):
            review = verified_json(row.review_artifact, row.review_sha256)
            fills = [item for item in review.get("fills", []) if item.get("order_id") == row.native_order_id]
            positions = [item for item in review.get("positions", [])
                         if item.get("ticker") == row.native_market_id]
            if (review.get("venue") != "kalshi" or review.get("account_fingerprint") != row.account_fingerprint
                    or review.get("subaccount") != 0 or len(fills) != 1 or len(positions) != 1
                    or row.sport != "other" or not row.native_market_id.startswith("KXSTREAMERAWARDS-")
                    or any(item.get("ticker") == row.native_market_id for item in review.get("settlements", []))):
                raise ValueError("Reviewed Kalshi external identity is incomplete")
            fill, position = fills[0], positions[0]
            try:
                matches = (fill.get("ticker") == row.native_market_id
                    and fill.get("side") == row.native_outcome_id and fill.get("action") == "buy"
                    and fill.get("exchange_index") == position.get("exchange_index") == row.exchange_index
                    and fill.get("subaccount_number") == 0
                    and Decimal(str(fill.get("count_fp"))) == Decimal(str(position.get("position_fp"))) == row.quantity
                    and Decimal(str(fill.get("yes_price_dollars") if row.native_outcome_id == "yes"
                                    else fill.get("no_price_dollars"))) * row.quantity == row.cost_usd
                    and Decimal(str(position.get("market_exposure_dollars"))) == row.cost_usd
                    and Decimal(str(position.get("total_traded_dollars"))) == row.cost_usd
                    and Decimal(str(fill.get("fee_cost"))) == Decimal(str(position.get("fees_paid_dollars"))) == row.fee_usd
                    and Decimal(str(position.get("realized_pnl_dollars"))) == 0
                    and datetime.fromisoformat(fill["created_time"]) == row.recorded_at)
            except (KeyError, TypeError, InvalidOperation) as exc:
                raise ValueError("Reviewed Kalshi external financials are malformed") from exc
            if not matches:
                raise ValueError("Reviewed Kalshi external financials differ from actual fill")
            continue

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
