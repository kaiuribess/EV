"""Offline official-shape fixtures; no native connection or payment proof."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from evplatform.core.conflicts import reviewed_correlations, risk_family
from evplatform.core.models import OrderStatus
from evplatform.core.native_accounting import NativeInstrument, NativeOrderIntent
from evplatform.core.native_oms import NativeOrder
from evplatform.venues.prophetx_account import probe_prophetx_account
from evplatform.venues.prophetx_financial import (
    ProphetXAccountReview, complete_order_history, normalize_financial_order,
    prophetx_fingerprint, reconcile_prophetx_financial, reporting_pages,
    source_time, validate_wallet_rows,
)
from evplatform.venues.prophetx_settlement import reconcile_prophetx_settlement
from evplatform.venues.reconciliation import ReconciliationError

NOW = datetime(2026, 10, 21, 2, tzinfo=timezone.utc)
USER = "11111111-1111-4111-8111-111111111111"
OID = "partner_offline-owned-order"


def iso(seconds=0):
    return (NOW+timedelta(seconds=seconds)).isoformat()


class Client:
    venue_id = "prophetx"
    settings = SimpleNamespace(environment="sandbox")
    _signin_confirmed = True
    _authenticated_user_id = USER

    def __init__(self, row, fills, wallet, balance):
        self.row, self.fills, self.wallet, self.balance = row, fills, wallet, balance
        self._last_sequences = {"private": 12}
        self.calls = []
        self.overrides = {}

    def health_check(self):
        return {"authenticated": True}

    async def reconcile_order(self, order_id):
        assert order_id == OID
        return {"data": deepcopy(self.row)}

    async def fetch_balance(self):
        return {"data": deepcopy(self.balance)}

    async def request(self, method, path, *, params):
        assert method == "GET"
        self.calls.append((path, params))
        if path in self.overrides:
            return self.overrides[path](params)
        if path.endswith("get_order_history"):
            stamp = datetime.fromisoformat(self.row["created_at"]).timestamp()
            rows = [self.row] if params["from"] <= stamp <= params["to"] else []
            return {"data": {"orders": deepcopy(rows), "next_cursor": ""}, "last_synced_at": iso()}
        if path.endswith("get_order_matched_detail"):
            return {"data": {"matching_details": deepcopy(self.fills), "next_cursor": ""}, "last_synced_at": iso()}
        if path.endswith("get_transactions"):
            return {"data": {"transactions": deepcopy(self.wallet)}}
        raise AssertionError(path)


@pytest.fixture
def case(market):
    instrument = NativeInstrument(venue="prophetx", instrument_id="strike-fixture", canonical_market_id=market.id,
        outcome="home", native_outcome="101", quantity_unit="usd_stake", price_format="american",
        payout_per_unit_usd=None, quantity_step=".01", minimum_quantity="1", cash_quantum_usd=".01",
        settlement_rules_fingerprint=market.identity(), verified=True, source="OFFLINE fixture", price_bands=())
    intent = NativeOrderIntent(id="owned", client_order_id="client-owned", instrument=instrument, quantity="10",
        limit_price="120", max_fee_usd=".5", fee_source="OFFLINE explicit cap", fees_verified=True,
        mode="sandbox", created_at=NOW-timedelta(seconds=10))
    order = NativeOrder(intent=intent, candidate_id="candidate", strategy="native_positive_ev",
        account_fingerprint=prophetx_fingerprint("sandbox", USER), market_snapshot=market,
        market_fingerprint=market.identity(), risk_family_id=risk_family(market),
        reviewed_correlation_groups=reviewed_correlations(market), status=OrderStatus.FILLED,
        filled_quantity="10", filled_cost_usd="10", gross_payout_usd="22", fees=".2",
        venue_order_id=OID, reconciliation_complete=True)
    row = dict(order_id=OID, external_id=intent.client_order_id, user_id=USER, strike_id="strike-fixture",
        market_id=219, original_quantity="10", filled_quantity="10", open_quantity="0", quantity="10",
        status="open", matching_status="fully_matched", winning_status="tbd", price=120,
        profit="12", fee=".2", created_at=iso(-10), updated_at=iso(-1))
    fills = [dict(id=1, order_id=OID, price=120, quantity="10", profit="12", matched_at=iso(-6))]
    wallet = [dict(details="match-1", user_id=USER, trade_id=OID, market_id="219", status="Completed",
        transaction_type="TRADE", transaction_sub_type="OTHER", amount="10", change="-10",
        balance_before="100", balance="90", created_at=iso(-6)),
        dict(details="commission-1", user_id=USER, trade_id=OID, market_id="219", status="Completed",
        transaction_type="COMMISSION", transaction_sub_type="COMMISSION", amount=".2", change="-.2",
        balance_before="90", balance="89.8", created_at=iso(-5))]
    balance = dict(balance="89.8", gec_balance="0", matched_order_balance="10", unmatched_order_balance="0",
        unmatched_order_balance_status="succeed", unmatched_order_last_synced_at=iso())
    review = ProphetXAccountReview(account_fingerprint=order.account_fingerprint,
        history_created_from=NOW-timedelta(days=8), history_scope_evidence="OFFLINE account opening fixture",
        funded_cash_only=True, funding_scope_evidence="OFFLINE no GEC or LEC fixture", valid_until=NOW+timedelta(days=1))
    return order, Client(row, fills, wallet, balance), review


def normalize(case):
    order, client, _ = case
    return normalize_financial_order(order.intent, client.row, client.fills, client.wallet,
        user_id=USER, expected_order_id=OID, observed_at=NOW, reporting_watermark=NOW)


async def test_actual_fills_and_cash_commission_make_cumulative_evidence(case):
    order, client, _ = case
    result = await reconcile_prophetx_financial(client, order.intent, OID, observed_at=NOW)
    assert result.financial_complete and result.terminal and result.status == "filled"
    assert result.snapshot.cumulative_quantity == result.snapshot.cumulative_cost_usd == 10
    assert result.snapshot.cumulative_gross_payout_usd == 22 and result.snapshot.cumulative_fee_usd == D(".2")
    assert result.snapshot.observed_at == NOW-timedelta(seconds=1)
    assert not result.fills  # Never allocate an aggregate commission to made-up fills.
    assert all("order_id" in args or "trade_id" in args for _, args in client.calls)


@pytest.mark.parametrize("field,value", [("fee", None), ("fee", "0"), ("fee", ".6"),
    ("user_id", "other"), ("strike_id", "other"), ("original_quantity", "11"),
    ("filled_quantity", "9"), ("updated_at", iso(1)), ("created_at", iso(-30))])
def test_missing_fee_identity_or_quantity_never_clears_accounting(case, field, value):
    case[1].row[field] = value
    with pytest.raises((ReconciliationError, ValueError)):
        normalize(case)


@pytest.mark.parametrize("field,value", [("profit", "11"), ("quantity", "9"), ("order_id", "other"),
    ("matched_at", iso(1)), ("matched_at", iso(-30)), ("price", 110), ("id", None)])
def test_fill_payout_limit_identity_and_clocks_fail_closed(case, field, value):
    case[1].fills[0][field] = value
    with pytest.raises((ReconciliationError, ValueError)):
        normalize(case)


def test_actual_rounded_american_profit_is_preserved(case):
    order, client, review = case
    order = order.model_copy(update={"intent": order.intent.model_copy(update={"limit_price": D(-110)})})
    client.row["price"] = -110
    client.fills[0].update(price=-110, profit="9.09")
    assert normalize((order, client, review)).snapshot.cumulative_gross_payout_usd == D("19.09")


def test_partial_fill_killed_remainder_preserves_actual_cash(case):
    client = case[1]
    client.row.update(filled_quantity="4", quantity="4", fee=".1")
    client.fills[0].update(quantity="4", profit="4.8")
    client.wallet[0].update(amount="4", change="-4", balance="96")
    client.wallet[1].update(amount=".1", change="-.1", balance_before="96", balance="95.9")
    result = normalize(case)
    assert result.status == "cancelled" and result.terminal and result.financial_complete
    assert result.snapshot.cumulative_cost_usd == 4 and result.snapshot.cumulative_gross_payout_usd == D("8.8")


@pytest.mark.parametrize("field,value", [("id", -1), ("id", "1"), ("id", True)])
def test_matched_ids_are_documented_positive_integers(case, field, value):
    case[1].fills[0][field] = value
    with pytest.raises(ReconciliationError):
        normalize(case)


@pytest.mark.parametrize("field,value", [("user_id", "other"), ("trade_id", "other"), ("market_id", "220"),
    ("status", "Pending"), ("transaction_sub_type", "RESETTLEMENT"), ("transaction_type", "VOID"),
    ("change", ".2"), ("balance", "89.7"), ("amount", "0"), ("details", ""), ("created_at", 1731230000)])
def test_wallet_cash_identity_and_corrections_fail_closed(case, field, value):
    case[1].wallet[1][field] = value
    with pytest.raises((ReconciliationError, ValueError)):
        normalize(case)


def test_missing_wallet_commission_cannot_be_inferred_from_fee_rate(case):
    case[1].wallet.pop()
    with pytest.raises(ReconciliationError, match="commission"):
        normalize(case)


def test_exact_duplicate_history_is_idempotent_conflicting_duplicate_is_blocked(case):
    client = case[1]
    client.fills.append(deepcopy(client.fills[0]))
    client.wallet.append(deepcopy(client.wallet[0]))
    assert normalize(case).snapshot.cumulative_quantity == 10
    client.wallet[-1]["amount"] = "9"
    with pytest.raises(ReconciliationError, match="Conflicting"):
        normalize(case)


@pytest.mark.parametrize("clock", [1731230000, "2026-10-21T02:00:00", iso(1), iso(-11), None])
def test_source_timestamp_is_not_replaced_with_local_receipt(clock):
    with pytest.raises(ReconciliationError):
        source_time(clock, observed_at=NOW, name="test", max_age=timedelta(seconds=10))


async def test_pagination_follows_current_query_name_and_complete_watermarks(case):
    client = case[1]
    client.overrides["/v4/mm/get_order_matched_detail"] = lambda p: {"last_synced_at": iso(), "data":
        {"matching_details": [client.fills[0]], "next_cursor": "page2" if "next_cursor" not in p else ""}}
    history = await reporting_pages(client, "/v4/mm/get_order_matched_detail", "matching_details", observed_at=NOW)
    assert len(history.rows) == 2 and history.last_synced_at == NOW
    assert client.calls[-1][1]["next_cursor"] == "page2"


@pytest.mark.parametrize("response", [
    {"data": {"matching_details": [], "next_cursor": None}, "last_synced_at": iso()},
    {"data": {"matching_details": []}, "last_synced_at": iso(-20)},
    {"data": {}, "last_synced_at": iso()},
    {"data": {"matching_details": [], "next_cursor": "loop"}, "last_synced_at": iso()},
])
async def test_incomplete_stale_looping_reporting_stays_blocked(case, response):
    client = case[1]
    client.overrides["/v4/mm/get_order_matched_detail"] = lambda _: response
    with pytest.raises(ReconciliationError):
        await reporting_pages(client, "/v4/mm/get_order_matched_detail", "matching_details", observed_at=NOW)


async def test_complete_history_spans_creation_windows_and_all_pages(case):
    _, client, review = case
    history = await complete_order_history(client, review, observed_at=NOW)
    assert len(history.rows) == 1 and len(client.calls) == 2
    ranges = [params for _, params in client.calls]
    assert ranges[0]["to"] == ranges[1]["from"]
    assert all(p["to"]-p["from"] <= 7*86400 for p in ranges)


@pytest.mark.parametrize("updates", [{"funded_cash_only": False}, {"account_fingerprint": "0"*64},
    {"valid_until": NOW}, {"history_created_from": NOW}, {"history_created_from": NOW-timedelta(days=1000)}])
async def test_history_review_is_account_specific_current_and_bounded(case, updates):
    _, client, review = case
    with pytest.raises(ReconciliationError):
        await complete_order_history(client, review.model_copy(update=updates), observed_at=NOW)


async def test_inventory_matches_owned_orders_positions_and_cash(case):
    order, client, review = case
    result = await probe_prophetx_account(client, [order], review=review, observed_at=NOW)
    assert result.reconciled and result.position_count == 1 and result.open_order_count == 0
    assert result.balance_usd == D("89.8") and not result.trading_permission_verified
    assert result.cash_available_verified


async def test_inventory_missing_history_review_never_assumes_empty_account(case):
    order, client, _ = case
    result = await probe_prophetx_account(client, [order], observed_at=NOW)
    assert not result.reconciled and result.position_count is None and not result.cash_available_verified


async def test_inventory_external_position_missing_managed_order_and_private_change_block(case):
    order, client, review = case
    result = await probe_prophetx_account(client, [], review=review, observed_at=NOW)
    assert not result.reconciled and "Unmanaged" in result.reason
    client.row["order_id"] = "external"
    result = await probe_prophetx_account(client, [order], review=review, observed_at=NOW)
    assert not result.reconciled and "missing" in result.reason
    client.row["order_id"] = OID
    original = client.fetch_balance
    async def changed_balance():
        client._last_sequences["private"] += 1
        return await original()
    client.fetch_balance = changed_balance
    result = await probe_prophetx_account(client, [order], review=review, observed_at=NOW)
    assert not result.reconciled and "private account changed" in result.reason


@pytest.mark.parametrize("field,value", [("gec_balance", "1"), ("unmatched_order_balance_status", "failed"),
    ("unmatched_order_last_synced_at", iso(-20))])
async def test_inventory_credit_and_unverified_wallet_freshness_rejected(case, field, value):
    order, client, review = case
    client.balance[field] = value
    with pytest.raises(ReconciliationError):
        await probe_prophetx_account(client, [order], review=review, observed_at=NOW)


async def test_aggregate_wallet_exposure_mismatch_blocks_inventory(case):
    order, client, review = case
    client.balance["matched_order_balance"] = "11"
    result = await probe_prophetx_account(client, [order], review=review, observed_at=NOW)
    assert not result.reconciled and "wallet exposure differs" in result.reason and not result.cash_available_verified


async def test_empty_account_requires_complete_history_and_actual_zero_exposure(case):
    _, client, review = case
    client.balance.update(matched_order_balance="0", unmatched_order_balance="0")
    client.overrides["/v4/mm/get_order_history"] = lambda _: {"data": {"orders": [], "next_cursor": ""}, "last_synced_at": iso()}
    result = await probe_prophetx_account(client, [], review=review, observed_at=NOW)
    assert result.reconciled and result.cash_available_verified and result.position_count == result.open_order_count == 0


async def test_changed_native_limit_price_does_not_clear_inventory(case):
    order, client, review = case
    client.row["price"] = 125
    result = await probe_prophetx_account(client, [order], review=review, observed_at=NOW)
    assert not result.reconciled and not result.cash_available_verified


def close_win(case):
    client = case[1]
    client.row.update(status="closed", winning_status="profit", settled_at=iso(-2))
    client.wallet.append(dict(details="pay-1", user_id=USER, trade_id=OID, market_id="219", status="Completed",
        transaction_type="PAY", transaction_sub_type="OTHER", amount="22", change="22",
        balance_before="89.8", balance="111.8", created_at=iso(-1)))


async def test_settlement_uses_actual_cash_and_stable_revision(case):
    order, client, review = case
    close_win(case)
    result = await reconcile_prophetx_settlement(client, order, [order], review=review, observed_at=NOW)
    assert result.payout_usd == 22 and result.additional_fee_usd == result.fee_refund_usd == 0
    assert result.settled_at == NOW-timedelta(seconds=2)
    again = await reconcile_prophetx_settlement(client, order, [order], review=review, observed_at=NOW)
    assert result.settlement_id == again.settlement_id


async def test_closed_loss_has_zero_cash_and_not_potential_profit(case):
    order, client, review = case
    client.row.update(status="closed", winning_status="loss", settled_at=iso(-2))
    result = await reconcile_prophetx_settlement(client, order, [order], review=review, observed_at=NOW)
    assert result.payout_usd == 0


async def test_settled_before_wallet_payment_is_not_closed_cash(case):
    order, client, review = case
    client.row["status"] = "settled"
    assert await reconcile_prophetx_settlement(client, order, [order], review=review, observed_at=NOW) is None


async def test_settlement_disagreeing_fill_grade_and_new_credit_fail_closed(case):
    order, client, review = case
    close_win(case)
    client.fills[0]["winning_status"] = "void"
    with pytest.raises(ReconciliationError, match="grade contradicts"):
        await reconcile_prophetx_settlement(client, order, [order], review=review, observed_at=NOW)
    client.fills[0]["winning_status"] = "profit"
    client.balance["gec_balance"] = "1"
    with pytest.raises(ReconciliationError, match="exposure-credit"):
        await reconcile_prophetx_settlement(client, order, [order], review=review, observed_at=NOW)


@pytest.mark.parametrize("mutation", ["no_pay", "push", "manual", "wrong_fee", "unmanaged", "no_review", "pending_pay"])
async def test_settlement_ambiguous_cash_or_scope_is_blocked(case, mutation):
    order, client, review = case
    close_win(case)
    managed = [order]
    if mutation == "no_pay": client.wallet.pop()
    if mutation == "push": client.row["winning_status"] = "push"
    if mutation == "manual": client.row["status"] = "manually_settled"
    if mutation == "wrong_fee": order = order.model_copy(update={"fees": D(".1")})
    if mutation == "unmanaged": managed.append(order.model_copy(update={"intent": order.intent.model_copy(update={"id": "other"})}))
    if mutation == "no_review": review = None
    if mutation == "pending_pay": client.wallet[-1]["status"] = "Pending"
    with pytest.raises(ReconciliationError):
        await reconcile_prophetx_settlement(client, order, managed, review=review, observed_at=NOW)
