"""Public MLB listing contract: read-only research matches, never trade mappings."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from evplatform.core.models import Market, Settlement
from evplatform.core.native_research import expected_kalshi_mlb_event, kalshi_mlb_matches
from evplatform.core.native_runtime import NativeRuntime
from evplatform.engine import Engine
from evplatform.plugins.external import KalshiMonitorConfig, NativeMonitor


OBSERVED = datetime(2026, 10, 23, 4, tzinfo=UTC)
START = datetime(2026, 10, 24, 0, tzinfo=UTC)
GAME = "KXMLBGAME-26OCT232000TBMIL"
TOTAL = "KXMLBTOTAL-26OCT232000TBMIL"


def source_market(family="moneyline", **changes):
    values = dict(id="research:parlay:tbmil", event_id="research:parlay:event-tbmil",
                  event_name="Tampa Bay Rays at Milwaukee Brewers", sport="baseball",
                  league="MLB", participants=("Milwaukee Brewers", "Tampa Bay Rays"),
                  start_time=START, market_type=family,
                  outcomes=(("Milwaukee Brewers", "Tampa Bay Rays") if family == "moneyline"
                            else ("over", "under")),
                  line=None if family == "moneyline" else Decimal("6.5"),
                  status="scheduled", settlement=Settlement(period="FULL", scope="event",
                      overtime="unverified", void_rule="unreviewed_source_rules"),
                  extensions={"research_only": True})
    return Market(**(values | changes))


def game_rows():
    secondary = "Postponements may remain open; cancellation after two days resolves to a fair price."
    return [{"ticker": f"{GAME}-{code}", "event_ticker": GAME, "status": "active",
             "market_type": "binary", "exchange_index": 3,
             "rules_primary": f"If {label} wins the Tampa Bay vs Milwaukee professional baseball game originally scheduled for Oct 23, 2026, then the market resolves to Yes.",
             "rules_secondary": secondary}
            for code, label in (("TB", "Tampa Bay"), ("MIL", "Milwaukee"))]


def total_rows():
    return [{"ticker": f"{TOTAL}-{index}", "event_ticker": TOTAL, "status": "active",
             "market_type": "binary", "exchange_index": 3, "floor_strike": line,
             "rules_primary": f"If Tampa Bay and Milwaukee collectively score more {line} runs in the Tampa Bay vs Milwaukee professional baseball game originally scheduled for Oct 23, 2026 at 8:00 PM EDT, then the market resolves to Yes.",
             "rules_secondary": "Official market rule details remain subject to native settlement."}
            for index, line in ((6, "5.5"), (7, "6.5"), (8, "7.5"))]


def test_mlb_game_matches_both_winners_and_both_contract_sides_without_approval():
    market = source_market()
    matches = kalshi_mlb_matches(market, game_rows(), observed_at=OBSERVED)
    assert len(matches) == 4
    assert {(m.native_market_id, m.outcome, m.side) for m in matches} == {
        (f"{GAME}-TB", "Tampa Bay Rays", "yes"),
        (f"{GAME}-TB", "Milwaukee Brewers", "no"),
        (f"{GAME}-MIL", "Milwaukee Brewers", "yes"),
        (f"{GAME}-MIL", "Tampa Bay Rays", "no")}
    assert expected_kalshi_mlb_event(market) == GAME
    assert all(m.exchange_index == 3 and m.native_start == START
               and not m.public()["mapping_reviewed"] and not m.public()["execution_eligible"]
               for m in matches)


def test_mlb_total_matches_only_exact_half_run_threshold():
    market = source_market("total")
    matches = kalshi_mlb_matches(market, total_rows(), observed_at=OBSERVED)
    assert {(m.native_market_id, m.outcome, m.side) for m in matches} == {
        (f"{TOTAL}-7", "over", "yes"), (f"{TOTAL}-7", "under", "no")}
    assert expected_kalshi_mlb_event(market) == TOTAL
    assert kalshi_mlb_matches(source_market("total", line=Decimal("7.5")), total_rows(),
                              observed_at=OBSERVED)[0].native_market_id == f"{TOTAL}-8"
    assert kalshi_mlb_matches(source_market("total", line=Decimal("6.25")), total_rows(),
                              observed_at=OBSERVED) == []


@pytest.mark.parametrize("change", [
    lambda rows: rows.pop(),
    lambda rows: rows[0].update(status="paused"),
    lambda rows: rows[0].update(exchange_index=2),
    lambda rows: rows[0].update(rules_primary=rows[0]["rules_primary"].replace("Tampa Bay wins", "Milwaukee wins")),
    lambda rows: rows.append(dict(rows[0])),
])
def test_mlb_game_incomplete_suspended_conflicted_or_ambiguous_rejects(change):
    rows = game_rows()
    change(rows)
    assert kalshi_mlb_matches(source_market(), rows, observed_at=OBSERVED) == []


def test_mlb_total_rejects_wrong_time_team_period_line_and_expired_source():
    base = source_market("total")
    assert kalshi_mlb_matches(source_market("total", start_time=START + timedelta(hours=1)),
                              total_rows(), observed_at=OBSERVED) == []
    assert kalshi_mlb_matches(source_market("total", participants=("Milwaukee Brewers", "Other")),
                              total_rows(), observed_at=OBSERVED) == []
    assert kalshi_mlb_matches(source_market("total", settlement=Settlement(period="1H")),
                              total_rows(), observed_at=OBSERVED) == []
    assert kalshi_mlb_matches(base, total_rows(), observed_at=START) == []
    rows = total_rows()
    rows[1]["rules_primary"] = rows[1]["rules_primary"].replace("more 6.5", "more 7.5")
    assert kalshi_mlb_matches(base, rows, observed_at=OBSERVED) == []


def _runtime(*, series, rows=(), matches=None):
    monitor = SimpleNamespace(context=SimpleNamespace(config={"discovery_series": series}),
                              research_catalog_rows=rows, research_matches=matches or {})
    record = SimpleNamespace(enabled=True, plugin=monitor)
    engine = SimpleNamespace(registry=SimpleNamespace(records={"kalshi": record}))
    return SimpleNamespace(engine=engine)


def test_research_diagnostics_distinguish_catalog_mapping_depth_expiry_and_net_ev():
    market = source_market("total")
    runtime = _runtime(series=["KXMLBGAME"])
    assert NativeRuntime.research_discovery_status(runtime, market, [])[0] == "not_discovered"
    runtime = _runtime(series=["KXMLBTOTAL"], rows=total_rows())
    assert NativeRuntime.research_discovery_status(runtime, market, [])[0] == "discovered_unmatched"
    runtime = _runtime(series=["KXMLBTOTAL"], matches={market.id: [object()]})
    assert NativeRuntime.research_discovery_status(runtime, market, [])[0] == "matched_no_depth"
    no_depth = [{"rejection": "No synchronized executable opposing bid depth"}]
    assert NativeRuntime.research_discovery_status(runtime, market, no_depth)[0] == "matched_no_depth"
    negative = [{"net_estimated_profit_usd": "-0.02"}]
    positive = [{"net_estimated_profit_usd": "0.01"}]
    assert NativeRuntime.research_discovery_status(runtime, market, negative)[0] == "nonpositive_net_ev"
    assert NativeRuntime.research_discovery_status(runtime, market, positive)[0] == "provisional_positive"


def test_five_series_config_remains_bounded():
    series = ["KXNBAGAME", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME", "KXMLBTOTAL"]
    assert KalshiMonitorConfig(discovery_series=series).discovery_series == series


def test_existing_monitor_selects_exact_mlb_catalog_rows_for_native_subscription():
    game, total = source_market(), source_market("total", id="research:parlay:tbmil-total")
    monitor = SimpleNamespace(manifest=SimpleNamespace(name="kalshi"),
        research_catalog_at=OBSERVED, research_catalog_rows=[*game_rows(), *total_rows()],
        engine=SimpleNamespace(reference_estimates={game.id: {}, total.id: {}},
                               markets={game.id: game, total.id: total}),
        research_events={}, research_fee_metadata={GAME: {}, TOTAL: {}},
        research_matches={}, _last_research_source_ids=frozenset())
    priority = asyncio.run(NativeMonitor._refresh_research_matches(monitor))
    assert set(monitor.research_matches) == {game.id, total.id}
    assert set(priority) == {f"{GAME}-TB", f"{GAME}-MIL", f"{TOTAL}-7"}
    assert len(priority) == 3  # Both YES/NO directions share the same monitored ticker.


def test_monitor_health_exposes_exact_catalog_and_subscription_scope_read_only():
    market = source_market("total")
    match = kalshi_mlb_matches(market, total_rows(), observed_at=OBSERVED)[0]
    monitor = SimpleNamespace(config_error=None,
        client=SimpleNamespace(health_check=lambda: {"state": "streaming", "streaming": True}),
        manifest=SimpleNamespace(name="kalshi"), task=None, frames=1,
        discovery_status="current", discovered_event_ids=[],
        discovered_market_ids=[f"{TOTAL}-7"],
        _subscribed_market_ids=[f"{TOTAL}-7"],
        research_matches={market.id: [match]})
    health = asyncio.run(NativeMonitor.health_check(monitor))
    assert health["discovered_market_ids"] == [f"{TOTAL}-7"]
    assert health["subscribed_market_ids"] == [f"{TOTAL}-7"]
    assert health["research_matched_market_ids"] == [f"{TOTAL}-7"]
    assert health["streaming"] is True


def test_cached_research_offer_cannot_remain_a_live_positive_without_current_depth():
    market = source_market()
    old = {"venue": "kalshi", "native_market_id": f"{GAME}-TB", "positive_after_costs": True,
           "net_estimated_profit_usd": "0.03"}
    row = {"market_id": market.id, "quality_mode": "strict_verified", "status": "PROVISIONAL POSITIVE EV",
           "native_research_estimates": [old], "native_market_matched": True}
    engine = SimpleNamespace(reference_estimates={market.id: row}, markets={market.id: market},
        reference_fairs={market.id: None},
        native=SimpleNamespace(research_discovery_status=lambda _market, offers:
            ("matched_no_depth", "Current depth missing")))
    displayed = Engine.public_reference_estimates(engine)[0]
    assert displayed["native_research_estimates"] == []
    assert displayed["native_market_matched"] is False
    assert displayed["status"] == "REFERENCE ESTIMATE"


def test_expired_reference_archives_original_quote_and_native_clocks():
    old = {"market_id": "research:parlay:tbmil", "native_research_estimates": [
        {"venue": "kalshi", "native_market_id": f"{GAME}-TB",
         "calculated_at": "2026-10-22T20:30:00Z", "execution_eligible": False}],
        "references": [{"bookmaker": "pinnacle",
                        "source_observed_at": "2026-10-22T20:29:59Z"}]}
    engine = SimpleNamespace(_last_current_research={old["market_id"]: old}, research_history=[])
    Engine._archive_research(engine, old["market_id"], "Pinnacle observation expired")
    archived = engine.research_history[0]
    assert archived["native_research_stage"] == "references_expired"
    assert archived["historical"] is True and archived["execution_eligible"] is False
    assert archived["references"][0]["source_observed_at"] == "2026-10-22T20:29:59Z"
    assert archived["native_research_estimates"][0]["calculated_at"] == "2026-10-22T20:30:00Z"
