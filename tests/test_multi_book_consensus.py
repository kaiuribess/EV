"""The new consensus is research-only; sharp and execution policy stay separate."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from evplatform.core.config import PricingConfig
from evplatform.core.models import Market, ProviderFrame, Quote, Settlement
from evplatform.core.native_research import ResearchNativeMatch
from evplatform.core.native_runtime import NativeRuntime
from evplatform.core.multi_book_consensus import (consensus_fair_value, consensus_scope,
    leave_one_book_out)
from evplatform.core.pricing import NoSignal, fair_value, validate_fair_references
from evplatform.core.reference_profiles import ProfileSelector, ReferenceProfile
from evplatform.plugins.oddize_research import OddizeResearchMatcher

NOW = datetime(2026, 10, 23, 18, tzinfo=UTC)


def market():
    return Market(id="research:parlay:fixture", event_id="research:parlay:event",
        event_name="Oklahoma City Thunder at Denver Nuggets", sport="basketball",
        league="NBA", participants=("Denver Nuggets", "Oklahoma City Thunder"),
        start_time=NOW+timedelta(hours=1), status="scheduled", market_type="moneyline",
        outcomes=("Denver Nuggets", "Oklahoma City Thunder"),
        settlement=Settlement(period="FULL", overtime="unverified",
            void_rule="unreviewed_source_rules", push_rule="unverified"),
        extensions={"research_only": True})


def quote(book, provider="parlay_rest", *, odds=("1.80", "2.10"), **changes):
    m = market()
    base = Quote(market_id=m.id, market_fingerprint=m.identity(), provider=provider,
        bookmaker=book, decimal_odds=dict(zip(m.outcomes, map(D, odds))), status="unknown",
        source_time=NOW-timedelta(minutes=2), received_at=NOW,
        source_scope_poll_at=NOW-timedelta(seconds=1), source_scope_is_current=True,
        verification_reason="unverified_observation_clock",
        observation_verified=False, observation_complete=False,
        market_status_verified=False, normal_outcome_verified=False,
        settlement_equivalence_verified=False)
    return base.model_copy(update=changes)


def config():
    profile = ReferenceProfile(selectors=(ProfileSelector(sport="basketball", league="NBA",
        market_type="moneyline"),), weights_percent={"pinnacle": D(35), "circa": D(35),
            "fanduel": D(10), "draftkings": D(10), "betonline": D(10)},
        required_any_anchor=("pinnacle", "circa"), min_distinct_books=2,
        status="research_prior", live_execution_allowed=False)
    return PricingConfig(reference_profile_mode="strict", reference_quality_mode="trusted_provider",
        normal_outcome_policy="winner_only_binary", reference_profiles={"nba": profile},
        enabled_reference_profiles=["nba"], multi_book_consensus_enabled=True)


def test_two_regular_books_price_without_sharp_anchor_but_not_for_execution():
    m, quotes, pricing = market(), [quote("fanduel"), quote("draftkings")], config()
    with pytest.raises(NoSignal, match="anchor"):
        fair_value(m, quotes, pricing, NOW)
    fair, research, basis = consensus_fair_value(m, quotes, pricing, NOW)
    assert basis == "research_prior:nba"
    assert len(fair.references) == 2
    assert {r["bookmaker"] for r in fair.references} == {"fanduel", "draftkings"}
    assert all(D(r["effective_weight_percent"]) == 50 for r in fair.references)
    assert not fair.execution_eligible and not fair.live_execution_allowed
    validate_fair_references(m, fair, research, NOW+timedelta(seconds=1))
    assert set(leave_one_book_out(fair)) == {"fanduel", "draftkings"}


def test_unranked_extra_book_uses_explicit_neutral_research_baseline():
    _, policy, basis = None, *consensus_scope(market(),
        [quote("fanduel"), quote("draftkings"), quote("betmgm")], config(), NOW)
    assert basis == "neutral_equal_books_unranked"
    assert set(policy.weights) == {"fanduel", "draftkings", "betmgm"}
    assert sum(policy.weights.values()) == 100


def test_two_api_copies_of_fanduel_count_once_and_do_not_pick_best_ev_copy():
    m = market()
    observed = quote("fanduel", "oddize_rest", odds=("1.70", "2.25"),
        source_observed_at=NOW-timedelta(seconds=2), observation_verified=True,
        observation_complete=True, verification_reason="oddize_price_observation_status_unknown")
    fair, _, _ = consensus_fair_value(m,
        [quote("fanduel", odds=("1.90", "1.90")), observed, quote("draftkings")], config(), NOW)
    assert len(fair.references) == 2
    assert next(r for r in fair.references if r["bookmaker"] == "fanduel")["provider"] == "oddize_rest"
    assert not fair.execution_eligible


@pytest.mark.parametrize("bad", ["degraded_source", "retained_top_up", "stale_cached_board"])
def test_degraded_or_cached_pair_cannot_supply_second_book(bad):
    with pytest.raises(NoSignal, match="Fewer than two"):
        consensus_fair_value(market(), [quote("fanduel"),
            quote("draftkings", verification_reason=bad)], config(), NOW)


def test_expired_or_suspended_pair_rejects_even_with_recent_delivery():
    with pytest.raises(NoSignal, match="Fewer than two"):
        consensus_fair_value(market(), [quote("fanduel"),
            quote("draftkings", source_scope_poll_at=NOW-timedelta(minutes=5))], config(), NOW)
    with pytest.raises(NoSignal, match="Fewer than two"):
        consensus_fair_value(market(), [quote("fanduel"),
            quote("draftkings", status="suspended")], config(), NOW)


def test_exchange_and_dfs_names_cannot_enter_sportsbook_consensus():
    with pytest.raises(NoSignal, match="Fewer than two"):
        consensus_fair_value(market(), [quote("fanduel"), quote("kalshi"),
            quote("underdog")], config(), NOW)


def test_oddize_exact_event_observation_joins_only_research_market():
    m = market()
    matcher = OddizeResearchMatcher()
    matcher.register_market(m)
    observed = int((NOW-timedelta(seconds=2)).timestamp())
    event = {"event_id": "oddize-1", "sport": "nba", "team1": "OKC", "team2": "DEN",
        "start_date": m.start_time.isoformat(), "odds": [
            {"book": "fd", "offer_type": "ml", "team": team,
             "american_odds": price, "ts": observed}
            for team, price in (("DEN", "-110"), ("OKC", "+100"))]}
    frame = ProviderFrame(provider_id="oddize_rest", kind="snapshot",
        payload={"events": [event]}, received_at=NOW, snapshot_complete=True,
        metadata={"sport": "nba"})
    updates = matcher.apply_frame(frame)
    assert len(updates) == 1
    assert updates[0].bookmaker == "fanduel" and updates[0].status == "unknown"
    assert updates[0].source_observed_at == NOW-timedelta(seconds=2)
    assert not updates[0].market_status_verified and not updates[0].normal_outcome_verified
    assert matcher.apply_frame(frame) == []
    assert len(matcher.apply_frame(frame.model_copy(update={"payload": {"events": [
        {**event, "start_date": (m.start_time+timedelta(minutes=5)).isoformat()}]}}))) == 1
    assert matcher.quotes == {}  # Five-minute event mismatch withdraws the old pair.


def test_oddize_research_quote_withdrawn_when_parlay_identity_is_invalidated():
    m = market().model_copy(update={"extensions": {
        "research_only": True, "provider_sport": "basketball_nba"}})
    matcher = OddizeResearchMatcher()
    matcher.register_market(m)
    observed = int((NOW-timedelta(seconds=1)).timestamp())
    frame = ProviderFrame(provider_id="oddize_rest", kind="snapshot", received_at=NOW,
        snapshot_complete=True, metadata={"sport": "nba"}, payload={"events": [{
            "event_id": "oddize-event", "sport": "nba", "team1": "OKC", "team2": "DEN",
            "start_date": m.start_time.isoformat(), "odds": [{"book": "fd", "offer_type": "ml",
                "team": side, "american_odds": price, "ts": observed}
                for side, price in (("DEN", "-110"), ("OKC", "+100"))]}]})
    assert len(matcher.apply_frame(frame)) == 1
    withdrawn = matcher.invalidate_parlay("basketball_nba", NOW+timedelta(seconds=1))
    assert len(withdrawn) == 1 and withdrawn[0].status == "withdrawn"
    assert matcher.quotes == {}


def test_consensus_flows_to_read_only_native_fee_comparison_without_margin(monkeypatch):
    import evplatform.core.native_research as native_research
    import evplatform.core.native_runtime as native_runtime

    monkeypatch.setattr(native_runtime, "utcnow", lambda: NOW)
    monkeypatch.setattr(native_research, "kalshi_research_taker_fee", lambda *args, **kwargs: D("0.02"))
    m = market()
    fair, research, _ = consensus_fair_value(m, [quote("fanduel"), quote("draftkings")], config(), NOW)
    match = ResearchNativeMatch(venue="kalshi", source_market_id=m.id,
        source_event_id=m.event_id, source_market_fingerprint=m.identity(),
        native_market_id="FIXTURE-NBA-DEN", native_event_id="FIXTURE-NBA",
        native_outcome_id="yes", side="yes", outcome=m.outcomes[0],
        opposing_outcome_id="no", exchange_index=3, source_start=m.start_time,
        native_start=m.start_time, rules_sha256="fixture-rules", rules_excerpt="fixture",
        normal_win_condition="Denver wins", price_denomination_usd="1",
        catalog_observed_at=NOW)
    client = SimpleNamespace(settings=SimpleNamespace(environment="production"),
        book=SimpleNamespace(asks=lambda *_: [(D("0.54"), D(10))]),
        health_check=lambda: {"streaming": True})
    monitor = SimpleNamespace(client=client, task=SimpleNamespace(done=lambda: False),
        context=SimpleNamespace(config={"discovery_interval_seconds": 300}),
        research_catalog_at=NOW, research_matches={m.id: [match]},
        research_fee_metadata={"FIXTURE-NBA": {"series": {}, "event": {},
            "series_changes": {}, "event_changes": {}, "observed_at": NOW}})
    engine = SimpleNamespace(settings=SimpleNamespace(mode="production", pricing=config()),
        registry=SimpleNamespace(records={"kalshi": SimpleNamespace(enabled=True, plugin=monitor)}))
    runtime = SimpleNamespace(engine=engine, account_snapshots={})
    result = NativeRuntime.research_comparisons(runtime, m, fair, pricing_config=research)
    assert len(result) == 1
    offer = result[0]
    assert offer["reference_strategy"] == "multi_book_consensus"
    assert offer["taker_fee_usd"] == "0.02"
    assert offer["net_estimated_profit_usd"] is None
    assert offer["positive_after_costs"] is False and offer["execution_eligible"] is False
    assert [item["approved"] for item in offer["research_uncertainty_scenarios"]] == [False]*3
