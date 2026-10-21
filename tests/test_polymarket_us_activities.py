"""Official retail fixtures; no authenticated production lifecycle certification."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from evplatform.core.models import utcnow
from evplatform.venues.polymarket_us_activities import activity_read_guard, capture_activities, parse_activity


def trade(*, identity="trade-1", state="TRADE_STATE_CLEARED"):
    at = (utcnow()-timedelta(seconds=1)).isoformat()
    return {"type": "ACTIVITY_TYPE_TRADE", "trade": {"id": identity, "marketSlug": "market-1", "state": state,
        "createTime": at, "updateTime": at, "qty": "1", "qtyDecimal": "1.25", "isAggressor": True,
        "price": {"value": "0.4444", "currency": "USD"}, "realizedPnl": {"value": "-0.03", "currency": "USD"}}}


def resolution():
    return {"type": "ACTIVITY_TYPE_POSITION_RESOLUTION", "positionResolution": {"tradeId": "trade-1",
        "marketSlug": "market-1", "updateTime": (utcnow()-timedelta(seconds=1)).isoformat(),
        "side": "POSITION_RESOLUTION_SIDE_SHORT", "beforePosition": {"netPositionDecimal": "-1.25", "netPosition": "-1"},
        "afterPosition": {"netPositionDecimal": "0", "netPosition": "0", "expired": True}}}


def balance():
    at = (utcnow()-timedelta(seconds=1)).isoformat()
    return {"type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "accountBalanceChange": {"transactionId": "transaction-1",
        "createTime": at, "updateTime": at, "status": "ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED",
        "amount": {"value": "25.00", "currency": "USD"}}}


def test_exact_fractional_trade_and_signed_diagnostic_pnl():
    row = parse_activity(trade(), observed_at=utcnow())
    assert row.quantity == Decimal("1.25") and row.yes_price == Decimal(".4444")
    assert row.realized_pnl_usd == Decimal("-.03") and row.trade_id == "trade-1"


def test_short_resolution_preserves_position_sign_not_payout_or_cash():
    row = parse_activity(resolution(), observed_at=utcnow())
    assert row.before.net_quantity == Decimal("-1.25") and row.after.net_quantity == 0
    assert not row.paid_cash_verified and row.after.cash_value_usd is None


def test_completed_deposit_does_not_join_by_trade_id_time_or_money():
    row = parse_activity(balance(), observed_at=utcnow())
    assert row.amount_usd == 25 and not row.trade_link_verified
    assert not hasattr(row, "trade_id")


@pytest.mark.parametrize("change", ["missing_decimal", "float", "nan", "bad_id", "unknown_state", "currency", "future", "reverse_clock", "conflicting_body", "wrong_type"])
def test_invalid_or_rounded_trade_evidence_is_rejected(change):
    raw = trade()
    body = raw["trade"]
    if change == "missing_decimal": body.pop("qtyDecimal")
    elif change == "float": body["qtyDecimal"] = 1.25
    elif change == "nan": body["qtyDecimal"] = "NaN"
    elif change == "bad_id": body["id"] = "bad/id"
    elif change == "unknown_state": body["state"] = "CLEARED"
    elif change == "currency": body["price"]["currency"] = "EUR"
    elif change == "future": body["updateTime"] = (utcnow()+timedelta(days=1)).isoformat()
    elif change == "reverse_clock": body["updateTime"] = (utcnow()-timedelta(days=1)).isoformat()
    elif change == "conflicting_body": raw["positionResolution"] = resolution()["positionResolution"]
    else: raw["type"] = "ACTIVITY_TYPE_UNKNOWN"
    with pytest.raises(ValueError): parse_activity(raw, observed_at=utcnow())


@pytest.mark.parametrize("change", ["rounded_only", "wrong_market", "missing_trade", "wrong_side"])
def test_resolution_requires_exact_market_and_trade_evidence(change):
    raw = resolution()
    body = raw["positionResolution"]
    if change == "rounded_only": body["beforePosition"].pop("netPositionDecimal")
    elif change == "wrong_market": body["beforePosition"]["marketMetadata"] = {"slug": "other"}
    elif change == "missing_trade": body.pop("tradeId")
    else: body["side"] = "YES"
    with pytest.raises(ValueError): parse_activity(raw, observed_at=utcnow())


class Pages:
    venue_id = "polymarket_us"
    settings = SimpleNamespace(environment="production")
    authenticated = True

    def __init__(self, pages):
        self.pages, self.calls = iter(pages), []

    async def activities(self, **params):
        self.calls.append(params)
        value = next(self.pages)
        if isinstance(value, Exception): raise value
        return value


async def test_pagination_preserves_bust_versions_and_idempotent_duplicates():
    first = trade()
    bust = deepcopy(first)
    bust["trade"].update(state="TRADE_STATE_BUSTED", updateTime=utcnow().isoformat())
    client = Pages([{"activities": [first], "nextCursor": "page2", "eof": False},
        {"activities": [first, bust, resolution(), balance()], "eof": True}])
    result = await capture_activities(client)
    assert result["complete"] and result["duplicate_rows"] == 1 and len(result["parsed"]) == 4
    assert client.calls[1]["cursor"] == "page2"
    assert [x["state"] for x in result["parsed"] if x["kind"] == "trade"] == ["TRADE_STATE_CLEARED", "TRADE_STATE_BUSTED"]
    assert not result["settlement_cash_verified"]


@pytest.mark.parametrize("pages", [
    [{"activities": [], "nextCursor": "unused"}], [{"activities": [], "eof": 1}],
    [{"activities": [], "eof": False}], [{"activities": [], "eof": False, "nextCursor": None}],
    [{"activities": [], "eof": False, "nextCursor": "same"}, {"activities": [], "eof": False, "nextCursor": "same"}],
    [{"activities": [], "eof": False, "nextCursor": "one"}, {"activities": [], "eof": False, "nextCursor": "two"}],
    [RuntimeError("sensitive transport detail must not be exported")],
])
async def test_failed_malformed_and_truncated_history_remains_incomplete(pages):
    result = await capture_activities(Pages(pages), max_pages=2)
    assert not result["complete"] and result["errors"]
    assert "sensitive" not in str(result["errors"])


async def test_conflicting_same_revision_is_not_silently_deduplicated():
    first = trade()
    second = deepcopy(first)
    second["trade"]["qtyDecimal"] = "1.26"
    result = await capture_activities(Pages([{"activities": [first, second], "eof": True}]))
    assert result["pagination_complete"] and not result["complete"]


@pytest.mark.parametrize("second_kind", ["same_revision", "later_revision", "resolution"])
async def test_trade_identity_cannot_change_market_across_revisions_or_resolution(second_kind):
    first = trade()
    second = resolution() if second_kind == "resolution" else deepcopy(first)
    body = second["positionResolution"] if second_kind == "resolution" else second["trade"]
    body["marketSlug"] = "different-market"
    if second_kind == "later_revision": body["updateTime"] = utcnow().isoformat()
    result = await capture_activities(Pages([{"activities": [first, second], "eof": True}]))
    assert result["pagination_complete"] and not result["complete"] and result["errors"]


async def test_unknown_activity_preserves_raw_but_never_claims_complete():
    raw = {"activities": [{"type": "future_kind"}], "eof": True}
    result = await capture_activities(Pages([raw]))
    assert result["pages"][0]["response"] == raw and not result["complete"]


@pytest.mark.parametrize("url,method", [("https://api.polymarket.us/v1/orders", "POST"),
    ("https://api.polymarket.us/v1/portfolio/activities", "POST"),
    ("https://api.prod.polymarketexchange.com/v1/funding/balance-ledger", "GET"),
    ("http://api.polymarket.us/v1/portfolio/activities", "GET"),
    ("https://api.polymarket.us:444/v1/portfolio/activities", "GET")])
async def test_capture_boundary_cannot_transmit_orders_or_use_institutional_api(url, method):
    with pytest.raises(ValueError): await activity_read_guard(httpx.Request(method, url))


async def test_only_authentic_retail_read_scope_allowed():
    await activity_read_guard(httpx.Request("GET", "https://api.polymarket.us/v1/portfolio/activities"))
    client = Pages([])
    client.authenticated = False
    with pytest.raises(ValueError): await capture_activities(client)
