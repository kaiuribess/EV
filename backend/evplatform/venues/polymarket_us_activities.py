"""Diagnostic retail history with exact IDs; never proof of paid settlement.

Official contract: https://docs.polymarket.us/api-reference/portfolio/get-activities
Trades and position resolutions have trade IDs. Balance activities have a
transaction ID but no documented trade/order/market binding. This reader never
joins by time or money, emits NativeSettlement, or changes execution readiness.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from ..core.models import utcnow
from ..core.native_accounting import ExactDecimal, NativeContract, Nonnegative, Positive
from .polymarket_us import validated_slugs
from .polymarket_us_execution import native_id, source_time
from .reconciliation import ReconciliationError


class TradeActivity(NativeContract):
    kind: Literal["trade"] = "trade"
    trade_id: str
    market_slug: str
    state: Literal["TRADE_STATE_NEW", "TRADE_STATE_CLEARED", "TRADE_STATE_BUSTED", "TRADE_STATE_INFLIGHT",
        "TRADE_STATE_PENDING_RISK", "TRADE_STATE_PENDING_CLEARED", "TRADE_STATE_REJECTED"]
    quantity: Positive
    yes_price: Nonnegative = Field(le=1)
    cost_basis_usd: ExactDecimal | None = None
    realized_pnl_usd: ExactDecimal | None = None
    is_aggressor: bool
    created_at: datetime
    updated_at: datetime


class PositionDiagnostic(NativeContract):
    net_quantity: ExactDecimal
    cost_usd: ExactDecimal | None = None
    realized_usd: ExactDecimal | None = None
    cash_value_usd: ExactDecimal | None = None
    expired: bool | None = None


class ResolutionActivity(NativeContract):
    kind: Literal["resolution"] = "resolution"
    trade_id: str
    market_slug: str
    side: Literal["POSITION_RESOLUTION_SIDE_LONG", "POSITION_RESOLUTION_SIDE_SHORT"]
    before: PositionDiagnostic
    after: PositionDiagnostic
    updated_at: datetime
    paid_cash_verified: Literal[False] = False


class BalanceActivity(NativeContract):
    kind: Literal["balance"] = "balance"
    activity_type: str
    transaction_id: str
    status: Literal["ACCOUNT_BALANCE_CHANGE_STATUS_PENDING", "ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED",
        "ACCOUNT_BALANCE_CHANGE_STATUS_PARTIALLY_REFUNDED", "ACCOUNT_BALANCE_CHANGE_STATUS_REJECTED"]
    amount_usd: ExactDecimal
    created_at: datetime
    updated_at: datetime
    trade_link_verified: Literal[False] = False


_BALANCE_TYPES = {"ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "ACTIVITY_TYPE_ACCOUNT_ADVANCED_DEPOSIT",
    "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL", "ACTIVITY_TYPE_REFERRAL_BONUS", "ACTIVITY_TYPE_TRANSFER",
    "ACTIVITY_TYPE_TAKER_FEE_REBATE", "ACTIVITY_TYPE_LIQUIDITY_PROGRAM"}


async def activity_read_guard(request):
    if (request.method != "GET" or request.url.scheme != "https" or request.url.host != "api.polymarket.us"
            or request.url.port not in {None, 443} or request.url.userinfo
            or request.url.path not in {"/v1/account/balances", "/v1/portfolio/activities", "/v1/rfqs/user-id"}):
        raise ValueError("Retail activity capture rejected a non-allowlisted request")


def _exact(value):
    # The retail schema specifies decimal strings. Do not accept rounded legacy
    # integer quantities or a binary-float conversion as exact history evidence.
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ReconciliationError("Expected exact retail decimal string")
    try:
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError
        return result
    except (ValueError, ArithmeticError):
        raise ReconciliationError("Invalid retail decimal") from None


def _money(value):
    if not isinstance(value, dict) or value.get("currency") != "USD":
        raise ReconciliationError("Expected USD retail amount")
    return _exact(value.get("value"))


def _optional_money(raw, name):
    return _money(raw[name]) if raw.get(name) is not None else None


def _position(raw, slug):
    if not isinstance(raw, dict):
        raise ReconciliationError("Missing retail resolution position")
    metadata = raw.get("marketMetadata")
    if metadata is not None and (not isinstance(metadata, dict) or metadata.get("slug") not in {None, slug}):
        raise ReconciliationError("Resolution position market mismatch")
    if "expired" in raw and not isinstance(raw["expired"], bool):
        raise ReconciliationError("Invalid resolution expired flag")
    return PositionDiagnostic(net_quantity=_exact(raw.get("netPositionDecimal")),
        cost_usd=_optional_money(raw, "cost"), realized_usd=_optional_money(raw, "realized"),
        cash_value_usd=_optional_money(raw, "cashValue"), expired=raw.get("expired"))


def parse_activity(raw, *, observed_at):
    if not isinstance(raw, dict):
        raise ReconciliationError("Invalid retail activity")
    kind = raw.get("type")
    body_key = "trade" if kind == "ACTIVITY_TYPE_TRADE" else "positionResolution" if kind == "ACTIVITY_TYPE_POSITION_RESOLUTION" else "accountBalanceChange" if kind in _BALANCE_TYPES else None
    if body_key is None or not isinstance(raw.get(body_key), dict):
        raise ReconciliationError("Unknown or incomplete retail activity")
    if any(raw.get(key) not in (None, {}) for key in {"trade", "positionResolution", "accountBalanceChange"}-{body_key}):
        raise ReconciliationError("Conflicting retail activity bodies")
    body = raw[body_key]
    updated = source_time(body.get("updateTime"), observed_at)
    if body_key == "positionResolution":
        slug = body.get("marketSlug")
        if not isinstance(slug, str):
            raise ReconciliationError("Missing resolution market")
        validated_slugs([slug])
        return ResolutionActivity(trade_id=native_id(body.get("tradeId")), market_slug=slug,
            side=body.get("side"), before=_position(body.get("beforePosition"), slug),
            after=_position(body.get("afterPosition"), slug), updated_at=updated)
    created = source_time(body.get("createTime"), observed_at)
    if created > updated:
        raise ReconciliationError("Retail activity update predates creation")
    if body_key == "trade":
        slug = body.get("marketSlug")
        if not isinstance(slug, str) or not isinstance(body.get("isAggressor"), bool):
            raise ReconciliationError("Invalid retail trade identity or role")
        validated_slugs([slug])
        return TradeActivity(trade_id=native_id(body.get("id")), market_slug=slug, state=body.get("state"),
            quantity=_exact(body.get("qtyDecimal")), yes_price=_money(body.get("price")),
            cost_basis_usd=_optional_money(body, "costBasis"), realized_pnl_usd=_optional_money(body, "realizedPnl"),
            is_aggressor=body["isAggressor"], created_at=created, updated_at=updated)
    return BalanceActivity(activity_type=kind, transaction_id=native_id(body.get("transactionId")),
        status=body.get("status"), amount_usd=_money(body.get("amount")), created_at=created, updated_at=updated)


async def capture_activities(client, *, max_pages=4, limit=100):
    if (isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 20
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100):
        raise ValueError("Activity capture requires bounded page limits")
    if client.venue_id != "polymarket_us" or client.settings.environment != "production" or not client.authenticated:
        raise ValueError("Authenticated Polymarket US retail account required")
    pages, parsed, seen, cursors, errors, trade_markets = [], [], {}, set(), [], {}
    cursor, pagination_complete, duplicates = None, False, 0
    for _ in range(max_pages):
        try:
            raw = await client.activities(limit=limit, **({"cursor": cursor} if cursor else {}))
        except Exception as exc:
            errors.append({"error": type(exc).__name__, "http_status": getattr(exc, "status_code", None)})
            break
        at = utcnow()
        pages.append({"received_at": at.isoformat(), "response": raw})
        if not isinstance(raw, dict) or not isinstance(raw.get("activities"), list) or not isinstance(raw.get("eof"), bool):
            errors.append({"error": "invalid_activity_page"})
            break
        for index, activity in enumerate(raw["activities"]):
            try:
                value = parse_activity(activity, observed_at=at)
                if value.kind in {"trade", "resolution"}:
                    if value.trade_id in trade_markets and trade_markets[value.trade_id] != value.market_slug:
                        raise ReconciliationError("Exchange trade ID changed market binding")
                    trade_markets[value.trade_id] = value.market_slug
                key = (value.kind, getattr(value, "trade_id", getattr(value, "transaction_id", None)),
                    getattr(value, "market_slug", None), getattr(value, "side", None), value.updated_at)
                if key in seen:
                    if seen[key] != value:
                        raise ReconciliationError("Conflicting activity revision")
                    duplicates += 1
                else:
                    seen[key] = value
                    parsed.append(value)
            except (ValueError, TypeError) as exc:
                errors.append({"page": len(pages), "row": index, "error": type(exc).__name__})
        if raw["eof"] is True:
            pagination_complete = True
            break
        cursor = raw.get("nextCursor")
        if not isinstance(cursor, str) or not cursor.strip() or cursor in cursors:
            errors.append({"error": "invalid_activity_cursor"})
            break
        cursors.add(cursor)
    else:
        errors.append({"error": "activity_page_bound"})
    return {"pages": pages, "parsed": [row.model_dump(mode="json") for row in parsed],
        "pagination_complete": pagination_complete, "complete": pagination_complete and not errors,
        "duplicate_rows": duplicates, "errors": errors, "settlement_cash_verified": False,
        "note": "Served history only. Trade revisions and resolutions are diagnostic; no retail cash-posting join is established."}
