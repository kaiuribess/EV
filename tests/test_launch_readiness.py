from datetime import timedelta
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from evplatform.core.models import utcnow
from evplatform.core.readiness import (
    FeedReadiness, LaunchEvidence, LaunchProfile, LiveLimits, MarketReadiness,
    RuntimeReadiness, evaluate_readiness,
)
from evplatform.integrations.normalization import MappingFile, VerifiedOfferMapping
from evplatform.venues.bridge import VerifiedVenueMapping


def script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def limits():
    return LiveLimits(max_order_stake_usd="1", max_event_exposure_usd="2", max_player_exposure_usd="2",
                      max_venue_exposure_usd="3", max_correlated_exposure_usd="2", max_total_exposure_usd="5",
                      daily_loss_limit_usd="2", max_open_orders=2, max_orders_per_minute=2)


def ready_fixture(environment="production"):
    now = utcnow()
    profile = LaunchProfile(venue="test_venue", environment=environment, account_fingerprint="a" * 64,
                            build_id="fixture-build", market_ids=("market-1",), strategy_ids=("positive_ev",),
                            provider_ids=("provider-1",), mapping_digest="b" * 64, pricing_digest="c" * 64,
                            limits=limits(), min_reference_books=2, max_reference_age_ms=5000, max_orderbook_age_ms=2000)
    checks = {
        "native_lifecycle": ("submit_ack", "private_fill", "partial_fill", "cancel_race", "unknown_outcome",
                             "duplicate_prevention", "fees_and_units", "sequence_gap_recovery"),
        "restart_recovery": ("durable_intent_restart", "open_order_reconciliation", "position_reconciliation"),
        "settlement_reconciliation": ("native_result", "payout_and_fees", "position_release", "idempotency"),
        "account_access": (), "risk_authorization": (), "geographic_eligibility": (),
    }
    evidence = tuple(LaunchEvidence(kind=kind, venue=profile.venue, environment=environment,
                                   account_fingerprint=profile.account_fingerprint, profile_digest=profile.digest(),
                                   build_id=profile.build_id, observed_at=now-timedelta(seconds=1),
                                   expires_at=now+timedelta(hours=1), verified_by="fixture reviewer",
                                   source={"risk_authorization": "operator_authorization",
                                           "geographic_eligibility": "official_account_verification"}.get(kind, "authenticated_native"),
                                   artifact_sha256="d" * 64, passed=True, synthetic=False, checks=items)
                     for kind, items in checks.items())
    profile = profile.model_copy(update={"evidence": evidence})
    runtime = RuntimeReadiness(
        environment=environment, venue=profile.venue, account_fingerprint=profile.account_fingerprint,
        build_id=profile.build_id, mapping_digest=profile.mapping_digest, pricing_digest=profile.pricing_digest,
        checked_at=now, configured_limits=profile.limits, enabled_strategy_ids=profile.strategy_ids,
        central_risk_enforced=True, opposing_position_guards=True, durable_intents=True, persistence_healthy=True,
        signal_ledger_healthy=True, native_routing_supported=True, private_stream_authenticated=True,
        private_stream_synchronized=True, reconciliation_healthy=True, startup_recovery_complete=True,
        cancellation_races_supported=True, settlement_reconciliation_supported=True,
        native_credentials_present=True, account_identity_verified=True,
        feeds=(FeedReadiness(provider_id="provider-1", enabled=True, authenticated=True, connected=True,
                             entitlement_confirmed=True, quota_available=True, circuit_open=False,
                             transport="websocket", synchronization_ready=True),),
        markets=(MarketReadiness(market_id="market-1", identity_verified=True, provider_mapping_verified=True,
                                 venue_mapping_verified=True, fee_schedule_verified=True, native_units_supported=True,
                                 usable_reference_books=2, required_books_present=True, oldest_reference_age_ms=100,
                                 orderbook_age_ms=100, orderbook_synchronized=True, market_active=True,
                                 executable_liquidity=True, synthetic=False),),
        live_enabled=True, auto_trade=True, kill_switch=False,
    )
    return profile, runtime, now


def codes(report):
    return {item.code for item in report.blockers}


def test_empty_readiness_fails_closed():
    report = evaluate_readiness(None, RuntimeReadiness())
    assert not report.safe_to_arm and not report.live_trading_ready
    assert "profile.missing" in codes(report)
    assert "runtime.native_routing_supported" in codes(report)
    assert report.performance.conclusion == "not_established"


def test_complete_scoped_evidence_and_facts_can_pass_without_profit_claim():
    profile, runtime, now = ready_fixture()
    report = evaluate_readiness(profile, runtime, now=now)
    assert report.safe_to_arm and report.live_trading_ready
    assert not report.blockers
    assert report.performance.conclusion == "not_established"


def test_sandbox_is_not_live_and_arm_state_is_separate():
    profile, runtime, now = ready_fixture("sandbox")
    report = evaluate_readiness(profile, runtime, now=now)
    assert report.sandbox_trading_ready and not report.live_trading_ready
    report = evaluate_readiness(profile, runtime.model_copy(update={"auto_trade": False, "kill_switch": True}), now=now)
    assert report.safe_to_arm and not report.sandbox_trading_ready
    assert {b.category for b in report.blockers} == {"arming"}


@pytest.mark.parametrize("field", ["central_risk_enforced", "opposing_position_guards", "durable_intents",
                                   "persistence_healthy", "signal_ledger_healthy", "native_routing_supported",
                                   "private_stream_authenticated", "private_stream_synchronized", "reconciliation_healthy",
                                   "startup_recovery_complete", "cancellation_races_supported",
                                   "settlement_reconciliation_supported", "account_identity_verified"])
def test_every_runtime_guard_is_mandatory(field):
    profile, runtime, now = ready_fixture()
    report = evaluate_readiness(profile, runtime.model_copy(update={field: False}), now=now)
    assert not report.safe_to_arm and f"runtime.{field}" in codes(report)


@pytest.mark.parametrize("change", [
    {"expires_at": utcnow()-timedelta(seconds=5)}, {"synthetic": True}, {"passed": False},
    {"account_fingerprint": "f"*64}, {"profile_digest": "f"*64}, {"build_id": "old-build"},
    {"environment": "sandbox"}, {"source": "operator_authorization"}, {"checks": ()},
])
def test_untrustworthy_lifecycle_evidence_blocks(change):
    profile, runtime, now = ready_fixture()
    evidence = tuple(e.model_copy(update=change) if e.kind == "native_lifecycle" else e for e in profile.evidence)
    report = evaluate_readiness(profile.model_copy(update={"evidence": evidence}), runtime, now=now)
    assert not report.safe_to_arm and "evidence.native_lifecycle" in codes(report)


def test_new_failed_check_revokes_older_pass():
    profile, runtime, now = ready_fixture()
    previous = next(e for e in profile.evidence if e.kind == "account_access")
    revoked = previous.model_copy(update={"observed_at": now, "passed": False})
    profile = profile.model_copy(update={"evidence": (*profile.evidence, revoked)})
    assert "evidence.account_access" in codes(evaluate_readiness(profile, runtime, now=now))


def test_limits_or_mapping_change_invalidates_certificate():
    profile, runtime, now = ready_fixture()
    profile = profile.model_copy(update={"mapping_digest": "f"*64})
    report = evaluate_readiness(profile, runtime, now=now)
    assert "profile.mapping_digest_mismatch" in codes(report)
    assert "evidence.native_lifecycle" in codes(report)
    assert "evidence.risk_authorization" in codes(report)


def test_fresh_heartbeat_does_not_make_quotes_fresh():
    profile, runtime, now = ready_fixture()
    market = runtime.markets[0].model_copy(update={"oldest_reference_age_ms": None, "orderbook_age_ms": 5001})
    report = evaluate_readiness(profile, runtime.model_copy(update={"markets": (market,)}), now=now)
    assert "market.oldest_reference_age_ms" in codes(report) and "market.orderbook_age_ms" in codes(report)


def test_elapsed_snapshot_age_added_to_quote_age():
    profile, runtime, now = ready_fixture()
    report = evaluate_readiness(profile, runtime, now=now+timedelta(seconds=3))
    assert "market.orderbook_age_ms" in codes(report)


def test_provider_quota_entitlement_and_sequence_gap_fail_closed():
    profile, runtime, now = ready_fixture()
    feed = runtime.feeds[0].model_copy(update={"quota_available": False, "entitlement_confirmed": False,
                                             "synchronization_ready": False})
    report = evaluate_readiness(profile, runtime.model_copy(update={"feeds": (feed,)}), now=now)
    assert {"feed.quota_available", "feed.entitlement_confirmed", "feed.synchronization_ready"} <= codes(report)


def test_unknown_orders_stale_runtime_and_synthetic_markets_block():
    profile, runtime, now = ready_fixture()
    runtime = runtime.model_copy(update={"unresolved_unknown_orders": 1, "checked_at": now-timedelta(seconds=6),
                                         "markets": (runtime.markets[0].model_copy(update={"synthetic": True}),)})
    report = evaluate_readiness(profile, runtime, now=now)
    assert {"runtime.unresolved_orders", "runtime.stale", "market.synthetic"} <= codes(report)


def test_explicit_sandbox_account_certificate_transfer_requires_exact_binding():
    profile, runtime, now = ready_fixture()
    profile = profile.model_copy(update={"sandbox_account_fingerprint": "e"*64})
    evidence = tuple(e.model_copy(update={"profile_digest": profile.digest(),
                                          **({"environment": "sandbox", "account_fingerprint": "e"*64}
                                             if e.kind == "native_lifecycle" else {})}) for e in profile.evidence)
    profile = profile.model_copy(update={"evidence": evidence})
    assert evaluate_readiness(profile, runtime, now=now).safe_to_arm
    evidence = tuple(e.model_copy(update={"account_fingerprint": "a"*64}) if e.kind == "native_lifecycle" else e for e in evidence)
    assert not evaluate_readiness(profile.model_copy(update={"evidence": evidence}), runtime, now=now).safe_to_arm


def test_limits_have_no_implicit_money_authorization():
    with pytest.raises(ValidationError):
        LiveLimits()
    with pytest.raises(ValidationError):
        LiveLimits.model_validate({**limits().model_dump(), "max_order_stake_usd": "NaN"})
    with pytest.raises(ValidationError):
        LiveLimits.model_validate({**limits().model_dump(), "max_order_stake_usd": "9"})


def mapping_fixture(market):
    provider = MappingFile(mappings=[VerifiedOfferMapping(signature=char*64, market=market,
                         outcome_map={outcome: outcome for outcome in market.outcomes}, bookmaker=book,
                         verified=True, evidence="fixture external rule review")
                        for char, book in (("a", "pinnacle"), ("b", "circa"))])
    native = VerifiedVenueMapping(canonical_market_id=market.id, canonical_outcome=market.outcomes[0],
                                native_instrument_id="test-native", native_outcome="yes",
                                settlement_fingerprint=market.identity(), mapping_evidence="fixture only",
                                verified=True, fee_schedule_verified=True, native_payout_usd=Decimal(1),
                                native_quantity_unit="contracts")
    return provider, native


def test_mapping_validator_exact_line_and_rules(market):
    module = script("validate_market_mappings")
    provider, native = mapping_fixture(market)
    bundle = module.VenueMappingFile(venue="kalshi", mappings=[native])
    assert module.validate_mappings([provider], [bundle])["valid"]
    bad = native.model_copy(update={"settlement_fingerprint": "wrong"})
    report = module.validate_mappings([provider], [bundle.model_copy(update={"mappings": [bad]})])
    assert not report["valid"] and any(i["code"] == "venue.fingerprint_mismatch" for i in report["issues"])


def test_mapping_validator_duplicate_book_is_not_independent(market):
    module = script("validate_market_mappings")
    provider, native = mapping_fixture(market)
    provider.mappings[1] = provider.mappings[1].model_copy(update={"bookmaker": "Pinnacle"})
    report = module.validate_mappings([provider], [module.VenueMappingFile(venue="kalshi", mappings=[native])])
    assert any(i["code"] == "provider.insufficient_independent_books" for i in report["issues"])


def test_mapping_validator_rejects_canonical_collision_and_cent_units(market):
    module = script("validate_market_mappings")
    provider, native = mapping_fixture(market)
    different = market.model_copy(update={"line": Decimal("1.5")})
    provider.mappings[1] = provider.mappings[1].model_copy(update={"market": different})
    report = module.validate_mappings([provider], [module.VenueMappingFile(venue="novig", mappings=[native])])
    found = {i["code"] for i in report["issues"]}
    assert {"canonical.identity_collision", "venue.legacy_bridge_units_unsupported"} <= found


def test_readiness_cli_errors_do_not_echo_rejected_secrets(tmp_path, capsys):
    module = script("check_live_readiness")
    path = tmp_path / "mistaken-secret.json"
    secret = "must-never-appear-in-diagnostics"
    path.write_text(json.dumps({"venue": secret}), encoding="utf-8")
    assert module.main(["--profile", str(path)]) == 2
    assert secret not in capsys.readouterr().out


def test_offline_cli_fails_closed_without_profile(capsys):
    module = script("check_live_readiness")
    assert module.main([]) == 1
    assert json.loads(capsys.readouterr().out)["live_trading_ready"] is False
