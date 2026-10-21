"""Full retail depth fixtures, not proof of live execution connectivity."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from evplatform.core.models import utcnow
from evplatform.core.native_accounting import NativeInstrument, NativePriceBand
from evplatform.core.native_pricing import NativeFeeBound, instrument_fingerprint
from evplatform.venues.book_normalization import NativeBookMapper, NativeMarketMapping


def mappings(market):
    result = []
    for outcome, native in zip(market.outcomes, ("yes", "no")):
        instrument = NativeInstrument(venue="polymarket_us", instrument_id="sample-nba", canonical_market_id=market.id,
            outcome=outcome, native_outcome=native, quantity_unit="contracts", price_format="probability", payout_per_unit_usd="1",
            quantity_step="0.01", minimum_quantity="0.01", cash_quantum_usd="0.00001",
            price_bands=(NativePriceBand(minimum="0.001", maximum="0.999", step="0.001"),),
            settlement_rules_fingerprint=market.identity(), verified=True, source="offline reviewed mapping")
        fee = NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument), per_unit_usd="0.03", per_fill_usd="0", fixed_order_usd="0", verified=True,
            source="offline reviewed fee cap", observed_at=utcnow()-timedelta(hours=1), valid_until=utcnow()+timedelta(hours=1))
        result.append(NativeMarketMapping(market=market, instrument=instrument, fee_bound=fee))
    return result


def frame(clock=None):
    return {"type":"polymarket_us_monitor", "channel":"markets", "kind":"data", "payload":{
        "marketData":{"marketSlug":"sample-nba", "state":"MARKET_STATE_OPEN",
            "bids":[{"px":{"value":"0.555","currency":"USD"},"qty":"0.50"},
                    {"px":{"value":"0.550","currency":"USD"},"qty":"2.50"}],
            "offers":[{"px":{"value":"0.560","currency":"USD"},"qty":"0.80"}],
            "transactTime":(clock or utcnow()-timedelta(seconds=1)).isoformat()}}}


def apply(client, raw):
    client.books["sample-nba"] = raw["payload"]["marketData"]


def client_for(raw):
    client = SimpleNamespace(_ready={"markets":True},market_slugs=["sample-nba"],books={})
    apply(client,raw)
    return client


def test_full_book_yes_offers_no_complement_bids_preserves_native_quantity_and_clock(market):
    mapper = NativeBookMapper(mappings(market))
    raw=frame()
    books=[book for _,book in mapper.process("polymarket_us",client_for(raw),raw)]
    assert len(books)==2
    assert books[0].asks[0].price==D("0.560") and books[0].asks[0].quantity==D("0.80")
    assert [(level.price,level.quantity) for level in books[1].asks]==[(D("0.445"),D("0.50")),(D("0.450"),D("2.50"))]
    assert books[0].source_time < books[0].received_at and books[0].provider_time is None
    assert not books[0].synthetic and books[0].synchronized


@pytest.mark.parametrize("state,status",[("MARKET_STATE_SUSPENDED","suspended"),("MARKET_STATE_HALTED","suspended"),
    ("MARKET_STATE_MATCH_AND_CLOSE_AUCTION","suspended"),("MARKET_STATE_EXPIRED","closed"),("MARKET_STATE_TERMINATED","closed")])
def test_noncontinuous_open_states_remove_executable_liquidity(market,state,status):
    mapper=NativeBookMapper(mappings(market))
    raw=frame(); client=client_for(raw)
    first=mapper.process("polymarket_us",client,raw)[0][1]
    raw["payload"]["marketData"]["state"]=state
    stopped=mapper.process("polymarket_us",client,raw)[0][1]
    assert stopped.status==status and not stopped.asks and stopped.revision>first.revision


@pytest.mark.parametrize("change",["missing_clock","future_clock","regressed_clock","float","duplicate","crossed","not_ready","not_applied","unknown_state","currency"])
def test_malformed_snapshot_invalidates_previously_executable_book(market,change):
    mapper=NativeBookMapper(mappings(market))
    raw=frame(); client=client_for(raw)
    mapper.process("polymarket_us",client,raw)
    raw=deepcopy(raw); data=raw["payload"]["marketData"]
    if change=="missing_clock": data.pop("transactTime")
    elif change=="future_clock": data["transactTime"]=(utcnow()+timedelta(days=1)).isoformat()
    elif change=="regressed_clock": data["transactTime"]=(utcnow()-timedelta(days=1)).isoformat()
    elif change=="float": data["bids"][0]["qty"]=0.29
    elif change=="duplicate": data["bids"].append(data["bids"][0])
    elif change=="crossed": data["offers"][0]["px"]["value"]="0.5"
    elif change=="unknown_state": data["state"]="unexpected"
    elif change=="currency": data["bids"][0]["px"]["currency"]="EUR"
    elif change=="not_ready": client._ready["markets"]=False
    elif change=="not_applied": client.books={}
    if change!="not_applied": apply(client,raw)
    invalid=mapper.process("polymarket_us",client,raw)
    assert len(invalid)==2 and all(not book.synchronized and not book.asks for _,book in invalid)


def test_heartbeat_lite_trade_and_private_frames_do_not_refresh_book(market):
    mapper=NativeBookMapper(mappings(market)); raw=frame(); client=client_for(raw)
    first=mapper.process("polymarket_us",client,raw)[0][1]
    for kind in ("heartbeat","unknown"):
        assert mapper.process("polymarket_us",client,{**raw,"kind":kind})==[]
    assert mapper.process("polymarket_us",client,{**raw,"channel":"private"})==[]
    assert mapper.process("polymarket_us",client,{**raw,"payload":{"marketDataLite":{"marketSlug":"sample-nba"}}})==[]
    assert next(iter(mapper._last.values()))[1].revision==first.revision
    invalid=mapper.process("polymarket_us",client,{"type":"transport_invalidated"})[0][1]
    assert not invalid.synchronized
    recovered=mapper.process("polymarket_us",client,raw)[0][1]
    assert recovered.revision>invalid.revision
