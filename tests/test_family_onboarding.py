"""Offline proof that reviewed family scope can onboard new native fixtures."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evplatform.core.config import PricingConfig, Settings, TrustedPilotMarketScope, TrustedPilotPolicy
from evplatform.core.multi_book_consensus import native_live_pricing
from evplatform.core.models import utcnow
from evplatform.core.native_accounting import NativeInstrument, NativePriceBand
from evplatform.core.native_pricing import NativeBook, NativeFeeBound, NativeLevel, instrument_fingerprint, native_opportunity
from evplatform.core.native_research import kalshi_moneyline_matches, prophetx_matches
from evplatform.core.native_runtime import NativeRuntime
from evplatform.core.pricing import NoSignal, fair_value
from evplatform.core.readiness import ApprovedNativeFamilyRule, LaunchProfile, LiveLimits, MarketReadiness, family_market_ready
from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import TrustedSourceProjector, VerifiedProviderMapper
from evplatform.venues.book_normalization import NativeBookMapper
from evplatform.venues.prophetx_market_data import ProphetXMarketData
from evplatform.engine import Engine


BOOKS = ("fanduel", "draftkings", "caesars")
TEAMS = ("Charlotte Hornets", "Atlanta Hawks")
RULE = "If postponed but begins within 48 hours, resolve official result. Otherwise resolve to a fair price."
RULE_ID = "OFFLINE-NBA-full-winner-v1"


def approved_config(venue="kalshi"):
    scope = TrustedPilotMarketScope(venue=venue, sport="basketball", league="NBA",
        market_type="moneyline", reviewed_normal_rule_id=RULE_ID,
        source_normal_rule_evidence="OFFLINE reviewed NBA full game and overtime source rules")
    policy = TrustedPilotPolicy(enabled=True, owner_approved=True, market_scope=scope,
        provider_id="parlay_rest", trusted_books=list(BOOKS), min_distinct_books=3,
        reference_weights_percent={"fanduel": Decimal(40), "draftkings": Decimal(35),
            "caesars": Decimal(25)},
        accept_unknown_active_status=True, accept_unknown_quote_observation_age=True,
        max_indicator_age_ms=30000, independent_crosscheck="two_distinct_books",
        uncertainty_margin_probability=Decimal("0.01"), max_debit_usd=Decimal(25),
        max_loss_usd=Decimal(25), recurring_authorization=True,
        maximum_native_attempts=None, automatic_followups=True,
        approval_evidence="OFFLINE fixture owner source review only")
    config = PricingConfig(reference_profile_mode="strict", reference_quality_mode="trusted_provider",
        normal_outcome_policy="winner_only_binary", multi_book_consensus_enabled=True,
        live_multi_book_enabled=True, trusted_pilot_policy=policy,
        trusted_max_delivery_age_ms=30000, trusted_max_indicator_age_ms=30000)
    return config, scope


def source_fixture(config, now, event, start):
    payload = [{"id": event, "sport_key": "basketball_nba", "home_team": TEAMS[0],
        "away_team": TEAMS[1], "commence_time": start.isoformat(),
        "bookmakers": [{"key": book, "verified_at": (now-timedelta(seconds=1)).isoformat(),
            "is_current": True, "line_changed_at_ms": int((now-timedelta(seconds=1)).timestamp()*1000),
            "markets": [{"key": "h2h", "period": "FULL",
                "outcomes": [{"name": TEAMS[0], "price": 2.40},
                             {"name": TEAMS[1], "price": 1.60}]}]}
            for book in BOOKS]}]
    mapper = VerifiedProviderMapper()
    mapper.research_projector = TrustedSourceProjector(lambda: config)
    envelope = ProviderEnvelope("parlay_rest", "snapshot", payload, now, 1,
        published_at=now, metadata={"sport": "basketball_nba"})
    assert mapper(envelope) == []
    updates = mapper.research_updates
    assert len(updates) == 3
    market = updates[0][0]
    assert all(item.id == market.id for item, _ in updates)
    return market, [quote for _, quote in updates]


def native_rows(start, event):
    ticker = "KXNBAGAME-" + start.astimezone(__import__("zoneinfo").ZoneInfo(
        "America/New_York")).strftime("%y%b%d").upper() + "ATLCHA"
    rows = []
    for code, team in (("ATL", "Atlanta"), ("CHA", "Charlotte")):
        rows.append({"ticker": f"{ticker}-{code}", "event_ticker": ticker,
            "market_type": "binary", "status": "active", "exchange_index": 3,
            "occurrence_datetime": start.isoformat(),
            "rules_primary": f"If {team} wins the Atlanta vs Charlotte Pro Basketball game originally scheduled for {start.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).strftime('%b %d, %Y').replace(' 0', ' ')}, then the market resolves to Yes.",
            "rules_secondary": RULE,
            "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}]})
    return rows


def family_rule(scope, now):
    return ApprovedNativeFamilyRule(scope=scope, rule_version="OFFLINE-v1", approved_by="fixture",
        approval_evidence="OFFLINE only; exact source and native rule review", approved_at=now-timedelta(minutes=1),
        expires_at=now+timedelta(days=2), allowed_native_rule_texts=(RULE,),
        max_start_delta_seconds=0, quantity_step=Decimal(1), minimum_quantity=Decimal(1),
        minimum_order_cost_usd=Decimal(0), cash_quantum_usd=Decimal(".01"),
        order_minimum_source="OFFLINE native order-grid fixture", kalshi_exchange_index=3)


def test_two_new_games_onboard_under_one_approved_rule_without_static_ids(monkeypatch):
    now = utcnow().replace(microsecond=0)
    config, scope = approved_config()
    rule = family_rule(scope, now)
    mapper = NativeBookMapper()
    profile = SimpleNamespace(venue="kalshi", market_ids=(), family_rule=rule)
    engine = SimpleNamespace(settings=SimpleNamespace(pricing=config), markets={})
    runtime = SimpleNamespace(profile_for=lambda venue: profile, mapper=mapper, engine=engine)
    def fee_bound(instrument, *_args, observed_at, **_kwargs):
        return NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
            per_unit_usd=".01", per_fill_usd="0", fixed_order_usd="0", verified=True,
            source="OFFLINE current fee fixture", observed_at=observed_at,
            valid_until=observed_at+timedelta(seconds=60))
    monkeypatch.setattr("evplatform.venues.kalshi_fees.kalshi_taker_fee_bound", fee_bound)
    markets = []
    for index in range(2):
        # Different event identities and dates; neither appears in the launch
        # profile or persisted static mapping file.
        start = now+timedelta(days=index+1)
        market, quotes = source_fixture(config, now, f"fixture-{index}", start)
        assert market.id.startswith("reviewed:") and market.settlement.push_rule == "not_applicable"
        assert all(q.source_observed_at is None and q.status == "unknown" for q in quotes)
        rows = native_rows(start, index)
        matches = kalshi_moneyline_matches(market, rows, observed_at=now)
        assert len(matches) == 4
        monitor = SimpleNamespace(research_catalog_at=now, research_catalog_rows=rows,
            research_fee_metadata={rows[0]["event_ticker"]: {"observed_at": now,
                "series": {}, "series_changes": {}, "event": {}, "event_changes": {}}})
        assert NativeRuntime.onboard_family_matches(runtime, "kalshi", market, matches, monitor, now=now) == 2
        engine.markets[market.id] = market
        assert NativeRuntime.profile_permits_market(runtime, profile, market.id, TEAMS[0])
        assert NativeRuntime.profile_permits_market(runtime, profile, market.id, TEAMS[1])
        priced = native_live_pricing(market, quotes, config, now)
        fair = fair_value(market, quotes, priced, now)
        assert fair.execution_eligible and fair.live_execution_allowed
        assert {r["bookmaker"] for r in fair.references} == set(BOOKS)
        markets.append((market, quotes, fair))
    assert len(mapper.mappings) == 4 and mapper.mapping_digest == NativeBookMapper().mapping_digest
    assert markets[0][0].id != markets[1][0].id


def test_family_rejects_changed_rules_missing_evidence_and_stale_quotes(monkeypatch):
    now = utcnow().replace(microsecond=0)
    config, scope = approved_config()
    market, quotes = source_fixture(config, now, "fixture-reject", now+timedelta(days=1))
    rows = native_rows(market.start_time, "fixture-reject")
    matches = kalshi_moneyline_matches(market, rows, observed_at=now)
    rule = family_rule(scope, now)
    profile = SimpleNamespace(venue="kalshi", market_ids=(), family_rule=rule)
    runtime = SimpleNamespace(profile_for=lambda venue: profile, mapper=NativeBookMapper(),
        engine=SimpleNamespace(settings=SimpleNamespace(pricing=config), markets={market.id: market}))
    monitor = SimpleNamespace(research_catalog_at=now, research_catalog_rows=rows,
        research_fee_metadata={rows[0]["event_ticker"]: {"observed_at": now}})
    changed = [dict(row, rules_secondary="different settlement") for row in rows]
    assert NativeRuntime.onboard_family_matches(runtime, "kalshi", market, matches,
        SimpleNamespace(**{**monitor.__dict__, "research_catalog_rows": changed}), now=now) == 0
    assert NativeRuntime.onboard_family_matches(runtime, "kalshi", market, matches,
        SimpleNamespace(**{**monitor.__dict__, "research_catalog_at": now-timedelta(seconds=61)}), now=now) == 0
    assert NativeRuntime.onboard_family_matches(runtime, "kalshi", market, matches,
        SimpleNamespace(**{**monitor.__dict__, "research_fee_metadata": {}}), now=now) == 0
    assert not runtime.mapper.mappings
    with pytest.raises(NoSignal):
        native_live_pricing(market, [q.model_copy(update={"received_at": now-timedelta(seconds=31)})
            for q in quotes], config, now)


def test_changed_native_rule_revokes_existing_family_market(monkeypatch):
    now = utcnow().replace(microsecond=0)
    config, scope = approved_config()
    market, _ = source_fixture(config, now, "fixture-rule-change", now+timedelta(days=1))
    rows = native_rows(market.start_time, "fixture-rule-change")
    matches = kalshi_moneyline_matches(market, rows, observed_at=now)
    rule = family_rule(scope, now)
    profile = SimpleNamespace(venue="kalshi", market_ids=(), family_rule=rule)
    runtime = SimpleNamespace(profile_for=lambda venue: profile, mapper=NativeBookMapper(),
        engine=SimpleNamespace(settings=SimpleNamespace(pricing=config), markets={market.id: market}))
    def fee_bound(instrument, *_args, observed_at, **_kwargs):
        return NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
            per_unit_usd=".01", per_fill_usd="0", fixed_order_usd="0", verified=True,
            source="OFFLINE current fee fixture", observed_at=observed_at,
            valid_until=observed_at+timedelta(seconds=60))
    monkeypatch.setattr("evplatform.venues.kalshi_fees.kalshi_taker_fee_bound", fee_bound)
    monitor = SimpleNamespace(research_catalog_at=now, research_catalog_rows=rows,
        research_fee_metadata={rows[0]["event_ticker"]: {"observed_at": now,
            "series": {}, "series_changes": {}, "event": {}, "event_changes": {}}})
    assert NativeRuntime.reconcile_family_catalog(runtime, "kalshi", market, matches, monitor) == 2
    assert NativeRuntime.profile_permits_market(runtime, profile, market.id, TEAMS[0])
    monitor.research_catalog_rows = [dict(row, rules_secondary="changed payout rule") for row in rows]
    assert NativeRuntime.reconcile_family_catalog(runtime, "kalshi", market, matches, monitor) == 0
    assert not NativeRuntime.profile_permits_market(runtime, profile, market.id, TEAMS[0])
    assert len(runtime.mapper.mappings) == 2  # original audit evidence is retained


def test_prophetx_family_requires_exact_period_strikes_and_fee_bound():
    now = utcnow().replace(microsecond=0)
    config, scope = approved_config("prophetx")
    market, _ = source_fixture(config, now, "fixture-px", now+timedelta(days=1))
    catalog = ProphetXMarketData()
    catalog.replace_catalog([{"data": {"123": [{"id": 219, "name": "Moneyline",
        "type": "moneyline", "sub_type": "moneyline", "period": "full_game",
        "status": "active", "selections": [[{"strike_id": "atl-strike", "outcome_id": 4,
        "name": "Atlanta Hawks", "status": "active"}],
        [{"strike_id": "cha-strike", "outcome_id": 5,
        "name": "Charlotte Hornets", "status": "active"}]]}]}}], ["123"], observed_at=now)
    events = {"123": {"event_id": 123, "name": market.event_name,
        "scheduled": market.start_time.isoformat(), "status": "not_started", "tournament_id": 132}}
    matches = prophetx_matches(market, catalog.catalog, events, observed_at=now)
    assert {item.native_strike_id for item in matches} == {"atl-strike", "cha-strike"}
    rule = ApprovedNativeFamilyRule(scope=scope, rule_version="OFFLINE-px-v1",
        approved_by="fixture", approval_evidence="OFFLINE only; exact ProphetX catalog review",
        approved_at=now-timedelta(minutes=1), expires_at=now+timedelta(days=2),
        allowed_native_rule_texts=("moneyline|moneyline|full_game",),
        max_start_delta_seconds=0, quantity_step=Decimal(".01"),
        minimum_quantity=Decimal(1), minimum_order_cost_usd=Decimal(1),
        cash_quantum_usd=Decimal(".01"), order_minimum_source="OFFLINE native order-grid fixture",
        prophetx_winnings_fee_upper_per_stake=Decimal(".05"),
        prophetx_fee_source="OFFLINE conservative winnings fee cap",
        prophetx_native_settlement_evidence="OFFLINE reviewed game-winner settlement fixture",
        prophetx_source_timestamp_unit="nanoseconds",
        prophetx_timestamp_unit_evidence="OFFLINE confirmed v2 clock unit")
    profile = SimpleNamespace(venue="prophetx", market_ids=(), family_rule=rule)
    runtime = SimpleNamespace(profile_for=lambda venue: profile, mapper=NativeBookMapper(),
        engine=SimpleNamespace(settings=SimpleNamespace(pricing=config), markets={market.id: market}))
    monitor = SimpleNamespace(research_catalog_at=now,
        client=SimpleNamespace(public_market_data=catalog), research_events=events)
    assert NativeRuntime.reconcile_family_catalog(runtime, "prophetx", market, matches, monitor) == 2
    assert {item.instrument.instrument_id for item in runtime.mapper.mappings} == {"atl-strike", "cha-strike"}
    assert all(item.fee_bound.fee_payment_timing == "winnings_only" for item in runtime.mapper.mappings)
    assert NativeRuntime.profile_permits_market(runtime, profile, market.id, TEAMS[1])
    catalog.catalog[("123", "219")]["period"] = None
    assert NativeRuntime.reconcile_family_catalog(runtime, "prophetx", market, matches, monitor) == 0
    assert not NativeRuntime.profile_permits_market(runtime, profile, market.id, TEAMS[1])


def test_non_nba_prophetx_family_requires_explicit_full_period_and_league():
    now = utcnow().replace(microsecond=0)
    config, _ = approved_config("prophetx")
    scope = TrustedPilotMarketScope(venue="prophetx", sport="football", league="NFL",
        market_type="moneyline", reviewed_normal_rule_id=RULE_ID,
        source_normal_rule_evidence="OFFLINE reviewed NFL full-game winner source rule")
    config = config.model_copy(update={"trusted_pilot_policy":
        config.trusted_pilot_policy.model_copy(update={"market_scope": scope})})
    base, _ = source_fixture(config, now, "fixture-nfl-family", now+timedelta(days=1))
    market = base.model_copy(update={"id": "reviewed:nfl-family-fixture", "sport": "football", "league": "NFL",
        "settlement": base.settlement.model_copy(update={"overtime": "included"}),
        "extensions": {**base.extensions, "research_only": False,
            "reviewed_normal_rule_id": RULE_ID, "normal_outcome_reviewed": True}})
    native = {"name": "Moneyline", "type": "moneyline", "sub_type": "moneyline",
        "period": "full_game", "status": "active", "strikes": {
            "atl": {"name": TEAMS[1], "outcome_id": "4", "status": "active"},
            "cha": {"name": TEAMS[0], "outcome_id": "5", "status": "active"}}}
    catalog = {("123", "219"): native}
    events = {"123": {"event_id": 123, "name": market.event_name,
        "scheduled": market.start_time.isoformat(), "status": "not_started", "league": "NFL"}}
    matches = prophetx_matches(market, catalog, events, observed_at=now)
    assert len(matches) == 2
    rule = ApprovedNativeFamilyRule(scope=scope, rule_version="OFFLINE-nfl-v1",
        approved_by="fixture", approval_evidence="OFFLINE exact NFL family rule fixture",
        approved_at=now-timedelta(minutes=1), expires_at=now+timedelta(days=1),
        allowed_native_rule_texts=("moneyline|moneyline|full_game",),
        quantity_step=Decimal(".01"), minimum_quantity=Decimal(1),
        minimum_order_cost_usd=Decimal(1), cash_quantum_usd=Decimal(".01"),
        order_minimum_source="OFFLINE ProphetX minimum fixture",
        prophetx_fee_evidence_kind="owner_approved_assumption",
        prophetx_assumed_winnings_profit_rate=Decimal(".02"),
        prophetx_fee_source="OFFLINE fee assumption fixture",
        prophetx_native_settlement_evidence="OFFLINE reviewed NFL settlement fixture",
        prophetx_source_timestamp_unit="nanoseconds",
        prophetx_timestamp_unit_evidence="OFFLINE catalog timestamp fixture")
    profile = SimpleNamespace(venue="prophetx", market_ids=(), family_rule=rule)
    assert rule.matches(market)
    runtime = SimpleNamespace(profile_for=lambda venue: profile, mapper=NativeBookMapper(),
        engine=SimpleNamespace(settings=SimpleNamespace(pricing=config), markets={market.id: market}))
    monitor = SimpleNamespace(research_catalog_at=now, research_events=events,
        client=SimpleNamespace(public_market_data=SimpleNamespace(catalog=catalog)))
    assert NativeRuntime.onboard_family_matches(runtime, "prophetx", market, matches, monitor, now=now) == 2
    native["period"] = None
    assert NativeRuntime.reconcile_family_catalog(runtime, "prophetx", market, matches, monitor) == 0
    assert not NativeRuntime.profile_permits_market(runtime, profile, market.id, TEAMS[0])


def test_prophetx_prop_family_needs_exact_player_stat_line_and_period():
    now = utcnow().replace(microsecond=0)
    config, _ = approved_config("prophetx")
    scope = TrustedPilotMarketScope(venue="prophetx", sport="football", league="NFL",
        market_type="player_prop", statistics=("receptions",), reviewed_normal_rule_id=RULE_ID,
        source_normal_rule_evidence="OFFLINE reviewed NFL receptions source rule")
    config = config.model_copy(update={"trusted_pilot_policy":
        config.trusted_pilot_policy.model_copy(update={"market_scope": scope})})
    base, _ = source_fixture(config, now, "fixture-prop-family", now+timedelta(days=1))
    market = base.model_copy(update={"id": "reviewed:prop-family-fixture", "sport": "football",
        "league": "NFL", "market_type": "player_prop", "player": "Example Player",
        "player_id": "player-7", "statistic": "receptions", "line": Decimal("3.5"),
        "outcomes": ("over", "under"),
        "settlement": base.settlement.model_copy(update={"overtime": "included"}),
        "extensions": {**base.extensions, "research_only": False,
            "reviewed_normal_rule_id": RULE_ID, "normal_outcome_reviewed": True}})
    native = {"name": "Player Receptions", "type": "player_prop", "sub_type": "receptions",
        "period": "full_game", "player_id": "player-7", "statistic": "receptions",
        "line": "3.5", "status": "active", "strikes": {
            "over-strike": {"name": "Over", "outcome_id": "4", "status": "active"},
            "under-strike": {"name": "Under", "outcome_id": "5", "status": "active"}}}
    catalog = {("123", "219"): native}
    events = {"123": {"event_id": 123, "name": market.event_name,
        "scheduled": market.start_time.isoformat(), "status": "not_started", "league": "NFL"}}
    matches = prophetx_matches(market, catalog, events, observed_at=now)
    assert len(matches) == 2
    assert all("Example Player" in item.normal_win_condition for item in matches)
    rule = ApprovedNativeFamilyRule(scope=scope, rule_version="OFFLINE-prop-v1",
        approved_by="fixture", approval_evidence="OFFLINE exact prop rule fixture",
        approved_at=now-timedelta(minutes=1), expires_at=now+timedelta(days=1),
        allowed_native_rule_texts=("player_prop|receptions|full_game",),
        quantity_step=Decimal(".01"), minimum_quantity=Decimal(1),
        minimum_order_cost_usd=Decimal(1), cash_quantum_usd=Decimal(".01"),
        order_minimum_source="OFFLINE ProphetX minimum fixture",
        prophetx_fee_evidence_kind="owner_approved_assumption",
        prophetx_assumed_winnings_profit_rate=Decimal(".02"),
        prophetx_fee_source="OFFLINE fee assumption fixture",
        prophetx_native_settlement_evidence="OFFLINE reviewed NFL prop settlement fixture",
        prophetx_source_timestamp_unit="nanoseconds",
        prophetx_timestamp_unit_evidence="OFFLINE catalog timestamp fixture")
    profile = SimpleNamespace(venue="prophetx", market_ids=(), family_rule=rule)
    runtime = SimpleNamespace(profile_for=lambda venue: profile, mapper=NativeBookMapper(),
        engine=SimpleNamespace(settings=SimpleNamespace(pricing=config), markets={market.id: market}))
    monitor = SimpleNamespace(research_catalog_at=now, research_events=events,
        client=SimpleNamespace(public_market_data=SimpleNamespace(catalog=catalog)))
    assert NativeRuntime.onboard_family_matches(runtime, "prophetx", market, matches, monitor, now=now) == 2
    native["line"] = "4.5"
    assert NativeRuntime.reconcile_family_catalog(runtime, "prophetx", market, matches, monitor) == 0
    assert not NativeRuntime.profile_permits_market(runtime, profile, market.id, "over")


def test_prophetx_certified_default_full_game_rule_needs_exact_unqualified_moneyline():
    now = utcnow().replace(microsecond=0)
    config, scope = approved_config("prophetx")
    market, _ = source_fixture(config, now, "default-period-fixture", now+timedelta(days=1))
    catalog = ProphetXMarketData()
    catalog.replace_catalog([{"data": {"123": [{"id": 219, "name": "Moneyline",
        "type": "moneyline", "sub_type": "moneyline", "period": None,
        "status": "active", "selections": [[{"strike_id": "atl-strike", "outcome_id": 4,
        "name": "Atlanta Hawks", "status": "active"}],
        [{"strike_id": "cha-strike", "outcome_id": 5,
        "name": "Charlotte Hornets", "status": "active"}]]}]}}], ["123"], observed_at=now)
    events = {"123": {"event_id": 123, "name": market.event_name,
        "scheduled": market.start_time.isoformat(), "status": "not_started", "tournament_id": 132}}
    matches = prophetx_matches(market, catalog.catalog, events, observed_at=now)
    assert len(matches) == 2
    assert all(item.identity_limitations == ("Native period is not explicit in the catalog",)
               for item in matches)
    rule = ApprovedNativeFamilyRule(scope=scope, rule_version="OFFLINE-px-default-v1",
        approved_by="fixture", approval_evidence="OFFLINE approved exact default-full-game rule",
        approved_at=now-timedelta(minutes=1), expires_at=now+timedelta(days=2),
        allowed_native_rule_texts=("moneyline|moneyline|default_full_game",),
        max_start_delta_seconds=0, quantity_step=Decimal(".01"),
        minimum_quantity=Decimal(1), minimum_order_cost_usd=Decimal(1),
        cash_quantum_usd=Decimal(".01"), order_minimum_source="OFFLINE order-grid fixture",
        prophetx_fee_evidence_kind="owner_approved_assumption",
        prophetx_assumed_winnings_profit_rate=Decimal(".02"),
        prophetx_fee_source="OWNER ASSUMPTION: 2% of winning profit",
        prophetx_native_settlement_evidence="CFTC NBA filing 61599: default full event including overtime",
        prophetx_source_timestamp_unit="nanoseconds",
        prophetx_timestamp_unit_evidence="OFFLINE v2 clock fixture")
    profile = SimpleNamespace(venue="prophetx", market_ids=(), family_rule=rule)
    runtime = SimpleNamespace(profile_for=lambda venue: profile, mapper=NativeBookMapper(),
        engine=SimpleNamespace(settings=SimpleNamespace(pricing=config), markets={market.id: market}))
    monitor = SimpleNamespace(research_catalog_at=now,
        client=SimpleNamespace(public_market_data=catalog), research_events=events)
    assert NativeRuntime.onboard_family_matches(runtime, "prophetx", market, matches, monitor, now=now) == 2
    assert all(item.fee_bound.evidence_kind == "owner_approved_assumption"
               for item in runtime.mapper.mappings)
    catalog.catalog[("123", "219")]["name"] = "First Half Moneyline"
    assert NativeRuntime.reconcile_family_catalog(runtime, "prophetx", market, matches, monitor) == 0
    assert not NativeRuntime.profile_permits_market(runtime, profile, market.id, TEAMS[0])


def test_conditional_prophetx_first_trade_profile_uses_family_without_future_game_ids():
    now = utcnow().replace(microsecond=0)
    _, scope = approved_config("prophetx")
    rule = ApprovedNativeFamilyRule(scope=scope, rule_version="OFFLINE-px-pilot-v1",
        approved_by="fixture", approval_evidence="OFFLINE reviewed full-game winner rule",
        approved_at=now-timedelta(minutes=1), expires_at=now+timedelta(hours=12),
        allowed_native_rule_texts=("moneyline|moneyline|default_full_game",),
        quantity_step=Decimal(".01"), minimum_quantity=Decimal(1),
        minimum_order_cost_usd=Decimal(1), cash_quantum_usd=Decimal(".01"),
        order_minimum_source="OFFLINE order-grid fixture",
        prophetx_fee_evidence_kind="owner_approved_assumption",
        prophetx_assumed_winnings_profit_rate=Decimal(".02"),
        prophetx_fee_source="OWNER ASSUMPTION: 2% of winning profit",
        prophetx_native_settlement_evidence="OFFLINE full-game rule fixture",
        prophetx_source_timestamp_unit="nanoseconds",
        prophetx_timestamp_unit_evidence="OFFLINE v2 clock fixture")
    caps = {key: Decimal(5) for key in ("max_order_stake_usd", "max_event_exposure_usd",
        "max_player_exposure_usd", "max_venue_exposure_usd",
        "max_correlated_exposure_usd", "max_total_exposure_usd",
        "daily_loss_limit_usd", "max_native_daily_debit_usd")}
    limits = LiveLimits(**caps, available_cash_only=True,
        block_overlapping_events=True, block_overlapping_players=True,
        stake_sizing="quarter_kelly", max_open_orders=1, max_orders_per_minute=1,
        max_native_order_attempts=1)
    profile = LaunchProfile(venue="prophetx", environment="production",
        account_fingerprint="a"*64, build_id="OFFLINE-build", family_rule=rule,
        strategy_ids=("native_positive_ev",), provider_ids=("parlay_game_ws",),
        mapping_digest="b"*64, pricing_digest="c"*64, limits=limits,
        min_reference_books=3, max_reference_age_ms=30000,
        max_orderbook_age_ms=2500,
        settlement_policy="prophetx_funded_cash_win_loss_halt_on_exception",
        validation_pilot=True)
    assert profile.market_ids == () and profile.limits.max_native_order_attempts == 1
    assert profile.digest() == profile.model_copy(update={"evidence": ()}).digest()
    for changed in ({"max_native_order_attempts": None}, {"max_order_stake_usd": Decimal(6)},
                    {"daily_loss_limit_usd": Decimal(6)}):
        with pytest.raises(ValueError):
            LaunchProfile.model_validate({**profile.model_dump(),
                "limits": {**limits.model_dump(), **changed}})
    from scripts.prepare_prophetx_conditional_pilot import prepare
    recurring_limits = LiveLimits.model_validate({**limits.model_dump(),
        **{name: Decimal(25) for name in caps}, "max_native_order_attempts": None})
    recurring = LaunchProfile.model_validate({**profile.model_dump(),
        "validation_pilot": False, "limits": recurring_limits.model_dump()})
    draft = prepare(recurring)
    assert draft["approved"] is False
    assert draft["authorization_template"]["authorization_id"] is None
    assert draft["launch_profile"]["market_ids"] == []
    assert draft["launch_profile"]["limits"]["max_order_stake_usd"] == "5"
    assert draft["launch_profile"]["limits"]["max_native_order_attempts"] == 1


def test_conditional_prophetx_pilot_reaches_existing_native_preflight(monkeypatch):
    from evplatform.core.risk import RiskConfig, RiskEngine, RiskRejected
    now = utcnow().replace(microsecond=0)
    pricing, scope = approved_config("prophetx")
    market, _ = source_fixture(pricing, now, "conditional-preflight", now+timedelta(hours=3))
    risk = RiskEngine(RiskConfig(available_cash_only=True, stake_sizing="quarter_kelly",
        max_order_stake=Decimal(5), live_stake_authorized=True))
    risk.auto_trade, risk.kill_switch = True, False
    account = "a"*64
    rule = SimpleNamespace(scope=scope, approved_at=now-timedelta(minutes=1),
        expires_at=now+timedelta(hours=2))
    profile = SimpleNamespace(venue="prophetx", environment="production",
        account_fingerprint=account, market_ids=(), family_rule=rule,
        strategy_ids=("native_positive_ev",), validation_pilot=True,
        settlement_policy="prophetx_funded_cash_win_loss_halt_on_exception",
        min_reference_books=3, max_reference_age_ms=30000, max_orderbook_age_ms=2500,
        limits=SimpleNamespace(max_native_order_attempts=1,
            max_order_stake_usd=Decimal(5), max_total_exposure_usd=Decimal(5)),
        evidence=(SimpleNamespace(expires_at=now+timedelta(minutes=1)),),
        digest=lambda: "OFFLINE-conditional-profile")
    account_view = SimpleNamespace(cash_available_verified=True, balance_usd=Decimal(49),
        identity_verified=True, account_fingerprint=account, environment="production")
    adapter = SimpleNamespace(client=object(), preflight=None)
    runtime = SimpleNamespace(report=lambda *_: SimpleNamespace(live_trading_ready=True,
        sandbox_trading_ready=False, safe_to_arm=True), profile_for=lambda *_: profile,
        runtime_facts=lambda *_: SimpleNamespace(markets=(MarketReadiness(
            market_id=market.id, identity_verified=True, provider_mapping_verified=True,
            venue_mapping_verified=True, fee_assumption_approved=True,
            native_units_supported=True, order_minimum_verified=True,
            usable_reference_books=3, required_books_present=True,
            oldest_reference_age_ms=100, orderbook_age_ms=100,
            orderbook_synchronized=True, market_active=True,
            executable_liquidity=True, synthetic=False),)),
        account_snapshots={"prophetx": account_view},
        manager=SimpleNamespace(adapters={"prophetx": adapter}),
        _prophetx_location_valid=lambda *_: True,
        engine=SimpleNamespace(settings=SimpleNamespace(live_enabled=True, pricing=pricing),
            markets={market.id: market}, risk=risk))
    monkeypatch.setattr(NativeRuntime, "profile_permits_market", lambda *_: True)
    order = SimpleNamespace(id="OFFLINE-intent", venue="prophetx", mode="production",
        account_fingerprint=account, market_id=market.id, outcome=TEAMS[0],
        strategy="native_positive_ev", reserved_cost=Decimal(5),
        execution_origin="automatic")
    NativeRuntime.require_preflight(runtime, order)
    assert adapter.preflight.native_trading_enabled
    with pytest.raises(RiskRejected):
        NativeRuntime.require_preflight(runtime, SimpleNamespace(**{**vars(order),
            "reserved_cost": Decimal(6)}))


def test_prophetx_owner_approved_two_percent_winnings_assumption_is_not_exchange_verified():
    now = utcnow().replace(microsecond=0)
    config, scope = approved_config("prophetx")
    market, _ = source_fixture(config, now, "assumed-fee-fixture", now+timedelta(days=1))
    catalog = ProphetXMarketData()
    catalog.replace_catalog([{"data": {"123": [{"id": 219, "name": "Moneyline",
        "type": "moneyline", "sub_type": "moneyline", "period": "full_game",
        "status": "active", "selections": [[{"strike_id": "atl-strike", "outcome_id": 4,
        "name": "Atlanta Hawks", "status": "active"}],
        [{"strike_id": "cha-strike", "outcome_id": 5,
        "name": "Charlotte Hornets", "status": "active"}]]}]}}], ["123"], observed_at=now)
    events = {"123": {"event_id": 123, "name": market.event_name,
        "scheduled": market.start_time.isoformat(), "status": "not_started", "tournament_id": 132}}
    matches = prophetx_matches(market, catalog.catalog, events, observed_at=now)
    rule = ApprovedNativeFamilyRule(scope=scope, rule_version="OFFLINE-px-assumed-v1",
        approved_by="fixture", approval_evidence="OFFLINE explicit owner fee assumption only",
        approved_at=now-timedelta(minutes=1), expires_at=now+timedelta(days=2),
        allowed_native_rule_texts=("moneyline|moneyline|full_game",),
        max_start_delta_seconds=0, quantity_step=Decimal(".01"),
        minimum_quantity=Decimal(1), minimum_order_cost_usd=Decimal(1),
        cash_quantum_usd=Decimal(".01"), order_minimum_source="OFFLINE native order-grid fixture",
        prophetx_fee_evidence_kind="owner_approved_assumption",
        prophetx_assumed_winnings_profit_rate=Decimal(".02"),
        prophetx_fee_source="OWNER ASSUMPTION: 2% of net winning profit; not exchange verified",
        prophetx_native_settlement_evidence="OFFLINE reviewed game-winner settlement fixture",
        prophetx_source_timestamp_unit="nanoseconds",
        prophetx_timestamp_unit_evidence="OFFLINE confirmed v2 clock unit")
    profile = SimpleNamespace(venue="prophetx", market_ids=(), family_rule=rule)
    runtime = SimpleNamespace(profile_for=lambda venue: profile, mapper=NativeBookMapper(),
        engine=SimpleNamespace(settings=SimpleNamespace(pricing=config), markets={market.id: market}))
    monitor = SimpleNamespace(research_catalog_at=now,
        client=SimpleNamespace(public_market_data=catalog), research_events=events)
    assert NativeRuntime.reconcile_family_catalog(runtime, "prophetx", market, matches, monitor) == 2
    fee = runtime.mapper.mappings[0].fee_bound
    instrument = runtime.mapper.mappings[0].instrument
    assert not fee.verified and fee.usable_for_execution
    assert fee.evidence_kind == "owner_approved_assumption"
    assert fee.maximum_fee(instrument, Decimal(1), now, Decimal(150)) == Decimal(".03")
    assert fee.maximum_fee(instrument, Decimal(1), now, Decimal(-200)) == Decimal(".01")
    assert fee.maximum_upfront_fee(instrument, Decimal(1), now, Decimal(150)) == 0
    market, quotes = source_fixture(config, now, "assumed-pricing", now+timedelta(days=1))
    live_config = native_live_pricing(market, quotes, config, now)
    fair = fair_value(market, quotes, live_config, now).model_copy(update={
        "probabilities": {TEAMS[0]: Decimal(".55"), TEAMS[1]: Decimal(".45")},
        "sensitivity_probabilities": {TEAMS[0]: Decimal(".55"), TEAMS[1]: Decimal(".45")}})
    priced_instrument = instrument.model_copy(update={"canonical_market_id": market.id,
        "outcome": TEAMS[0], "settlement_rules_fingerprint": market.identity()})
    priced_fee = fee.model_copy(update={"instrument_fingerprint": instrument_fingerprint(priced_instrument)})
    book = NativeBook(instrument=priced_instrument, market_fingerprint=market.identity(),
        asks=(NativeLevel(price=Decimal(150), quantity=Decimal(10)),), fee_bound=priced_fee,
        source_time=now-timedelta(seconds=1), received_at=now, revision=1,
        synchronized=True, synthetic=False)
    candidate = native_opportunity(market, fair, book, live_config,
        max_stake_usd=Decimal(1), mode="sandbox", now=now)
    assert candidate.fair_probability == Decimal(".54")  # 1% payout uncertainty
    assert candidate.intent.max_fee_usd == Decimal(".03")
    assert candidate.fee_evidence_kind == "owner_approved_assumption"
    assert candidate.expected_profit_usd == (Decimal(".54") *
        (candidate.intent.minimum_gross_payout_usd - candidate.intent.max_fee_usd) - 1)
    sized = native_opportunity(market, fair, book, live_config,
        max_stake_usd=Decimal(25), stake_sizing="quarter_kelly",
        available_cash_usd=Decimal(100), mode="sandbox", now=now)
    assert sized.sizing.full_kelly_fraction > 0
    assert sized.intent.quantity <= sized.sizing.budget_usd
    ready_facts = MarketReadiness(market_id=market.id, identity_verified=True,
        provider_mapping_verified=True, venue_mapping_verified=True,
        fee_schedule_verified=False, fee_assumption_approved=True,
        native_units_supported=True, order_minimum_verified=True,
        usable_reference_books=3, required_books_present=True,
        oldest_reference_age_ms=1000, orderbook_age_ms=1000,
        orderbook_synchronized=True, market_active=True,
        executable_liquidity=True, synthetic=False)
    ready_scope = SimpleNamespace(min_reference_books=3, max_reference_age_ms=30000,
        max_orderbook_age_ms=2500)
    assert family_market_ready(ready_facts, ready_scope)
    assert not family_market_ready(ready_facts.model_copy(update={
        "fee_assumption_approved": False}), ready_scope)
    with pytest.raises(NoSignal, match="executable American price"):
        fee.maximum_fee(instrument, Decimal(1), now)
    with pytest.raises(ValueError):
        ApprovedNativeFamilyRule.model_validate({**rule.model_dump(),
            "prophetx_assumed_winnings_profit_rate": Decimal(".01")})


def test_prophetx_fee_excluded_edge_is_explicit_and_keeps_fee_reservation():
    now = utcnow().replace(microsecond=0)
    config, _ = approved_config("prophetx")
    market, quotes = source_fixture(config, now, "fee-choice-fixture", now+timedelta(days=1))
    live_config = native_live_pricing(market, quotes, config, now)
    fair = fair_value(market, quotes, live_config, now)
    probabilities = {TEAMS[0]: Decimal(".53"), TEAMS[1]: Decimal(".47")}
    fair = fair.model_copy(update={"probabilities": probabilities,
        "sensitivity_probabilities": probabilities})
    instrument = NativeInstrument(venue="prophetx", instrument_id="OFFLINE-strike",
        canonical_market_id=market.id, outcome=TEAMS[0], native_outcome="4",
        quantity_unit="usd_stake", price_format="american", quantity_step=Decimal(".01"),
        minimum_quantity=Decimal(1), minimum_order_cost_usd=Decimal(1),
        order_minimum_verified=True, order_minimum_source="OFFLINE verified minimum fixture",
        cash_quantum_usd=Decimal(".01"), settlement_rules_fingerprint=market.identity(),
        verified=True, source="OFFLINE reviewed strike fixture")
    fee = NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
        per_unit_usd=Decimal(".02"), per_fill_usd=Decimal(0), fixed_order_usd=Decimal(0),
        fee_payment_timing="winnings_only", verified=True, source="OFFLINE fee bound fixture",
        observed_at=now-timedelta(seconds=1), valid_until=now+timedelta(minutes=1))
    book = NativeBook(instrument=instrument, market_fingerprint=market.identity(),
        asks=(NativeLevel(price=Decimal(100), quantity=Decimal(10)),), fee_bound=fee,
        source_time=now-timedelta(seconds=1), received_at=now, revision=1,
        synchronized=True, synthetic=False)
    after_fees = native_opportunity(market, fair, book, live_config, max_stake_usd=Decimal(1),
        mode="sandbox", now=now)
    assert not after_fees.qualified and after_fees.fee_estimate_mode == "after_fees"
    policy = config.trusted_pilot_policy.model_copy(update={"fee_estimate_mode": "exclude_fees"})
    pre_fee_config = config.model_copy(update={"trusted_pilot_policy": policy})
    pre_fee_live = native_live_pricing(market, quotes, pre_fee_config, now)
    pre_fee_fair = fair_value(market, quotes, pre_fee_live, now).model_copy(update={
        "probabilities": probabilities, "sensitivity_probabilities": probabilities})
    before_fees = native_opportunity(market, pre_fee_fair, book, pre_fee_live,
        max_stake_usd=Decimal(1), mode="sandbox", now=now)
    assert before_fees.qualified
    assert before_fees.expected_profit_usd == Decimal(".04")
    assert before_fees.fee_estimate_mode == "exclude_fees"
    assert "before fees" in before_fees.estimate_label
    assert before_fees.intent.max_fee_usd == Decimal(".02")
    assert before_fees.intent.reserved_cost_usd == Decimal(1)
    sized = native_opportunity(market, pre_fee_fair, book, pre_fee_live, max_stake_usd=Decimal(25),
        stake_sizing="quarter_kelly", available_cash_usd=Decimal(500), mode="sandbox", now=now)
    assert sized.sizing.full_kelly_fraction == Decimal(".04")
    assert sized.intent.quantity <= sized.sizing.budget_usd
    assert sized.intent.max_fee_usd > 0
    with pytest.raises(NoSignal, match="Unverified or incompatible native fee bound"):
        native_opportunity(market, pre_fee_fair, book.model_copy(update={"fee_bound": fee.model_copy(
            update={"verified": False})}), pre_fee_live, max_stake_usd=Decimal(1),
            mode="sandbox", now=now)


def test_fee_exclusion_cannot_be_enabled_without_approved_prophetx_scope():
    config, _ = approved_config("prophetx")
    raw = config.model_dump(mode="json")
    raw["trusted_pilot_policy"]["fee_estimate_mode"] = "exclude_fees"
    raw["trusted_pilot_policy"]["owner_approved"] = False
    with pytest.raises(ValueError, match="approved ProphetX family scope"):
        PricingConfig.model_validate(raw)
    raw["trusted_pilot_policy"]["owner_approved"] = True
    raw["trusted_pilot_policy"]["market_scope"]["venue"] = "kalshi"
    with pytest.raises(ValueError, match="approved ProphetX family scope"):
        PricingConfig.model_validate(raw)


def test_live_consensus_requires_reviewed_weights_and_source_approval():
    config, _ = approved_config()
    payload = config.model_dump()
    payload["trusted_pilot_policy"]["reference_weights_percent"] = None
    with pytest.raises(ValueError, match="approved recurring source policy"):
        PricingConfig.model_validate(payload)
    payload = config.model_dump()
    payload["trusted_pilot_policy"]["owner_approved"] = False
    with pytest.raises(ValueError):
        PricingConfig.model_validate(payload)
    now = utcnow()
    market, quotes = source_fixture(config, now, "fixture-other-policy", now+timedelta(days=1))
    unrelated = market.model_copy(update={"league": "NFL"})
    assert native_live_pricing(unrelated, quotes, config, now) is config


@pytest.mark.asyncio
async def test_qualified_new_game_reaches_existing_oms_handoff_without_per_game_file(tmp_path, monkeypatch):
    now = utcnow().replace(microsecond=0)
    config, _ = approved_config()
    market, quotes = source_fixture(config, now, "fixture-oms", now+timedelta(days=1))
    instrument = NativeInstrument(
        venue="kalshi", instrument_id="KXNBAGAME-OFFLINE-CHA", exchange_index=3,
        canonical_market_id=market.id, outcome=TEAMS[0], native_outcome="yes",
        quantity_unit="contracts", price_format="probability", payout_per_unit_usd="1",
        quantity_step="1", minimum_quantity="1", minimum_order_cost_usd="0",
        order_minimum_verified=True, order_minimum_source="OFFLINE verified native grid fixture",
        cash_quantum_usd=".01", price_bands=(NativePriceBand(
            minimum=".01", maximum=".99", step=".01"),),
        settlement_rules_fingerprint=market.identity(), verified=True, source="OFFLINE family mapping")
    fee = NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
        per_unit_usd=".01", per_fill_usd="0", fixed_order_usd="0", verified=True,
        source="OFFLINE fee fixture", observed_at=now-timedelta(seconds=1),
        valid_until=now+timedelta(seconds=60))
    book = NativeBook(instrument=instrument, market_fingerprint=market.identity(),
        asks=(NativeLevel(price=".20", quantity="100"),), fee_bound=fee,
        source_time=now, received_at=now, revision=1, synchronized=True)
    live = native_live_pricing(market, quotes, config, now)
    fair = fair_value(market, quotes, live, now)
    candidate = native_opportunity(market, fair, book, live, max_stake_usd=None,
        mode="production", stake_sizing="quarter_kelly", available_cash_usd=Decimal(100),
        now=now)
    assert candidate.qualified and candidate.execution_eligible
    assert candidate.ev_per_usd_payout >= Decimal(".015")
    assert candidate.intent.reserved_cost_usd <= Decimal(25)
    engine = Engine(Settings(_env_file=None, mode="production", live_enabled=True,
        auto_trade=False, sample_feed_enabled=False,
        database_path=str(tmp_path / "offline-family.db"), pricing=config))
    engine.markets[market.id] = market
    engine.risk.auto_trade = True  # Offline handoff only; no actual adapter exists.
    native = engine.native
    native.profile = SimpleNamespace(venue="kalshi", account_fingerprint="a"*64)
    native.books[(market.id, "kalshi", TEAMS[0])] = book
    native.manager.execute = AsyncMock(return_value="OFFLINE OMS handoff")
    monkeypatch.setattr("evplatform.core.native_runtime.utcnow", lambda: now)
    assert await native.execute(candidate) == "OFFLINE OMS handoff"
    submitted = native.manager.execute.await_args.args[0]
    assert submitted.market_id == market.id
    assert submitted.intent.instrument.instrument_id == instrument.instrument_id
    assert submitted.intent.quantity == candidate.intent.quantity
    assert not engine.risk.native_orders


def test_expired_family_rule_and_unaffordable_kelly_do_not_onboard_or_trade():
    now = utcnow().replace(microsecond=0)
    config, scope = approved_config()
    market, quotes = source_fixture(config, now, "fixture-expired", now+timedelta(days=1))
    rows = native_rows(market.start_time, "fixture-expired")
    matches = kalshi_moneyline_matches(market, rows, observed_at=now)
    rule = family_rule(scope, now).model_copy(update={"expires_at": now-timedelta(seconds=1)})
    runtime = SimpleNamespace(profile_for=lambda venue: SimpleNamespace(family_rule=rule),
        mapper=NativeBookMapper(),
        engine=SimpleNamespace(settings=SimpleNamespace(pricing=config)))
    monitor = SimpleNamespace(research_catalog_at=now, research_catalog_rows=rows,
        research_fee_metadata={rows[0]["event_ticker"]: {"observed_at": now}})
    assert NativeRuntime.onboard_family_matches(runtime, "kalshi", market, matches, monitor, now=now) == 0
    assert not runtime.mapper.mappings
    with pytest.raises(NoSignal):
        native_live_pricing(market, quotes[:2], config, now)


def test_family_scope_digest_stays_fixed_across_new_fixture_and_checks_live_facts():
    now = utcnow().replace(microsecond=0)
    config, scope = approved_config()
    rule = family_rule(scope, now)
    limits = LiveLimits(max_order_stake_usd=Decimal(25), max_event_exposure_usd=Decimal(25),
        max_player_exposure_usd=Decimal(25), max_venue_exposure_usd=Decimal(25),
        max_correlated_exposure_usd=Decimal(25), max_total_exposure_usd=Decimal(25),
        daily_loss_limit_usd=Decimal(25), available_cash_only=True,
        block_overlapping_events=True, block_overlapping_players=True,
        stake_sizing="quarter_kelly", max_open_orders=1, max_orders_per_minute=1,
        max_native_daily_debit_usd=Decimal(25))
    profile = LaunchProfile(venue="kalshi", environment="production",
        account_fingerprint="a"*64, build_id="OFFLINE-build", family_rule=rule,
        strategy_ids=("native_positive_ev",), provider_ids=("parlay_rest",),
        mapping_digest=NativeBookMapper().mapping_digest, pricing_digest="b"*64,
        limits=limits, min_reference_books=3, max_reference_age_ms=30000,
        max_orderbook_age_ms=5000,
        settlement_policy="kalshi_ioc_binary_halt_on_exception")
    digest = profile.digest()
    facts = MarketReadiness(market_id="new-fixture-a", identity_verified=True,
        provider_mapping_verified=True, venue_mapping_verified=True,
        fee_schedule_verified=True, native_units_supported=True,
        order_minimum_verified=True, usable_reference_books=3,
        required_books_present=True, oldest_reference_age_ms=1000,
        orderbook_age_ms=500, orderbook_synchronized=True, market_active=True,
        executable_liquidity=True, synthetic=False)
    assert family_market_ready(facts, profile)
    assert family_market_ready(facts.model_copy(update={"market_id": "new-fixture-b"}), profile)
    assert profile.digest() == digest and profile.market_ids == ()
    for change in ({"oldest_reference_age_ms": 31000}, {"executable_liquidity": False},
                   {"identity_verified": False}, {"synthetic": True}):
        assert not family_market_ready(facts.model_copy(update=change), profile)
