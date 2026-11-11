"""The owner Live switch renews scope, never bypasses account evidence or OMS."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from evplatform.core.config import PricingConfig
from evplatform.core.readiness import LaunchProfile, LiveLimits, digest_json
from evplatform.core.recurring_switch import issue_prophetx_authorization
from evplatform.venues.account_probe import NativeAccountSnapshot
from tests.test_family_onboarding import approved_config
from tests.test_watch_native_recurring import prophetx_rule


def fixture_engine(tmp_path: Path, *, age_seconds: int = 2, reconciled: bool = True):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    config, scope = approved_config("prophetx")
    policy = config.trusted_pilot_policy.model_copy(update={
        "provider_id": "parlay_game_ws",
        "trusted_books": ["fanduel", "draftkings", "caesars", "bet365"],
        "reference_weights_percent": {book: Decimal(25) for book in
            ("fanduel", "draftkings", "caesars", "bet365")},
        "max_debit_usd": Decimal(5), "max_loss_usd": Decimal(5),
    })
    config = PricingConfig.model_validate({**config.model_dump(mode="json"),
        "trusted_pilot_policy": policy.model_dump(mode="json"), "min_ev": "0.015"})
    limits = LiveLimits(max_order_stake_usd=Decimal(5),
        max_event_exposure_usd=Decimal(20), max_player_exposure_usd=Decimal(20),
        max_venue_exposure_usd=Decimal(20), max_correlated_exposure_usd=Decimal(20),
        max_total_exposure_usd=Decimal(20), daily_loss_limit_usd=Decimal(20),
        max_native_daily_debit_usd=Decimal(20), cash_policy="prophetx_exchange_rejection",
        available_cash_only=False, stake_sizing="capped", max_open_orders=1,
        max_orders_per_minute=1, unit_size_usd=Decimal(5),
        block_overlapping_events=True, block_overlapping_players=True,
        external_exposure_scope="profile_account")
    profile = LaunchProfile(venue="prophetx", environment="production",
        account_fingerprint="b" * 64, build_id="old-build",
        family_rule=prophetx_rule(scope), strategy_ids=("native_positive_ev",),
        provider_ids=("parlay_game_ws",), mapping_digest="a" * 64,
        pricing_digest=digest_json(config.model_dump(mode="json")), limits=limits,
        min_reference_books=3, max_reference_age_ms=30000,
        max_orderbook_age_ms=30000,
        settlement_policy="prophetx_funded_cash_win_loss_halt_on_exception",
        owner_approved_recurring_lifecycle_deferral=True)
    snapshot = NativeAccountSnapshot(venue="prophetx", environment="production",
        account_fingerprint="b" * 64, identity_verified=True,
        observed_at=now-timedelta(seconds=age_seconds), balance_usd=Decimal("49.10"),
        cash_available_verified=True, open_order_count=0, position_count=0,
        reconciled=reconciled, trading_permission_verified=True)
    native = SimpleNamespace(profile=profile, portfolio=None, build_id="new-build",
        mapper=SimpleNamespace(mapping_digest="a" * 64),
        account_snapshots={"prophetx": snapshot}, account_probe_errors={},
        manager=SimpleNamespace(invalidated_settlements=set()))
    native.profile_for = lambda venue=None: native.profile if venue in (None, "prophetx") else None
    native.profiles_by_venue = lambda: {"prophetx": native.profile}
    native.report = lambda venue=None: SimpleNamespace(profile_digest=native.profile.digest())
    risk = SimpleNamespace(native_orders={}, kill_reason="",
        external_exposure_bounded=lambda venue: True)
    engine = SimpleNamespace(settings=SimpleNamespace(mode="production", pricing=config,
        data_dir=tmp_path), native=native, risk=risk)
    return engine, now


def test_owner_switch_issues_24_hour_scope_and_real_account_evidence(tmp_path):
    engine, now = fixture_engine(tmp_path)
    engine.risk.kill_reason = "Persisted emergency stop; reset explicitly"
    old_digest = engine.native.report().profile_digest
    auth_path = tmp_path / "authorization.json"
    result = issue_prophetx_authorization(engine, old_digest, now=now,
        authorization_path=auth_path)
    assert result["venues"] == ["prophetx"]
    assert result["maximum_native_attempts"] is None
    assert datetime.fromisoformat(result["expires_at"]) == now + timedelta(hours=24)
    assert result["launch_scope_digest"] == engine.native.profile.digest()
    assert engine.native.profile.build_id == "new-build"
    assert set(engine.native.profile.family_rule.scope.additional_player_prop_statistics) >= {
        "points", "rebounds", "assists", "threes", "steals", "blocks", "turnovers",
        "points_rebounds", "points_assists", "rebounds_assists",
        "points_rebounds_assists", "double_double", "triple_double"}
    assert engine.native.profile.family_rule.scope.additional_competitions == (
        ("football", "NFL"), ("football", "NCAAF"),
        ("basketball", "NCAABM"), ("tennis", "ATP"), ("tennis", "WTA"))
    assert set(engine.native.profile.family_rule.scope.additional_nfl_player_prop_statistics) >= {
        "passing_yards", "passing_touchdowns", "pass_completions", "pass_attempts",
        "rushing_yards", "rushing_attempts", "receiving_yards", "receptions",
        "kicking_points", "field_goals_made", "anytime_touchdown", "first_touchdown"}
    assert "NFL|moneyline|moneyline|default_full_game" in (
        engine.native.profile.family_rule.allowed_native_rule_texts)
    assert {"NFL", "NCAAF", "NCAABM", "ATP", "WTA"} <= {
        token.split("|", 1)[0] for token in
        engine.native.profile.family_rule.allowed_native_rule_texts if "|moneyline|" in token}
    assert set(engine.native.profile.family_rule.additional_native_settlement_evidence) == {
        "football:NFL", "football:NCAAF", "basketball:NCAABM",
        "tennis:ATP", "tennis:WTA"}
    assert engine.native.profile.provider_ids == ("parlay_game_ws", "parlay_ws")
    assert engine.settings.pricing.trusted_pilot_policy.player_prop_provider_id == "parlay_ws"
    assert engine.settings.pricing.trusted_pilot_policy.market_scope == engine.native.profile.family_rule.scope
    assert [item.kind for item in engine.native.profile.evidence] == [
        "account_access", "risk_authorization"]
    assert engine.native.profile.evidence[0].observed_at == engine.native.account_snapshots["prophetx"].observed_at
    assert engine.native.profile.evidence[1].source == "operator_authorization"
    assert (tmp_path / "launch_profile.json").exists()
    assert auth_path.exists()
    assert len(list((tmp_path / "recurring_switch_audit").glob("*.json"))) == 3


def test_owner_switch_can_reset_recovered_audit_persistence_halt(tmp_path):
    engine, now = fixture_engine(tmp_path)
    engine.risk.kill_reason = "Audit persistence unavailable"
    engine.native.runtime_facts = lambda venue: SimpleNamespace(
        persistence_healthy=True, durable_intents=True, signal_ledger_healthy=True)

    result = issue_prophetx_authorization(engine,
        engine.native.report().profile_digest, now=now,
        authorization_path=tmp_path / "authorization.json")

    assert result["venues"] == ["prophetx"]
    assert (tmp_path / "authorization.json").exists()


def test_owner_switch_rejects_unrecovered_audit_persistence_halt(tmp_path):
    engine, now = fixture_engine(tmp_path)
    engine.risk.kill_reason = "Audit persistence unavailable"
    engine.native.runtime_facts = lambda venue: SimpleNamespace(
        persistence_healthy=False, durable_intents=True, signal_ledger_healthy=True)

    with pytest.raises(ValueError, match="inventory or settlement"):
        issue_prophetx_authorization(engine,
            engine.native.report().profile_digest, now=now,
            authorization_path=tmp_path / "authorization.json")

    assert not (tmp_path / "authorization.json").exists()


@pytest.mark.parametrize("change", ["stale", "inventory", "scope", "pricing", "emergency", "unknown_order"])
def test_owner_switch_rejects_unverified_facts_without_writing_authorization(tmp_path, change):
    engine, now = fixture_engine(tmp_path,
        age_seconds=601 if change == "stale" else 2,
        reconciled=change != "inventory")
    digest = engine.native.report().profile_digest
    if change == "scope":
        digest = "f" * 64
    if change == "pricing":
        engine.native.profile = engine.native.profile.model_copy(update={
            "pricing_digest": "e" * 64})
        digest = engine.native.report().profile_digest
    if change == "emergency":
        engine.risk.kill_reason = "Unknown native order outcome"
    if change == "unknown_order":
        engine.risk.native_orders["offline"] = SimpleNamespace(
            venue="prophetx", reconciliation_complete=False)
    with pytest.raises(ValueError):
        issue_prophetx_authorization(engine, digest, now=now,
            authorization_path=tmp_path / "authorization.json")
    assert not (tmp_path / "authorization.json").exists()
