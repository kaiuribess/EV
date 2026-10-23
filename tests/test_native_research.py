"""Read-only catalog-to-reference comparison; no order transport is involved."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from evplatform.core.models import Market, Settlement, utcnow
from evplatform.core.config import PricingConfig
from evplatform.core.native_runtime import NativeRuntime
from evplatform.core.native_research import (
    compare_minimum_lot, kalshi_moneyline_matches, kalshi_research_taker_fee,
    novig_moneyline_matches, novig_prop_matches, novig_taker_fee,
)
from evplatform.venues.kalshi import KalshiBook
from evplatform.venues.novig import NovigBook

NOW = datetime(2026, 10, 22, 18, tzinfo=UTC)
START = datetime(2026, 10, 23, 23, tzinfo=UTC)
EVENT = "KXNBAGAME-26OCT23ATLCHA"
M = "11111111-1111-4111-8111-111111111111"
E = "22222222-2222-4222-8222-222222222222"
A = "33333333-3333-4333-8333-333333333333"
B = "44444444-4444-4444-8444-444444444444"


def market(**changes):
    values = dict(id="research:atl-cha", event_id="research:parlay:atl-cha", event_name="Atlanta Hawks at Charlotte Hornets",
                  sport="basketball", league="NBA", participants=("Charlotte Hornets", "Atlanta Hawks"),
                  start_time=START, market_type="moneyline", outcomes=("Atlanta Hawks", "Charlotte Hornets"),
                  settlement=Settlement(overtime="unverified", void_rule="unreviewed_source_rules"),
                  extensions={"research_only": True})
    return Market(**(values | changes))


def kalshi_rows():
    return [{"ticker": f"{EVENT}-{code}", "event_ticker": EVENT, "market_type": "binary",
             "status": "active", "exchange_index": 3, "occurrence_datetime": "2026-10-24T02:00:00Z",
             "rules_primary": f"If {team} wins the Atlanta vs Charlotte Pro Basketball game originally scheduled for Oct 23, 2026, then the market resolves to Yes.",
             "rules_secondary": "If postponed, market may resolve at a fair price."}
            for code, team in (("ATL", "Atlanta"), ("CHA", "Charlotte"))]


def novig_rows():
    start = int(START.timestamp()*1000)
    return [{"marketId": M, "eventId": E, "marketType": "MONEY", "status": "OPEN", "voids": "FMV",
             "strike": "0", "startsTs": start, "fee": {"coefficient": "0.06", "charged": "WHEN_LIVE"},
             "outcomes": [{"outcomeId": A, "name": "ATL", "status": "TBD"},
                          {"outcomeId": B, "name": "CHA", "status": "TBD"}]}]


def novig_event():
    return {"eventId": E, "league": "NBA", "sport": "BASKETBALL", "status": "OPEN_PREGAME",
            "startsTs": int(START.timestamp()*1000),
            "description": "Atlanta Hawks @ Charlotte Hornets"}


def prop_market(**changes):
    values = dict(id="research:atl-cha-trae-points", market_type="player_prop",
        player="Trae Young", player_id="Trae Young", statistic="points", line=D("24.5"),
        outcomes=("over", "under"),
        settlement=Settlement(period="FULL", overtime="unverified", void_rule="unreviewed_source_rules"))
    return market(**(values | changes))


def novig_prop_rows():
    return [{"marketId": M, "eventId": E, "marketType": "POINTS", "status": "OPEN",
             "voids": "FMV", "strike": "24.5", "startsTs": int(START.timestamp()*1000),
             "description": "Trae Young 24.5 POINTS",
             "fee": {"coefficient": "0.06", "charged": "WHEN_LIVE"},
             "outcomes": [{"outcomeId": A, "name": "Over 24.5", "status": "TBD"},
                          {"outcomeId": B, "name": "Under 24.5", "status": "TBD"}]}]


def test_kalshi_exact_two_winners_create_four_unapproved_directions():
    matches = kalshi_moneyline_matches(market(), kalshi_rows(), observed_at=NOW)
    assert len(matches) == 4
    assert {(m.outcome, m.side) for m in matches} == {
        ("Atlanta Hawks", "yes"), ("Atlanta Hawks", "no"),
        ("Charlotte Hornets", "yes"), ("Charlotte Hornets", "no")}
    assert all(not m.public()["mapping_reviewed"] and m.exchange_index == 3 for m in matches)
    assert all(m.source_start == START and m.native_start != START for m in matches)


@pytest.mark.parametrize("change", [
    lambda rows: rows.pop(),
    lambda rows: rows[0].update(status="paused"),
    lambda rows: rows[0].update(rules_primary=rows[0]["rules_primary"].replace("Atlanta wins", "Denver wins")),
    lambda rows: rows.append(dict(rows[0])),
])
def test_kalshi_incomplete_suspended_wrong_or_ambiguous_pair_rejects(change):
    rows = kalshi_rows()
    change(rows)
    assert kalshi_moneyline_matches(market(), rows, observed_at=NOW) == []


def test_novig_exact_event_time_and_two_outcomes_match_without_approval():
    found = novig_moneyline_matches(market(), novig_rows(), {E: novig_event()}, observed_at=NOW)
    assert len(found) == 2
    assert {m.native_outcome_id for m in found} == {A, B}
    assert {m.opposing_outcome_id for m in found} == {A, B}
    assert all(not m.public()["execution_eligible"] for m in found)


def test_novig_prop_requires_exact_player_stat_threshold_period_and_pair():
    found = novig_prop_matches(prop_market(), novig_prop_rows(), {E: novig_event()}, observed_at=NOW)
    assert len(found) == 2 and {item.outcome for item in found} == {"over", "under"}
    assert all(item.price_denomination_usd == "0.01" and not item.public()["normal_condition_reviewed"]
               for item in found)
    assert novig_prop_matches(prop_market(player="Other Player", player_id="Other Player"),
                              novig_prop_rows(), {E: novig_event()}, observed_at=NOW) == []
    assert novig_prop_matches(prop_market(statistic="rebounds"), novig_prop_rows(),
                              {E: novig_event()}, observed_at=NOW) == []
    assert novig_prop_matches(prop_market(line=D("25.5")), novig_prop_rows(),
                              {E: novig_event()}, observed_at=NOW) == []
    assert novig_prop_matches(prop_market(settlement=Settlement(period="1H")),
                              novig_prop_rows(), {E: novig_event()}, observed_at=NOW) == []
    rows = novig_prop_rows()
    rows[0]["outcomes"].pop()
    assert novig_prop_matches(prop_market(), rows, {E: novig_event()}, observed_at=NOW) == []


@pytest.mark.parametrize("mutation", [
    lambda rows, events: rows[0]["outcomes"][0].update(name="BOS"),
    lambda rows, events: rows[0]["outcomes"][1].update(status="SUSPENDED"),
    lambda rows, events: rows[0].update(startsTs=rows[0]["startsTs"]+300000),
    lambda rows, events: events[E].update(description="Atlanta Hawks @ Boston Celtics"),
    lambda rows, events: rows[0].update(marketType="TOTAL"),
])
def test_novig_wrong_identity_status_time_or_market_rejects(mutation):
    rows, events = novig_rows(), {E: novig_event()}
    mutation(rows, events)
    assert novig_moneyline_matches(market(), rows, events, observed_at=NOW) == []


def fee_evidence():
    return ({"series": {"ticker": "KXNBAGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": D(1)}},
            {"event": {"event_ticker": EVENT, "series_ticker": "KXNBAGAME", "exchange_index": 3,
                       "fee_type_override": None, "fee_multiplier_override": None}},
            {"series_fee_change_arr": []}, {"event_fee_changes": [], "cursor": ""})


def test_historical_atl_5734_versus_56_cent_yes_loses_after_real_fee():
    match = next(m for m in kalshi_moneyline_matches(market(), kalshi_rows(), observed_at=NOW)
                 if m.outcome == "Atlanta Hawks" and m.side == "yes")
    evidence = fee_evidence()
    result = compare_minimum_lot(match, D("0.5734"), [(D("0.56"), D(100))],
        fee_for=lambda p, q: kalshi_research_taker_fee(match, *evidence, p, q, now=NOW),
        uncertainty_margin_probability=D("0.01"), now=NOW)
    assert D(result["gross_estimated_profit_usd"]) == D("0.0134")
    assert D(result["taker_fee_usd"]) == D("0.02")
    assert D(result["fee_adjusted_estimated_profit_usd"]) < 0
    assert D(result["net_estimated_profit_usd"]) < 0
    assert result["positive_after_costs"] is False


def test_bid_complement_produces_yes_and_no_executable_prices():
    book = KalshiBook()
    book.apply({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
        "market_ticker": f"{EVENT}-ATL", "yes_dollars_fp": [["0.55", "5.00"]],
        # This existing socket requests unified YES-leg prices: a NO bid at
        # .44 is transported as YES-leg .56, unlike the REST bid convention.
        "no_dollars_fp": [["0.56", "7.00"]]}})
    assert book.asks(f"{EVENT}-ATL", "yes") == [(D("0.56"), D("7.00"))]
    assert book.asks(f"{EVENT}-ATL", "no") == [(D("0.45"), D("5.00"))]


def test_novig_contract_is_one_cent_and_pregame_fee_metadata_is_zero():
    match = novig_moneyline_matches(market(), novig_rows(), {E: novig_event()}, observed_at=NOW)[0]
    result = compare_minimum_lot(match, D("0.60"), [(D("0.56"), D(500))],
        fee_for=lambda p, q: novig_taker_fee(novig_rows()[0], novig_event(), p, q),
        uncertainty_margin_probability=D("0.01"), now=NOW)
    assert result["comparison_quantity"] == "100"
    assert result["normal_payout_usd"] == "1.00"
    assert D(result["cash_cost_usd"]) == D("0.5600")
    assert result["taker_fee_usd"] == "0"
    assert D(result["net_estimated_profit_usd"]) == D("0.03")


def test_novig_stream_snapshot_and_delta_feed_existing_read_only_research_path():
    """A catalog match consumes opposite-side L3 depth in Novig's cent units."""
    row = novig_rows()[0]
    match = next(item for item in novig_moneyline_matches(
        market(), [row], {E: novig_event()}, observed_at=NOW)
        if item.outcome == "Atlanta Hawks")
    book = NovigBook()
    snapshot = {"ts": int(NOW.timestamp() * 1000), "snapshot": {M: {
        "lifecycle": {"seq": 1, "status": "OPEN"},
        "book": {"seq": 8, "orders": {B: [
            {"orderId": "opposite-bid", "price": "0.440", "qty": 500}]}}}}}
    book.apply(snapshot)
    levels = book.asks(M, match.opposing_outcome_id)
    assert levels == [(D("0.560"), 500)]
    result = compare_minimum_lot(match, D("0.60"), levels,
        fee_for=lambda p, q: novig_taker_fee(row, novig_event(), p, q),
        uncertainty_margin_probability=D("0.01"), now=NOW)
    assert result["normal_payout_usd"] == "1.00"
    assert result["comparison_quantity"] == "100"
    assert result["positive_after_costs"] is True
    book.apply({"ts": int(NOW.timestamp() * 1000) + 1000,
        "delta": {M: {"book": {"seq": 9, "deltas": [
            {"kind": "add", "orderId": "better-bid", "outcomeId": B,
             "price": "0.450", "qty": 100}]}}}})
    assert book.asks(M, B)[0] == (D("0.550"), 100)
    book.reset()
    assert book.asks(M, B) == []
    unavailable = compare_minimum_lot(match, D("0.60"), book.asks(M, B),
        fee_for=lambda p, q: D(0), uncertainty_margin_probability=D("0.01"), now=NOW)
    assert unavailable["positive_after_costs"] is False


def test_missing_uncertainty_or_depth_does_not_claim_positive_net_ev():
    match = kalshi_moneyline_matches(market(), kalshi_rows(), observed_at=NOW)[0]
    unknown = compare_minimum_lot(match, D("0.70"), [(D("0.50"), D(3))],
        fee_for=lambda p, q: D("0.02"), uncertainty_margin_probability=None, now=NOW)
    empty = compare_minimum_lot(match, D("0.70"), [], fee_for=lambda p, q: D(0),
        uncertainty_margin_probability=D("0.01"), now=NOW)
    assert unknown["positive_after_costs"] is False and unknown["net_estimated_roi"] is None
    assert "rejection" in empty and "net_estimated_roi" not in empty


def test_comparison_walks_multiple_executable_levels_and_charges_each_fill():
    match = kalshi_moneyline_matches(market(), kalshi_rows(), observed_at=NOW)[0]
    result = compare_minimum_lot(match, D("0.70"),
        [(D("0.50"), D("0.4")), (D("0.55"), D("0.6"))],
        fee_for=lambda price, quantity: D("0.01"),
        uncertainty_margin_probability=D("0.02"), now=NOW)
    assert result["price"] == "0.530"
    assert result["limit_price"] == "0.55"
    assert result["available_at_best_price"] == "0.4"
    assert result["taker_fee_usd"] == "0.02"
    assert D(result["net_estimated_profit_usd"]) == D("0.13")


def test_research_margin_is_separate_from_owner_pilot_authorization():
    config = PricingConfig(trusted_research_margin_probability=D("0.02"))
    assert config.trusted_research_margin_probability == D("0.02")
    assert config.trusted_pilot_policy.enabled is False
    assert config.trusted_pilot_policy.owner_approved is False


def test_exact_market_research_allowance_does_not_approve_pilot():
    config = PricingConfig(trusted_research_market_margins={market().id: D("0.01")})
    assert config.trusted_research_market_margins[market().id] == D("0.01")
    assert config.trusted_pilot_policy.enabled is False
    with pytest.raises(ValueError, match="exact research market"):
        PricingConfig(trusted_research_market_margins={"basketball:moneyline": D("0.01")})


def test_historical_jeudy_pregame_novig_economics_at_one_dollar_payout():
    match = next(item for item in novig_prop_matches(prop_market(), novig_prop_rows(),
        {E: novig_event()}, observed_at=NOW) if item.outcome == "over")
    fair = D("0.600847487482864325318460014")
    result = compare_minimum_lot(match, fair, [(D("0.58"), D("6027"))],
        fee_for=lambda p, q: novig_taker_fee(novig_prop_rows()[0], novig_event(), p, q),
        uncertainty_margin_probability=None, now=NOW)
    assert result["comparison_quantity"] == "100"
    assert result["normal_payout_usd"] == "1.00"
    assert D(result["cash_cost_usd"]) == D("0.58")
    assert D(result["taker_fee_usd"]) == 0
    assert D(result["fee_adjusted_estimated_profit_usd"]) == fair-D("0.58")
    assert D(result["fee_adjusted_estimated_profit_usd"])/D("100") == (fair-D("0.58"))/D("100")
    assert result["net_estimated_profit_usd"] is None
    with_allowance = compare_minimum_lot(match, fair, [(D("0.58"), D("6027"))],
        fee_for=lambda p, q: novig_taker_fee(novig_prop_rows()[0], novig_event(), p, q),
        uncertainty_margin_probability=D("0.03"), now=NOW)
    assert D(with_allowance["net_estimated_profit_usd"]) < 0


def test_expired_research_archive_keeps_original_prices_but_cannot_execute():
    from collections import deque
    from evplatform.engine import Engine

    engine = object.__new__(Engine)
    engine.research_history = deque(maxlen=100)
    engine._last_current_research = {market().id: {"market_id": market().id,
        "calculated_at": NOW.isoformat(), "native_market_matched": True,
        "native_research_estimates": [{"price": "0.58", "calculated_at": NOW.isoformat(),
                                       "positive_after_costs": True}]}}
    engine._archive_research(market().id, "Verified sportsbook observation indicator expired")
    archived = engine.research_history[0]
    assert archived["calculated_at"] == NOW.isoformat()
    assert archived["native_research_estimates"][0]["price"] == "0.58"
    assert archived["historical"] is True and archived["execution_eligible"] is False
    assert archived["native_research_estimates"][0]["execution_eligible"] is False
    assert market().id not in engine._last_current_research


def test_new_sportsbook_source_market_triggers_cached_catalog_rematch():
    from evplatform.plugins.external import NativeMonitor
    monitor = object.__new__(NativeMonitor)
    monitor.engine = SimpleNamespace(reference_estimates={})
    monitor._last_research_source_ids = frozenset()
    assert monitor._research_match_refresh_due(5) is False
    monitor.engine.reference_estimates[market().id] = {"market_id": market().id}
    assert monitor._research_match_refresh_due(5) is True
    monitor._last_research_source_ids = frozenset({market().id})
    assert monitor._research_match_refresh_due(5) is False
    assert monitor._research_match_refresh_due(30) is True
    monitor.engine.reference_estimates.clear()
    assert monitor._research_match_refresh_due(5) is True


def test_depth_or_fee_gap_never_becomes_positive_research_signal():
    match = kalshi_moneyline_matches(market(), kalshi_rows(), observed_at=NOW)[0]
    insufficient = compare_minimum_lot(match, D("0.9"),
        [(D("0.4"), D("0.2")), (D("0.5"), D("0.3"))],
        fee_for=lambda *_: D(0), uncertainty_margin_probability=D(0), now=NOW)
    missing_fee = compare_minimum_lot(match, D("0.9"), [(D("0.4"), D(5))],
        fee_for=lambda *_: (_ for _ in ()).throw(ValueError("missing")),
        uncertainty_margin_probability=D(0), now=NOW)
    assert insufficient["positive_after_costs"] is False and "Insufficient" in insufficient["rejection"]
    assert missing_fee["positive_after_costs"] is False and "fee" in missing_fee["rejection"]


def test_runtime_research_comparison_is_read_only_and_requires_live_stream(monkeypatch):
    import evplatform.core.pricing as pricing
    monkeypatch.setattr(pricing, "validate_fair_references", lambda *_: None)
    current = utcnow()
    found = kalshi_moneyline_matches(market(), kalshi_rows(), observed_at=current)
    ticker = f"{EVENT}-ATL"
    book = KalshiBook()
    book.apply({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
        "market_ticker": ticker, "yes_dollars_fp": [["0.55", "5"]],
        "no_dollars_fp": [["0.56", "7"]]}})
    evidence = fee_evidence()
    monitor = SimpleNamespace(client=SimpleNamespace(
        settings=SimpleNamespace(environment="production"), book=book,
        health_check=lambda: {"streaming": True}),
        task=SimpleNamespace(done=lambda: False),
        context=SimpleNamespace(config={"discovery_interval_seconds": 300}),
        research_catalog_at=current, research_matches={market().id: found},
        research_fee_metadata={EVENT: dict(zip(
            ("series", "event", "series_changes", "event_changes"), evidence), observed_at=current)})
    engine = SimpleNamespace(settings=SimpleNamespace(mode="production",
        pricing=PricingConfig(trusted_research_margin_probability=D("0.01"))),
        registry=SimpleNamespace(records={"kalshi": SimpleNamespace(enabled=True, plugin=monitor)}))
    runtime = SimpleNamespace(engine=engine, account_snapshots={})
    fair = SimpleNamespace(probabilities={"Atlanta Hawks": D("0.5734"),
        "Charlotte Hornets": D("0.4266")}, reference_profile_id="nba",
        references=[{"bookmaker": "pinnacle"}, {"bookmaker": "fanduel"}])
    result = NativeRuntime.research_comparisons(runtime, market(), fair)
    yes = next(item for item in result if item["native_market_id"] == ticker and item["side"] == "yes")
    assert yes["price"] == "0.56"
    assert D(yes["taker_fee_usd"]) == D("0.02")
    assert yes["fee_evidence"]["series_fee_multiplier"] == "1"
    assert yes["fee_evidence"]["observed_at"] == current.isoformat()
    assert yes["positive_after_costs"] is False
    assert yes["execution_eligible"] is False and yes["mapping_reviewed"] is False
    engine.settings.pricing = PricingConfig(trusted_research_margin_probability=D("0.01"),
        trusted_research_market_margins={market().id: D("0.03")})
    scoped = NativeRuntime.research_comparisons(runtime, market(), fair)
    exact = next(item for item in scoped if item["native_market_id"] == ticker and item["side"] == "yes")
    assert exact["uncertainty_allowance_policy"] == "exact_market_research_scenario"
    assert exact["uncertainty_allowance_probability"] == "0.03"
    assert D(exact["net_estimated_profit_usd"]) == (
        D(exact["fee_adjusted_estimated_profit_usd"]) - D("0.03"))
    assert exact["execution_eligible"] is False
    monitor.task = SimpleNamespace(done=lambda: True)
    assert NativeRuntime.research_comparisons(runtime, market(), fair) == []


def test_novig_review_lead_keeps_exact_fee_phase_evidence(monkeypatch):
    import evplatform.core.pricing as pricing
    monkeypatch.setattr(pricing, "validate_fair_references", lambda *_: None)
    current = utcnow()
    found = novig_prop_matches(prop_market(), novig_prop_rows(), {E: novig_event()},
        observed_at=current)
    monitor = SimpleNamespace(client=SimpleNamespace(
        settings=SimpleNamespace(environment="production"),
        book=SimpleNamespace(asks=lambda *_: [(D("0.58"), D("6027"))]),
        health_check=lambda: {"streaming": True}),
        task=SimpleNamespace(done=lambda: False),
        context=SimpleNamespace(config={"discovery_interval_seconds": 300}),
        research_catalog_at=current, research_matches={prop_market().id: found},
        research_catalog_rows=novig_prop_rows(), research_events={E: novig_event()})
    runtime = SimpleNamespace(engine=SimpleNamespace(settings=SimpleNamespace(mode="production",
        pricing=PricingConfig(trusted_research_margin_probability=D("0.01"))),
        registry=SimpleNamespace(records={"novig": SimpleNamespace(enabled=True, plugin=monitor)})),
        account_snapshots={})
    fair = SimpleNamespace(probabilities={"over": D("0.60"), "under": D("0.40")},
        reference_profile_id="mlb_prop", references=[{"bookmaker": "pinnacle"}, {"bookmaker": "fanduel"}])
    result = NativeRuntime.research_comparisons(runtime, prop_market(), fair)
    over = next(item for item in result if item["outcome"] == "over")
    assert over["fee_evidence"]["charged"] == "WHEN_LIVE"
    assert over["fee_evidence"]["coefficient"] == "0.06"
    assert over["fee_evidence"]["event_phase"] == "OPEN_PREGAME"
    assert over["taker_fee_usd"] == "0"
    assert over["normal_payout_usd"] == "1.00"
    assert over["execution_eligible"] is False
    monitor.research_catalog_rows[0]["fee"] = None
    without_fee = NativeRuntime.research_comparisons(runtime, prop_market(), fair)
    assert all(item["rejection"] == "Applicable native fee metadata is unavailable"
               for item in without_fee)


def test_exact_research_review_draft_persists_without_approving_mapping(tmp_path):
    from scripts.export_native_research_review import review_payload, save_review
    match = kalshi_moneyline_matches(market(), kalshi_rows(), observed_at=NOW)[0]
    lead = {**match.public(), "quality": "provisional_native_research",
            "price": "0.56", "execution_eligible": False}
    source = {"market_id": market().id, "event_name": market().event_name,
              "start_time": START.isoformat(), "probabilities": {"Atlanta Hawks": "0.5734"},
              "references": [{"bookmaker": "pinnacle"}, {"bookmaker": "fanduel"}],
              "native_research_estimates": [lead]}
    state = {"mode": "production", "auto_trade": False, "kill_switch": True,
             "reference_estimates": [source], "timestamp": NOW.isoformat()}
    arguments = dict(source_market_id=market().id, venue="kalshi",
                     native_market_id=lead["native_market_id"],
                     native_outcome_id=lead["native_outcome_id"])
    payload = review_payload(state, **arguments)
    path = save_review(payload, tmp_path)
    assert path.exists() and save_review(payload, tmp_path) == path
    assert payload["execution_authorized"] is False
    assert payload["native_discovery"]["normal_condition_reviewed"] is False
    assert not (tmp_path / "native_mappings.json").exists()
    with pytest.raises(ValueError, match="exactly one"):
        review_payload(state, **(arguments | {"native_market_id": "other"}))
    with pytest.raises(ValueError, match="disarmed"):
        review_payload(state | {"kill_switch": False}, **arguments)


def test_research_review_api_saves_only_unapproved_draft(tmp_path):
    from fastapi.testclient import TestClient
    from evplatform.api import create_app
    from evplatform.core.config import Settings

    app = create_app(Settings(_env_file=None, mode="production", live_enabled=False,
        auto_trade=False, database_path=str(tmp_path / "api.db"), data_dir=str(tmp_path / "data")))
    match = kalshi_moneyline_matches(market(), kalshi_rows(), observed_at=NOW)[0]
    lead = {**match.public(), "quality": "provisional_native_research",
            "price": "0.56", "mapping_reviewed": False, "execution_eligible": False}
    state = {"mode": "production", "auto_trade": False, "kill_switch": True,
        "timestamp": NOW.isoformat(), "reference_estimates": [{"market_id": market().id,
            "event_name": market().event_name, "start_time": START.isoformat(),
            "probabilities": {"Atlanta Hawks": "0.5734"},
            "native_research_estimates": [lead]}]}
    body = {"source_market_id": market().id, "venue": "kalshi",
        "native_market_id": lead["native_market_id"],
        "native_outcome_id": lead["native_outcome_id"]}
    with TestClient(app) as client:
        app.state.engine.state = lambda: state
        assert client.post("/api/native/research-review/drafts", json=body).status_code == 403
        response = client.post("/api/native/research-review/drafts",
            headers={"X-EV-Control": "local"}, json=body)
        assert response.status_code == 200
        assert response.json()["execution_authorized"] is False
        listed = client.get("/api/native/research-review/drafts").json()["items"]
        assert len(listed) == 1 and listed[0]["status"] == "unapproved_research_draft"
        assert client.get("/api/native/orders").json()["orders"] == []
