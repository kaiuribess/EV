"""US public catalog and full-book research; all evidence is fixture-only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import httpx
from polymarket_us.errors import RateLimitError

from evplatform.core.config import PricingConfig
from evplatform.core.models import Market, Settlement
from evplatform.core.native_research import compare_minimum_lot
from evplatform.core.native_runtime import NativeRuntime
from evplatform.integrations.polymarket_us_research import (
    catalog_record, current_book, direction_levels, moneyline_matches, parsed_book, taker_fee,
)
from evplatform.venues.polymarket_us import PolymarketUSClient
from evplatform.venues.common import VenueError

NOW = datetime(2026, 10, 22, 18, tzinfo=timezone.utc)
START = NOW + timedelta(days=1)
SLUG = "aec-nba-chi-mil-2026-10-23"


def source_market(**changes):
    values = dict(id="research:chi-mil", event_id="research:chi-mil-event",
                  event_name="Chicago Bulls at Milwaukee Bucks", sport="basketball", league="NBA",
                  participants=("Milwaukee Bucks", "Chicago Bulls"), start_time=START,
                  market_type="moneyline", outcomes=("Chicago Bulls", "Milwaukee Bucks"),
                  settlement=Settlement(overtime="unverified", void_rule="unreviewed_source_rules"),
                  extensions={"research_only": True})
    return Market(**(values | changes))


def native_catalog():
    event = {"slug": "nba-chi-mil-2026-10-23", "title": "Bulls at Bucks",
             "startTime": START.isoformat(), "teams": [
                 {"id": 37, "safeName": "Chicago", "name": "Bulls", "abbreviation": "chi"},
                 {"id": 41, "safeName": "Milwaukee", "name": "Bucks", "abbreviation": "mil"}]}
    market = {"slug": SLUG, "id": 1305786, "gameStartTime": START.isoformat(),
              "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_MONEYLINE",
              "question": "Will the Chicago Bulls beat the Milwaukee Bucks?",
              "description": "The full game includes overtime. Postponements may settle at fair price.",
              "active": True, "closed": False, "hidden": False, "archived": False,
              "status": "MARKET_STATUS_OPEN", "feeCoefficient": "0.0695",
              "minimumTradeQty": "0.01", "orderPriceMinTickSize": "0.01",
              "marketSides": [{"id": "2611050", "long": True, "teamId": 37,
                               "description": "Bulls", "tradable": True},
                              {"id": "2611051", "long": False, "teamId": 41,
                               "description": "Bucks", "tradable": True}]}
    return catalog_record("nba", event, market, observed_at=NOW)


def payload(*, clock=NOW, state="MARKET_STATE_OPEN"):
    return {"marketData": {"marketSlug": SLUG, "state": state,
                           "transactTime": clock.isoformat(),
                           "bids": [{"px": {"value": "0.4900", "currency": "USD"}, "qty": "536371.2900"}],
                           "offers": [{"px": {"value": "0.5000", "currency": "USD"}, "qty": "184776.2600"}]}}


def test_exact_moneyline_identity_creates_unapproved_long_short_leads():
    matches = moneyline_matches(source_market(), [native_catalog()], observed_at=NOW)
    assert {(m.side, m.outcome, m.native_market_id) for m in matches} == {
        ("long", "Chicago Bulls", SLUG), ("short", "Milwaukee Bucks", SLUG)}
    assert all(not m.public()["mapping_reviewed"] and m.price_denomination_usd == "1"
               for m in matches)


@pytest.mark.parametrize("change", [
    lambda row: row["teams"][0].update(name="Chicago Bears"),
    lambda row: row.update(market_start=(START + timedelta(minutes=1)).isoformat()),
    lambda row: row.update(status="MARKET_STATUS_HALTED"),
    lambda row: row["sides"][0].update(tradable=False),
    lambda row: row.update(market_type="SPORTS_MARKET_TYPE_TOTAL"),
])
def test_wrong_identity_state_or_scope_is_not_matched(change):
    row = native_catalog()
    change(row)
    assert moneyline_matches(source_market(), [row], observed_at=NOW) == []


def test_ambiguous_duplicate_market_and_player_prop_do_not_match():
    row = native_catalog()
    assert moneyline_matches(source_market(), [row, deepcopy(row)], observed_at=NOW) == []
    prop = source_market(market_type="player_prop", player="A Player", player_id="A Player",
                         statistic="points", line=D("24.5"), outcomes=("over", "under"))
    assert moneyline_matches(prop, [row], observed_at=NOW) == []


def test_full_rest_book_fractional_depth_long_short_collateral_and_fee():
    row = native_catalog()
    book = parsed_book(row, payload(), received_at=NOW, now=NOW + timedelta(seconds=1),
                       transport="public_sdk_rest_snapshot")
    assert book["current"] and book["offers"] == [("0.5000", "184776.2600")]
    assert direction_levels(book, "long") == [(D("0.5000"), D("184776.2600"))]
    assert direction_levels(book, "short") == [(D("0.5100"), D("536371.2900"))]
    assert taker_fee(row, D("0.5"), D("1")) == D("0.02")
    long, short = moneyline_matches(source_market(), [row], observed_at=NOW)
    yes = compare_minimum_lot(long, D("0.6"), direction_levels(book, "long"),
                              fee_for=lambda p, q: taker_fee(row, p, q),
                              uncertainty_margin_probability=D("0.01"), now=NOW)
    no = compare_minimum_lot(short, D("0.4"), direction_levels(book, "short"),
                             fee_for=lambda p, q: taker_fee(row, p, q),
                             uncertainty_margin_probability=D("0.01"), now=NOW)
    assert yes["cash_cost_usd"] == "0.5000" and yes["net_estimated_profit_usd"] == "0.0700"
    assert no["cash_cost_usd"] == "0.5100" and D(no["net_estimated_profit_usd"]) < 0
    assert D(1) - D("0.49") == D("0.51")  # $1 margin less YES sale proceeds.


@pytest.mark.parametrize("variant", ["stale", "future", "suspended", "empty", "float_qty", "wrong_slug"])
def test_unusable_books_have_no_executable_depth(variant):
    raw = payload()
    if variant == "stale": raw["marketData"]["transactTime"] = (NOW-timedelta(minutes=1)).isoformat()
    if variant == "future": raw["marketData"]["transactTime"] = (NOW+timedelta(minutes=1)).isoformat()
    if variant == "suspended": raw["marketData"]["state"] = "MARKET_STATE_HALTED"
    if variant == "empty": raw["marketData"]["offers"] = []
    if variant == "float_qty": raw["marketData"]["offers"][0]["qty"] = 0.5
    if variant == "wrong_slug": raw["marketData"]["marketSlug"] = "other-market"
    book = parsed_book(native_catalog(), raw, received_at=NOW, now=NOW,
                       transport="public_sdk_rest_snapshot")
    if variant == "empty":
        assert book["current"] and direction_levels(book, "long") == []
    else:
        assert not book["current"] and direction_levels(book, "long") == []


def test_suspended_stream_vetoes_older_open_rest_and_disconnection_invalidates_stream():
    row = native_catalog()
    client = SimpleNamespace(books={SLUG: payload(state="MARKET_STATE_HALTED")["marketData"]},
                             book_received_at={SLUG: NOW.timestamp()},
                             rest_books={SLUG: payload()["marketData"]},
                             rest_book_received_at={SLUG: NOW.timestamp()})
    stopped = current_book(row, client, now=NOW, stream_connected=True)
    assert not stopped["current"] and not direction_levels(stopped, "short")
    # A public REST snapshot can be used as a separate labeled research read.
    public = current_book(row, client, now=NOW, stream_connected=False)
    assert public["current"] and public["source"] == "public_sdk_rest_snapshot"
    client.rest_books.clear()
    client.rest_book_received_at.clear()
    assert not current_book(row, client, now=NOW, stream_connected=False)["current"]


@pytest.mark.asyncio
async def test_public_sdk_market_and_book_calls_are_read_only_and_slug_bound():
    class SDKMarkets:
        calls = []
        async def retrieve_by_slug(self, slug):
            self.calls.append(("retrieve_by_slug", slug))
            return {"market": {"slug": slug}}
        async def book(self, slug):
            self.calls.append(("book", slug))
            return payload()
    sdk = SimpleNamespace(markets=SDKMarkets())
    client = PolymarketUSClient()
    client._public_sdk = sdk
    assert (await client.public_market_metadata(SLUG))["slug"] == SLUG
    assert (await client.fetch_snapshot(SLUG))["marketData"]["marketSlug"] == SLUG
    assert sdk.markets.calls == [("retrieve_by_slug", SLUG), ("book", SLUG)]
    assert client.rest_books[SLUG]["bids"] and not client.authenticated
    client._public_sdk = None
    await client.disconnect()


@pytest.mark.asyncio
async def test_sdk_public_429_stops_without_retry_and_backs_off():
    calls = 0
    async def limited(slug):
        nonlocal calls
        calls += 1
        request = httpx.Request("GET", "https://gateway.polymarket.us/v1/markets/example/book")
        raise RateLimitError("limited", response=httpx.Response(429, request=request))
    client = PolymarketUSClient()
    with pytest.raises(VenueError, match="rate limited"):
        await client._public_read(limited, SLUG)
    assert calls == 1 and client.error_count == 1
    assert client._next_request > __import__("time").monotonic() + 25
    await client.disconnect()


def test_real_shape_public_depth_reaches_existing_read_only_research_pipeline(monkeypatch):
    import evplatform.core.pricing as pricing
    import evplatform.core.native_runtime as native_runtime
    monkeypatch.setattr(pricing, "validate_fair_references", lambda *_: None)
    monkeypatch.setattr(native_runtime, "utcnow", lambda: NOW)
    row = native_catalog()
    match = moneyline_matches(source_market(), [row], observed_at=NOW)
    client = SimpleNamespace(settings=SimpleNamespace(environment="production"),
        authenticated=False, market_slugs=[], books={}, book_received_at={},
        rest_books={SLUG: payload()["marketData"]}, rest_book_received_at={SLUG: NOW.timestamp()},
        health_check=lambda: {"streaming": False, "market_streaming": False})
    monitor = SimpleNamespace(client=client, task=None, context=SimpleNamespace(config={
        "discovery_interval_seconds": 300}), research_catalog_at=NOW,
        research_catalog_rows=[row], research_matches={source_market().id: match})
    runtime = SimpleNamespace(engine=SimpleNamespace(settings=SimpleNamespace(mode="production",
        pricing=PricingConfig(reference_quality_mode="trusted_provider",
                             trusted_research_margin_probability=D("0.01"))),
        registry=SimpleNamespace(records={"polymarket_us": SimpleNamespace(enabled=True, plugin=monitor)})),
        account_snapshots={})
    fair = SimpleNamespace(probabilities={"Chicago Bulls": D("0.6"), "Milwaukee Bucks": D("0.4")},
                           reference_profile_id="nba_main", references=[{"bookmaker": "pinnacle"},
                                                                          {"bookmaker": "fanduel"}])
    results = NativeRuntime.research_comparisons(runtime, source_market(), fair)
    assert len(results) == 2
    assert next(r for r in results if r["side"] == "long")["positive_after_costs"]
    short = next(r for r in results if r["side"] == "short")
    assert short["native_order_direction"] == "SELL_SHORT"
    assert short["margin_locked_usd"] == "1" and short["sale_proceeds_usd"] == "0.4900"
    assert all(not r["execution_eligible"] and not r["mapping_reviewed"] for r in results)
