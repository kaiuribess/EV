"""Paper connector probe input gates; no live account or network is used."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from evplatform.core.models import utcnow
from scripts.probe_novig_paper_order import candidate_from_catalog, MAX_FEE, QUANTITY
from scripts import fund_novig_paper_probe as funding


M = "11111111-1111-4111-8111-111111111111"
E = "22222222-2222-4222-8222-222222222222"
A = "33333333-3333-4333-8333-333333333333"
B = "44444444-4444-4444-8444-444444444444"


def catalog():
    starts = int((utcnow()+timedelta(days=1)).timestamp()*1000)
    market = {"marketId": M, "eventId": E, "status": "OPEN", "marketType": "MONEY",
              "voids": "FMV", "startsTs": starts,
              "fee": {"coefficient": "0.06", "makerCredit": "0.25", "charged": "WHEN_LIVE"},
              "outcomes": [{"outcomeId": A, "name": "Home", "status": "TBD"},
                           {"outcomeId": B, "name": "Away", "status": "TBD"}]}
    event = {"eventId": E, "status": "OPEN_PREGAME", "startsTs": starts,
             "sport": "BASKETBALL", "league": "NBA", "description": "Away @ Home"}
    book = {"marketId": M, "seq": 12,
            "orders": {A: [{"price": "0.45", "qty": 200}],
                       B: [{"price": "0.54", "qty": 200}]}}
    return market, event, book


def test_paper_probe_is_native_only_and_cash_bounded():
    market, instrument, price = candidate_from_catalog(*catalog())
    assert market.market_type == "native_sandbox_moneyline_probe"
    assert market.extensions["sandbox_test_only"] is True
    assert instrument.instrument_id == M and instrument.native_outcome == A
    assert price == Decimal("0.46")
    assert instrument.cost_for(QUANTITY, price) + MAX_FEE < Decimal(1)


@pytest.mark.parametrize("mutate", [
    lambda m,e,b: m.update(status="CLOSED"),
    lambda m,e,b: e.update(status="OPEN_INGAME"),
    lambda m,e,b: m["fee"].update(coefficient="0.10"),
    lambda m,e,b: m.update(voids="UNKNOWN"),
    lambda m,e,b: b.update(marketId=E),
    lambda m,e,b: b["orders"][B][0].update(qty=99),
])
def test_paper_probe_rejects_unreviewed_market_or_missing_depth(mutate):
    market, event, book = deepcopy(catalog())
    mutate(market, event, book)
    with pytest.raises(ValueError):
        candidate_from_catalog(market, event, book)


def test_paper_funding_journal_reuses_one_idempotency_key(tmp_path, monkeypatch):
    monkeypatch.setattr(funding, "JOURNAL", tmp_path / "transfer.json")
    first = funding._write_new_journal(A, B)
    second = funding._write_new_journal(A, B)
    assert first == second and first["environment"] == "sandbox"
    with pytest.raises(ValueError, match="another configuration"):
        funding._write_new_journal(B, A)
