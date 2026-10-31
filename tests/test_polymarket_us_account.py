"""Account fixtures do not establish actual account eligibility or permissions."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from evplatform.core.models import OrderStatus, utcnow
from evplatform.venues.polymarket_us_account import probe_polymarket_us_account, read_all_positions
from evplatform.venues.reconciliation import ReconciliationError


class ReadOnlyClient:
    venue_id="polymarket_us"
    settings=SimpleNamespace(environment="production")
    authenticated=True
    _ready={"private":True}

    def __init__(self, pages=None, orders=None):
        self.pages=list(pages if pages is not None else [{"positions":{},"eof":True}])
        self.orders=orders or []
        self.calls=[]

    async def account_balances(self):
        self.calls.append("balances")
        return {"balances":[{"currency":"USD","currentBalance":"10.29","buyingPower":"8.29",
                             "lastUpdated":(utcnow()-timedelta(seconds=1)).isoformat()}]}

    async def open_orders(self):
        self.calls.append("orders")
        return {"orders":self.orders}

    async def positions(self, **params):
        self.calls.append(("positions",params))
        return self.pages.pop(0)

    async def authenticated_participant(self):
        self.calls.append("participant")
        return {"rfqUserId":"participant-fixture"}


def baseline():
    return SimpleNamespace(private_generation=2,private_revision=10,private_snapshot_complete=True,private_orders={})


def position(net="3",cost="1.2",**kwargs):
    return {"netPositionDecimal":net,"netPosition":"999", "cost":{"value":cost,"currency":"USD"},
            "expired":False,"updateTime":(utcnow()-timedelta(seconds=1)).isoformat(),**kwargs}


def owned(quantity="3",side="yes",**kwargs):
    return SimpleNamespace(id="local-1",venue_order_id="native-1",venue="polymarket_us",mode="production",
        status=OrderStatus.FILLED,reconciliation_complete=True,filled_quantity=D(quantity),filled_cost_usd=D("1.2"),
        intent=SimpleNamespace(instrument=SimpleNamespace(instrument_id="sample-nba",native_outcome=side)),**kwargs)


async def test_empty_inventory_and_rfq_participant_never_fabricate_account_or_write_scope():
    client=ReadOnlyClient()
    report=await probe_polymarket_us_account(client,adapter=baseline(),include_participant=True)
    assert report.balance_usd==D("8.29") and report.open_order_count==report.position_count==0
    assert report.inventory_quantities_agree and report.inventory_cash_agrees
    assert report.participant_fingerprint and "participant-fixture" not in report.model_dump_json()
    assert not report.account_fingerprint and not report.identity_verified and not report.reconciled
    assert not report.trading_permission_verified and not report.cash_available_verified
    assert report.api_key_scopes==() and report.private_connection_generation==2


async def test_nonzero_balance_reservation_is_disclosed_without_spendable_cash_claim():
    client=ReadOnlyClient()
    original=client.account_balances
    async def reserved():
        result=await original()
        result["balances"][0]["balanceReservation"]="25"
        return result
    client.account_balances=reserved
    report=await probe_polymarket_us_account(client,adapter=baseline())
    assert "balanceReservation" in report.reason
    assert not report.cash_available_verified and not report.reconciled


async def test_missing_balance_source_clock_returns_unverified_inventory_diagnostic():
    client=ReadOnlyClient()
    original=client.account_balances
    async def no_clock():
        result=await original()
        result["balances"][0].pop("lastUpdated")
        return result
    client.account_balances=no_clock
    report=await probe_polymarket_us_account(client,adapter=baseline())
    assert report.balance_usd==D("8.29") and report.open_order_count==report.position_count==0
    assert not report.inventory_quantities_agree and not report.inventory_cash_agrees
    assert not report.cash_available_verified and not report.reconciled
    assert "Balance source clock is missing or invalid" in report.reason


@pytest.mark.parametrize("side,net",[("yes","3"),("no","-3")])
async def test_exact_fractional_signed_position_quantity_is_used_not_deprecated_rounded_value(side,net):
    client=ReadOnlyClient(pages=[{"positions":{"sample-nba":position(net=net)},"eof":True}])
    report=await probe_polymarket_us_account(client,[owned(side=side)],adapter=baseline())
    assert report.inventory_quantities_agree and report.position_count==1
    assert not report.inventory_cash_agrees and not report.reconciled


@pytest.mark.parametrize("change",["cost","quantity","external","expired","zero_cost","incomplete"])
async def test_changed_position_or_missing_durable_evidence_blocks_inventory(change):
    row=position(); local=owned()
    if change=="cost": row["cost"]["value"]="1.3"
    elif change=="quantity": row["netPositionDecimal"]="4"
    elif change=="expired": row["expired"]=True
    elif change=="zero_cost": row["netPositionDecimal"]="0"
    elif change=="incomplete": local.reconciliation_complete=False
    client=ReadOnlyClient(pages=[{"positions":{"sample-nba":row},"eof":True}])
    report=await probe_polymarket_us_account(client,[] if change=="external" else [local],adapter=baseline())
    assert not report.inventory_quantities_agree and not report.reconciled


async def test_equal_opposing_fills_cannot_hide_netting():
    yes,no=owned(),owned(side="no")
    no.id="local-2";no.venue_order_id="native-2"
    client=ReadOnlyClient()
    report=await probe_polymarket_us_account(client,[yes,no],adapter=baseline())
    assert not report.inventory_quantities_agree and "Opposing" in report.reason


async def test_private_change_during_rest_baseline_blocks_inventory():
    adapter=baseline();client=ReadOnlyClient()
    original=client.open_orders
    async def changed():
        adapter.private_revision+=1
        return await original()
    client.open_orders=changed
    report=await probe_polymarket_us_account(client,adapter=adapter)
    assert not report.inventory_quantities_agree and report.private_as_of is None


async def test_positions_pagination_is_explicit_complete_and_bounded():
    client=ReadOnlyClient(pages=[{"positions":{"a":position()},"eof":False,"nextCursor":"next"},
                               {"positions":{"b":position()},"eof":True}])
    assert set(await read_all_positions(client))=={"a","b"}
    assert client.calls[1]==("positions",{"limit":100,"cursor":"next"})


@pytest.mark.parametrize("pages",[
    [{"positions":{}}], [{"positions":{},"eof":False}],
    [{"positions":{"a":{}},"eof":False,"nextCursor":"x"},{"positions":{"a":{}},"eof":True}],
    [{"positions":{},"eof":False,"nextCursor":"x"},{"positions":{},"eof":False,"nextCursor":"x"}],
    [{"positions":{},"eof":False,"nextCursor":str(n)} for n in range(20)],
])
async def test_incomplete_repeated_and_unbounded_account_pages_reject(pages):
    with pytest.raises(ReconciliationError):
        await read_all_positions(ReadOnlyClient(pages=deepcopy(pages)))


async def test_missing_decimal_or_future_timestamp_is_not_a_valid_position():
    for change in ("decimal","clock"):
        row=position()
        if change=="decimal": row.pop("netPositionDecimal")
        else: row["updateTime"]=(utcnow()+timedelta(days=1)).isoformat()
        with pytest.raises(ReconciliationError):
            await probe_polymarket_us_account(ReadOnlyClient(pages=[{"positions":{"sample-nba":row},"eof":True}]),adapter=baseline())


async def test_unmanaged_open_order_is_reported_without_binding_a_similar_intent():
    client=ReadOnlyClient(orders=[{"id":"unmanaged-order"}])
    report=await probe_polymarket_us_account(client,adapter=baseline())
    assert report.open_order_count==1 and not report.inventory_quantities_agree
    assert "Unmanaged native open order" in report.reason
