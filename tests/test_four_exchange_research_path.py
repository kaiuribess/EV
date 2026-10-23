"""The fourth venue uses the same fair-price research path, never order APIs."""
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from evplatform.core.config import PricingConfig
from evplatform.core.native_research import compare_prophetx_stake_lot, prophetx_matches
from evplatform.core.native_runtime import NativeRuntime
from evplatform.core.models import Market, Settlement, utcnow
from evplatform.core.plugins import PluginRecord, PluginRegistry
from evplatform.plugins.external import NativeMonitor
from evplatform.venues.prophetx_market_data import ProphetXMarketData


def source_market(now, **changes):
    values = dict(id="research:atl-cha", event_id="source-event", event_name="Atlanta Hawks at Charlotte Hornets",
        sport="basketball", league="NBA", participants=("Charlotte Hornets", "Atlanta Hawks"),
        start_time=now+timedelta(hours=2), market_type="moneyline",
        outcomes=("Atlanta Hawks", "Charlotte Hornets"), settlement=Settlement(),
        extensions={"research_only": True})
    return Market(**(values | changes))


def native_catalog():
    return {"data": {"123": [{"id": 219, "name": "Moneyline", "type": "moneyline",
        "sub_type": "moneyline", "period": "full_game", "status": "active",
        "selections": [[{"strike_id": "atl-strike", "outcome_id": 4, "name": "Atlanta Hawks", "status": "active"}],
                       [{"strike_id": "cha-strike", "outcome_id": 5, "name": "Charlotte Hornets", "status": "active"}]]}]}}


def native_frame():
    return {"event": "v2.market_selections", "scope": {"event_id": 123},
        "data": {"sport_event_id": 123, "market_id": 219, "info": {"id": 219, "status": "active",
        "selections": [[{"strike_id": "atl-strike", "outcome_id": 4, "price": -110,
                         "value": "10.00", "quantity": "9999", "updated_at": 1792731906663622000}],
                       [{"strike_id": "cha-strike", "outcome_id": 5, "price": 120,
                         "value": "5.00", "quantity": "9999", "updated_at": 1792731906663622000}]]}}}


def setup(now):
    market = source_market(now)
    data = ProphetXMarketData()
    data.replace_catalog([native_catalog()], ["123"], observed_at=now)
    events = {"123": {"event_id": 123, "name": market.event_name,
                       "scheduled": market.start_time.isoformat(),
                       "status": "not_started", "tournament_id": 132}}
    data.apply(native_frame(), received_at=now)
    return market, data, events


def test_prophetx_exact_strikes_and_usd_stake_fee_economics():
    now = utcnow()
    market, data, events = setup(now)
    matches = prophetx_matches(market, data.catalog, events, observed_at=now)
    assert {(m.outcome, m.native_strike_id, m.native_outcome_id) for m in matches} == {
        ("Atlanta Hawks", "atl-strike", "4"), ("Charlotte Hornets", "cha-strike", "5")}
    view = data.snapshot(now=now, connected=True)
    atl = next(row for row in view["levels"] if row["strike_id"] == "atl-strike")
    assert atl["executable_stake_usd"] == "10.00"  # not legacy quantity=9999
    estimate = compare_prophetx_stake_lot(next(m for m in matches if m.outcome == "Atlanta Hawks"),
        D("0.58"), [(D(atl["american_price"]), D(atl["executable_stake_usd"]))],
        fee_rate_on_winnings=D("0.02"), uncertainty_margin_probability=D("0.01"), now=now)
    payout = D(1) + D(100)/D(110)
    expected_fee = D("0.58")*(payout-D(1))*D("0.02")
    assert D(estimate["normal_payout_usd"]) == payout
    assert abs(D(estimate["taker_fee_usd"])-expected_fee) < D("1e-25")
    assert abs(D(estimate["net_estimated_profit_usd"]) - (
        D("0.58")*payout-D(1)-expected_fee-D("0.01")*(payout-(payout-D(1))*D("0.02")))) < D("1e-25")
    assert estimate["positive_after_costs"] and estimate["execution_eligible"] is False


def test_prophetx_ambiguous_clock_period_and_prop_fields_do_not_match():
    now = utcnow()
    market, data, events = setup(now)
    events["123"]["scheduled"] = (market.start_time-timedelta(minutes=5)).isoformat()
    assert prophetx_matches(market, data.catalog, events, observed_at=now) == []
    events["123"]["scheduled"] = market.start_time.isoformat()
    data.catalog[("123", "219")]["period"] = "first_half"
    assert prophetx_matches(market, data.catalog, events, observed_at=now) == []
    prop = source_market(now, market_type="player_prop", player="Trae Young", player_id="trae-1",
        statistic="points", line=D("24.5"), outcomes=("over", "under"))
    data.catalog[("123", "219")].update({"type": "player_prop", "sub_type": "player_total_points",
        "period": "full_game", "player_id": "trae-1", "statistic": "points", "line": "25.5",
        "strikes": {"over-strike": {"name": "Over", "outcome_id": "4", "status": "active"},
                    "under-strike": {"name": "Under", "outcome_id": "5", "status": "active"}}})
    assert prophetx_matches(prop, data.catalog, events, observed_at=now) == []
    data.catalog[("123", "219")]["line"] = "24.5"
    assert len(prophetx_matches(prop, data.catalog, events, observed_at=now)) == 2


def test_prophetx_enters_shared_read_only_runtime_and_stale_depth_rejects(monkeypatch):
    import evplatform.core.pricing as pricing
    monkeypatch.setattr(pricing, "validate_fair_references", lambda *_: None)
    now = utcnow()
    market, data, events = setup(now)
    matches = prophetx_matches(market, data.catalog, events, observed_at=now)
    client = SimpleNamespace(settings=SimpleNamespace(environment="production"),
        public_market_data=data, health_check=lambda: {"streaming": True})
    monitor = SimpleNamespace(client=client, task=SimpleNamespace(done=lambda: False),
        context=SimpleNamespace(config={"discovery_interval_seconds": 300}),
        research_catalog_at=now, research_matches={market.id: matches})
    engine = SimpleNamespace(settings=SimpleNamespace(mode="production",
        pricing=PricingConfig(trusted_research_margin_probability=D("0.01"))),
        registry=SimpleNamespace(records={"prophetx": SimpleNamespace(enabled=True, plugin=monitor)}))
    runtime = SimpleNamespace(engine=engine, account_snapshots={})
    fair = SimpleNamespace(probabilities={"Atlanta Hawks": D("0.58"), "Charlotte Hornets": D("0.42")},
        reference_profile_id="nba", references=[{"bookmaker": "pinnacle"}, {"bookmaker": "fanduel"}])
    results = NativeRuntime.research_comparisons(runtime, market, fair)
    assert len(results) == 2 and {row["venue"] for row in results} == {"prophetx"}
    assert all(row["quality"] == "provisional_native_research" and not row["execution_eligible"] for row in results)
    assert next(row for row in results if row["native_strike_id"] == "atl-strike")["cash_cost_usd"] == "1"
    data.invalidate()
    stale = NativeRuntime.research_comparisons(runtime, market, fair)
    assert all(row["positive_after_costs"] is False and "No current" in row["rejection"] for row in stale)


def test_live_health_bypasses_frozen_cached_record_and_heartbeat_is_not_book():
    now = utcnow()
    market, data, events = setup(now)
    monitor = object.__new__(NativeMonitor)
    monitor.manifest = SimpleNamespace(name="prophetx", model_dump=lambda **_: {"name": "prophetx"})
    monitor.client = SimpleNamespace(public_market_data=data,
        health_check=lambda: {"state": "streaming", "streaming": True, "reason": ""})
    monitor.task = SimpleNamespace(done=lambda: False)
    monitor.config_error = ""
    monitor.frames = 0
    monitor.discovery_status = "current"
    monitor.discovery_error_type = ""
    monitor.discovered_event_ids = ["123"]
    monitor.discovered_market_ids = []
    monitor.discovered_prop_subtypes = {}
    monitor._subscribed_event_ids = ["123"]
    monitor._subscribed_prop_subtypes = {}
    monitor._book_at = {}
    monitor._market_data_updates = 0
    monitor._last_market_data_at = None
    monitor._last_valid_book_at = None
    monitor._reconnect_count = 0
    monitor.research_catalog_rows = []
    monitor.research_matches = {}
    monitor.credentials = {}
    monitor._observe_market_frame({"event": "pusher:ping"})
    assert monitor.health_snapshot()["market_data_updates"] == 0
    monitor._observe_market_frame(native_frame())
    live = monitor.health_snapshot()
    assert live["market_data_updates"] == 1 and live["valid_book_count"] == 1
    registry = PluginRegistry()
    registry.records["prophetx"] = PluginRecord(monitor, enabled=True,
        health={"status": "streaming", "market_data_updates": 0})
    assert registry.public()[0]["health"]["market_data_updates"] == 1
    data.invalidate()
    assert registry.public()[0]["health"]["valid_book_count"] == 0
    assert registry.public()[0]["health"]["data_stale"] is True


@pytest.mark.parametrize("venue,heartbeat,book,subject", [
    ("kalshi", {"type": "heartbeat"},
     {"type": "orderbook_delta", "msg": {"market_ticker": "GAME-ATL"}}, "GAME-ATL"),
    ("novig", {"heartbeat": {"orders": 4}},
     {"delta": {"MARKET-1": {"book": {"seq": 2, "deltas": []}}}}, "MARKET-1"),
    ("prophetx", {"event": "pusher:ping"}, native_frame(), "123:219"),
    ("polymarket_us", {"type": "polymarket_us_monitor", "channel": "markets", "kind": "heartbeat"},
     {"type": "polymarket_us_monitor", "channel": "markets", "kind": "data",
      "payload": {"marketData": {"marketSlug": "game-atl"}}}, "game-atl"),
])
def test_all_four_health_counters_count_books_not_heartbeats(venue, heartbeat, book, subject):
    monitor = SimpleNamespace(manifest=SimpleNamespace(name=venue), _book_at={},
        _market_data_updates=0, _last_market_data_at=None, _last_valid_book_at=None,
        health_snapshot=lambda: {"valid_book_ids": [subject]})
    NativeMonitor._observe_market_frame(monitor, heartbeat)
    assert monitor._market_data_updates == 0 and monitor._last_market_data_at is None
    NativeMonitor._observe_market_frame(monitor, book)
    assert monitor._market_data_updates == 1 and subject in monitor._book_at
    assert monitor._last_market_data_at == monitor._last_valid_book_at


def test_schedule_conflict_diagnostic_never_creates_identity_match():
    now = utcnow()
    market, data, events = setup(now)
    events["123"]["scheduled"] = (market.start_time-timedelta(minutes=10)).isoformat()
    assert prophetx_matches(market, data.catalog, events, observed_at=now) == []
    monitor = SimpleNamespace(research_events=events, research_catalog_rows=[])
    runtime = SimpleNamespace(engine=SimpleNamespace(registry=SimpleNamespace(
        records={"prophetx": SimpleNamespace(enabled=True, plugin=monitor)})))
    reasons = NativeRuntime.research_identity_rejections(runtime, market)
    assert reasons == [{"venue": "prophetx", "reason":
        "ProphetX catalog start differs from sportsbook by 600 seconds; event identity needs review"}]
