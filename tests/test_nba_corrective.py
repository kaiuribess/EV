"""Recorded real identities are useful fixtures, never current live quotes."""
from __future__ import annotations

import json
import subprocess
import sys
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from evplatform.core.config import PricingConfig, Settings
from evplatform.core.paths import PROJECT_ROOT, runtime_data_file
from evplatform.core.plugins import PluginContext
from evplatform.core.readiness import LaunchProfile
from evplatform.core.reference_profiles import ReferenceFreshnessRule, quote_eligibility
from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import (
    VerifiedProviderMapper, _parlay_observation, extract_offers,
)
from evplatform.integrations.oddspapi import OddsPapiRest
from evplatform.plugins.external import ProviderBridge
from evplatform.engine import Engine
from scripts import bootstrap_nba_pilot


FIXTURE = PROJECT_ROOT / "tests/fixtures/oddspapi_den_okc_v4_20261022.json"
ROTATED_FIXTURE = PROJECT_ROOT / "tests/fixtures/oddspapi_den_okc_v4_market_rotation_20261022.json"


def recorded_frame():
    row = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ProviderEnvelope("oddspapi_rest", "snapshot", row["payload"],
        datetime.fromisoformat(row["captured_at"]), 0, metadata=row["metadata"])


def test_reviewed_pinnacle_replacement_clears_identity_only():
    row = json.loads(ROTATED_FIXTURE.read_text(encoding="utf-8"))
    frame = ProviderEnvelope("oddspapi_rest", "snapshot", row["payload"],
        datetime.fromisoformat(row["captured_at"]), 0, metadata=row["metadata"])
    _, mappings, structural = bootstrap_nba_pilot.validated_review()
    offer, = extract_offers(frame)
    assert offer.signature == structural.source_signatures["oddspapi_rest/pinnacle/replacement_3775347026"]
    assert offer.context["native_outcomes"] == {"111": "home", "112": "away"}
    mapped = VerifiedProviderMapper(mappings.mappings)(frame)
    assert len(mapped) == 1
    market, quote = mapped[0]
    assert market.id == "nba-20261022-den-okc-normal-winner"
    assert quote.bookmaker == "pinnacle" and quote.status == "active"
    assert quote.source_observed_at is None and not quote.observation_verified
    assert not quote.settlement_equivalence_verified


def test_explicit_oddspapi_fixture_uses_response_team_and_league_names():
    row = json.loads(ROTATED_FIXTURE.read_text(encoding="utf-8"))
    # fixture_ids are polled before discovery, so they have no discovery join.
    metadata = {"market_catalog": row["metadata"]["market_catalog"]}
    frame = ProviderEnvelope("oddspapi_rest", "snapshot", row["payload"],
        datetime.fromisoformat(row["captured_at"]), 0, metadata=metadata)
    _, mappings, _ = bootstrap_nba_pilot.validated_review()
    offers = extract_offers(frame)
    assert len(offers) == 1 and offers[0].signature == mappings.mappings[-1].signature
    mapped = VerifiedProviderMapper(mappings.mappings)(frame)
    assert len(mapped) == 1 and mapped[0][1].bookmaker == "pinnacle"
    assert not mapped[0][1].observation_verified


def test_reviewed_replacement_is_append_only_and_cannot_change_pilot_scope(tmp_path):
    _, mappings, structural = bootstrap_nba_pilot.validated_review()
    source = tmp_path / "verified_mappings.json"
    initial = mappings.model_copy(update={"mappings": mappings.mappings[:-1]})
    bootstrap_nba_pilot._append_reviewed_mappings(source, initial)
    bootstrap_nba_pilot._append_reviewed_mappings(source, mappings)
    assert json.loads(source.read_text(encoding="utf-8"))["mappings"][-1]["signature"] == mappings.mappings[-1].signature
    changed = mappings.model_copy(update={"mappings": [
        mappings.mappings[0].model_copy(update={"evidence": "unreviewed replacement"}), *mappings.mappings[1:]]})
    with pytest.raises(ValueError, match="changed or disappeared"):
        bootstrap_nba_pilot._append_reviewed_mappings(source, changed)
    review_path = tmp_path / "nba_pilot_structural_review.json"
    initial_review = structural.model_copy(update={
        "source_signatures": {key: value for key, value in structural.source_signatures.items()
                              if key != "oddspapi_rest/pinnacle/replacement_3775347026"},
        "review_provenance": structural.review_provenance.split("; Pinnacle native market ID replacement")[0],
    })
    bootstrap_nba_pilot._extend_structural_review(review_path, initial_review)
    bootstrap_nba_pilot._extend_structural_review(review_path, structural)
    assert len(json.loads(review_path.read_text(encoding="utf-8"))["source_signatures"]) == 4
    with pytest.raises(ValueError, match="scope or authorization changed"):
        bootstrap_nba_pilot._extend_structural_review(review_path,
            structural.model_copy(update={"canonical_market_id": "another-market"}))


def test_recorded_real_pair_clears_identity_but_not_quote_or_settlement_gate():
    _, mappings, _ = bootstrap_nba_pilot.validated_review()
    frame = recorded_frame()
    offers = extract_offers(frame)
    assert {offer.signature for offer in offers} == {
        "3189fecc9af376cfc73b633f5d1ae16f1965419b5d6c8cdd184f773586e1fb85",
        "a2e92910e3a4a040668b9bf199c20fa9c525135bca283c30225a64e3a11ae58d"}
    mapper = VerifiedProviderMapper(mappings.mappings)
    mapped = mapper(frame)
    assert len(mapped) == 2 and mapper.mapped_count == 2
    assert all(quote.market_id == "nba-20261022-den-okc-normal-winner" and
        set(quote.decimal_odds) == {"Denver Nuggets", "Oklahoma City Thunder"}
        for _, quote in mapped)
    assert all(quote.source_observed_at is None and not quote.observation_verified
        and not quote.settlement_equivalence_verified for _, quote in mapped)
    now = frame.received_at
    for market, quote in mapped:
        config = PricingConfig(profile_freshness_rules=[ReferenceFreshnessRule(
            sport="basketball", league="NBA", market_type="moneyline",
            provider=quote.provider, bookmaker=quote.bookmaker, ttl_seconds="5",
            verified=True, evidence="Recorded v4 source freshness check")])
        valid, reasons, age = quote_eligibility(config, market, quote, now)
        assert not valid and age is None
        assert any("observation is missing" in reason for reason in reasons)
        assert any("settlement comparison" in reason for reason in reasons)


@pytest.mark.parametrize("change", ["event", "market", "outcome"])
def test_unrelated_or_ambiguous_offer_remains_quarantined(change):
    _, mappings, _ = bootstrap_nba_pilot.validated_review()
    frame = recorded_frame()
    payload = json.loads(json.dumps(frame.payload))
    if change == "event":
        payload["fixtureId"] = "another-real-event"
    elif change == "market":
        payload["bookmakerOdds"]["pinnacle"]["markets"]["111"]["bookmakerMarketId"] = "changed"
    else:
        payload["bookmakerOdds"]["pinnacle"]["markets"]["111"]["outcomes"]["113"] = {
            "players": {"0": {"price": 2.1, "active": True}}}
    altered = ProviderEnvelope(frame.provider_id, frame.kind, payload, frame.received_at,
        frame.received_monotonic_ns, metadata=frame.metadata)
    mapper = VerifiedProviderMapper(mappings.mappings)
    mapped = mapper(altered)
    assert len(mapped) < 2 and mapper.quarantine


def test_five_second_gate_uses_verified_observation_not_old_price_change():
    market = bootstrap_nba_pilot.validated_review()[1].mappings[0].market
    now = datetime(2026, 10, 22, 19, 3, 48, tzinfo=timezone.utc)
    from evplatform.core.models import Quote
    config = PricingConfig(profile_freshness_rules=[ReferenceFreshnessRule(
        sport="basketball", league="NBA", market_type="moneyline",
        provider="documented_snapshot", bookmaker="pinnacle", ttl_seconds="5",
        verified=True, evidence="Exact quote/status re-observed by contract")])
    base = Quote(market_id=market.id, provider="documented_snapshot", bookmaker="pinnacle",
        decimal_odds={market.outcomes[0]: 1.35, market.outcomes[1]: 3.5},
        source_time=now-timedelta(hours=1), source_price_changed_at=now-timedelta(hours=1),
        received_at=now, status="active", market_status_verified=True,
        observation_complete=True, observation_verified=True,
        source_observed_at=now-timedelta(seconds=1))
    assert quote_eligibility(config, market, base, now)[0]
    unknown = base.model_copy(update={"source_observed_at": None, "observation_verified": False})
    assert not quote_eligibility(config, market, unknown, now)[0]
    stale = base.model_copy(update={"source_observed_at": now-timedelta(seconds=6)})
    assert any("expired" in reason for reason in quote_eligibility(config, market, stale, now)[1])
    future = base.model_copy(update={"source_observed_at": now+timedelta(seconds=1)})
    assert any("future" in reason for reason in quote_eligibility(config, market, future, now)[1])
    suspended_side = base.model_copy(update={"status": "suspended", "market_status_verified": False})
    assert not quote_eligibility(config, market, suspended_side, now)[0]


def test_exact_source_rule_never_inherits_other_sport_book_or_market():
    market = bootstrap_nba_pilot.validated_review()[1].mappings[0].market
    now = datetime(2026, 10, 22, 19, 3, 48, tzinfo=timezone.utc)
    # Offline hypothetical contract exercise; no production rule is installed.
    rule = ReferenceFreshnessRule(sport="basketball", league="NBA", market_type="moneyline",
        provider="fixture_contract", bookmaker="pinnacle", ttl_seconds="5", verified=True,
        evidence="OFFLINE exact selection snapshot and status guarantee")
    config = PricingConfig(profile_freshness_rules=[rule])
    from evplatform.core.models import Quote
    base = Quote(market_id=market.id, provider="fixture_contract", bookmaker="pinnacle",
        decimal_odds={market.outcomes[0]: 1.35, market.outcomes[1]: 3.5},
        source_time=now-timedelta(hours=1), source_observed_at=now-timedelta(seconds=1),
        received_at=now, status="active", observation_verified=True,
        observation_complete=True, market_status_verified=True,
        settlement_equivalence_verified=True)
    assert quote_eligibility(config, market, base, now)[0]
    assert not quote_eligibility(config, market, base.model_copy(update={"bookmaker": "fanduel"}), now)[0]
    assert not quote_eligibility(config, market, base.model_copy(update={"provider": "other"}), now)[0]
    assert not quote_eligibility(config, market.model_copy(update={"sport": "football"}), base, now)[0]
    assert not quote_eligibility(config, market.model_copy(update={"market_type": "total"}), base, now)[0]
    assert not quote_eligibility(config, market, base, now+timedelta(seconds=6))[0]
    assert not quote_eligibility(config, market, base.model_copy(update={"observation_complete": False}), now)[0]
    assert not quote_eligibility(config, market, base.model_copy(update={"status": "suspended"}), now)[0]


def test_response_level_scope_does_not_verify_uncovered_parlay_selection():
    now = datetime(2026, 10, 22, 19, 3, 48, tzinfo=timezone.utc)
    frame = ProviderEnvelope("parlay_game_ws", "odds_update", {}, now, 0,
        metadata={"headers": {"x-result-truncated": "true"}})
    row = {"last_observed": now.isoformat(), "last_update_type": "collector_observation"}
    evidence = _parlay_observation(frame, row, complete=True)
    assert evidence["observation_verified"]
    assert not evidence["observation_complete"]
    assert evidence["verification_reason"] == "truncated_response"


def test_receipt_fixture_heartbeat_and_scaled_clock_do_not_refresh_selection():
    frame = recorded_frame()
    offer = extract_offers(frame)[0]
    assert offer.source_price_changed_at < frame.received_at
    assert offer.source_observed_at is None
    recent_receipt = ProviderEnvelope(frame.provider_id, frame.kind, frame.payload,
        frame.received_at + timedelta(hours=1), 0, metadata=frame.metadata)
    assert extract_offers(recent_receipt)[0].source_observed_at is None
    for kind in ("event_discovery", "heartbeat"):
        assert extract_offers(ProviderEnvelope(frame.provider_id, kind, frame.payload,
            frame.received_at + timedelta(hours=1), 0, metadata=frame.metadata)) == []
    from evplatform.integrations.normalization import _oddize_quote_time
    assert _oddize_quote_time({"ts": 1_760_000_000_000}) is None
    assert _oddize_quote_time({"timestamp": None}) is None


def test_documented_parlay_quote_observation_can_follow_an_unchanged_price():
    now = datetime(2026, 10, 22, 19, 3, 48, tzinfo=timezone.utc)
    frame = ProviderEnvelope("parlay_game_ws", "odds_update", {}, now, 0)
    row = {"last_observed": (now-timedelta(seconds=1)).isoformat(),
           "last_update_type": "collector_observation",
           "line_changed_at_ms": int((now-timedelta(hours=1)).timestamp() * 1000)}
    evidence = _parlay_observation(frame, row, complete=True)
    assert evidence["observation_verified"] and evidence["observation_complete"]
    assert evidence["source_observed_at"] == now-timedelta(seconds=1)
    assert evidence["source_price_changed_at"] == now-timedelta(hours=1)
    assert not _parlay_observation(frame, {"last_update": now.isoformat()},
        complete=True)["observation_verified"]


def test_odds_papi_inactive_opposing_side_rejects_complete_pair():
    frame = recorded_frame()
    payload = json.loads(json.dumps(frame.payload))
    payload["bookmakerOdds"]["pinnacle"]["markets"]["111"]["outcomes"]["112"]["players"]["0"]["active"] = False
    changed = ProviderEnvelope(frame.provider_id, frame.kind, payload, frame.received_at,
        frame.received_monotonic_ns, metadata=frame.metadata)
    _, mappings, _ = bootstrap_nba_pilot.validated_review()
    mapped = VerifiedProviderMapper(mappings.mappings)(changed)
    pinnacle = next(quote for _, quote in mapped if quote.bookmaker == "pinnacle")
    assert pinnacle.status == "suspended"
    assert not pinnacle.market_status_verified
    assert not pinnacle.observation_verified


def test_oddize_observed_price_does_not_synthesize_active_market():
    fixture = json.loads((PROJECT_ROOT / "tests/fixtures/oddize_den_okc_20261022.json").read_text())
    frame = ProviderEnvelope("oddize_rest", "snapshot", {"event": fixture["event"]},
        datetime.fromisoformat(fixture["captured_at"]), 0)
    _, mappings, _ = bootstrap_nba_pilot.validated_review()
    matched = VerifiedProviderMapper(mappings.mappings)(frame)
    assert len(matched) == 1
    market, quote = matched[0]
    assert quote.bookmaker == "circa" and quote.status == "unknown"
    assert quote.observation_verified and not quote.market_status_verified
    config = PricingConfig(profile_freshness_rules=[ReferenceFreshnessRule(
        sport="basketball", league="NBA", market_type="moneyline",
        provider="oddize_rest", bookmaker="circa", ttl_seconds="5",
        verified=True, evidence="Observation only; status not supplied")])
    assert not quote_eligibility(config, market, quote, frame.received_at)[0]


def test_mapping_path_is_checkout_relative_from_two_working_directories(tmp_path, monkeypatch):
    expected = PROJECT_ROOT / "data/verified_mappings.json"
    assert runtime_data_file("verified_mappings.json") == expected
    monkeypatch.chdir(tmp_path)
    assert runtime_data_file("verified_mappings.json") == expected
    for cwd in (PROJECT_ROOT, tmp_path):
        result = subprocess.run([sys.executable, "-c",
            "from evplatform.core.paths import runtime_data_file; print(runtime_data_file('verified_mappings.json'))"],
            cwd=cwd, capture_output=True, text=True, timeout=10, check=True)
        assert Path(result.stdout.strip()) == expected


@pytest.mark.parametrize("different_cwd", [False, True])
def test_provider_bridge_loads_reviewed_mapping_from_both_working_directories(
        tmp_path, monkeypatch, different_cwd):
    _, mappings, _ = bootstrap_nba_pilot.validated_review()
    data = tmp_path / "pilot-data"
    data.mkdir()
    (data / "verified_mappings.json").write_text(mappings.model_dump_json(), encoding="utf-8")
    engine = Engine(Settings(mode="production", sample_feed_enabled=False,
        data_dir=str(data), database_path=str(tmp_path / "unused.db")))
    bridge = ProviderBridge(OddsPapiRest, engine)

    async def no_network_connect(self):
        return False

    monkeypatch.setattr(OddsPapiRest, "connect", no_network_connect)
    monkeypatch.chdir(tmp_path if different_cwd else PROJECT_ROOT)

    async def check():
        await bridge.start(PluginContext(engine.bus, engine.schemas, config={}))
        try:
            assert len(bridge.mapper.mappings) == 4
            matched = bridge.mapper(recorded_frame())
            assert len(matched) == 2
            assert all(quote.source_observed_at is None for _, quote in matched)
        finally:
            await bridge.stop()

    asyncio.run(check())


def test_bootstrap_persists_disabled_profile_and_structural_review(monkeypatch, tmp_path):
    calls = []
    before = {"mode": "production", "sample_feed_enabled": False,
              "auto_trade": False, "kill_switch": True}
    responses = {"/api/state": before,
        "/api/launch/probe": {"account": {"environment": "production",
            "identity_verified": True, "inventory_reconciled": True,
            "cash_available_verified": True, "account_fingerprint": "a" * 64}},
        "/api/launch/setup": {"pricing": PricingConfig().model_dump(mode="json")},
        "/api/native/orders": {"orders": []}}
    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.url.path in responses
        return httpx.Response(200, json=responses[request.url.path])
    real = httpx.Client
    monkeypatch.setattr(bootstrap_nba_pilot.httpx, "Client", lambda **kwargs:
        real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(bootstrap_nba_pilot, "build_fingerprint", lambda: "b" * 64)
    monkeypatch.chdir(tmp_path)
    result = bootstrap_nba_pilot.bootstrap(tmp_path / "data")
    assert result["execution_authorized"] is False
    profile = LaunchProfile.model_validate_json((tmp_path / "data/launch_profile.json").read_text())
    assert profile.market_ids == ("nba-20261022-den-okc-normal-winner",)
    assert profile.validation_pilot and profile.evidence == ()
    assert profile.limits.unit_size_usd == 5 and profile.limits.max_native_order_attempts == 1
    assert not json.loads((tmp_path / "data/nba_pilot_owner_approval_draft.json").read_text())["approved"]
    assert len(VerifiedProviderMapper.from_file(tmp_path / "data/verified_mappings.json").mappings) == 4
    assert calls == [("GET", "/api/state"), ("POST", "/api/launch/probe"),
                     ("GET", "/api/launch/setup"), ("GET", "/api/native/orders")]
    # Only the build-bound metadata may advance; scope, limits and disabled
    # authorization must remain identical across a safe code deployment.
    monkeypatch.setattr(bootstrap_nba_pilot, "build_fingerprint", lambda: "c" * 64)
    refreshed = bootstrap_nba_pilot.bootstrap(tmp_path / "data")
    assert refreshed["pilot_profile_saved"]
    saved = LaunchProfile.model_validate_json((tmp_path / "data/launch_profile.json").read_text())
    assert saved.build_id == "c" * 64 and saved.evidence == ()
    assert not json.loads((tmp_path / "data/nba_pilot_owner_approval_draft.json").read_text())["approved"]
