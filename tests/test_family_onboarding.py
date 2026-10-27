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
async def test_qualified_new_game_reaches_existing_oms_handoff_without_per_game_file(tmp_path):
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
