"""The scheduled one-shot gate cannot turn research into a production order."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import httpx
import pytest

from scripts import watch_native_one_shot as gate


HTTPX_CLIENT = httpx.Client
NOW = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)
DIGEST = "d" * 64
MARKET = "reviewed-current-market"


def authorization(*, expired=False):
    return {"schema_version": 1, "authorization_id": "owner-one-shot-20261024",
            "issued_at": (NOW-timedelta(hours=2 if expired else 1)).isoformat(),
            "expires_at": (NOW-timedelta(minutes=1) if expired else NOW+timedelta(hours=1)).isoformat(),
            "max_upfront_debit_usd": "5", "max_loss_usd": "5",
            "minimum_ev": "0.015", "uncertainty_probability": "0.01",
            "maximum_native_attempts": 1,
            "venues": ["kalshi", "novig", "prophetx", "polymarket_us"]}


def state(*, armed=False):
    risk = {name: "5" for name in ("max_order_stake", "max_event_exposure",
        "max_player_exposure", "max_venue_exposure", "max_correlated_exposure",
        "max_total_exposure", "live_max_order_stake")}
    risk.update(max_native_order_attempts=1, max_open_orders=1, max_orders_per_minute=1,
        available_cash_only=True, block_overlapping_events=True,
        block_overlapping_players=True, stake_sizing="quarter_kelly",
        live_stake_authorized=True, unit_size_usd="5")
    return {"mode": "production", "sample_feed_enabled": False, "live_enabled": True,
            "auto_trade": armed, "kill_switch": not armed,
            "native_authorization_expires_at": (NOW+timedelta(hours=1)).isoformat() if armed else None,
            "markets": [{"id": MARKET, "event_id": "nfl-event", "event_name": "Home vs Away",
                "sport": "football", "league": "NFL", "participants": ["Home", "Away"],
                "start_time": (NOW+timedelta(hours=5)).isoformat(), "market_type": "player_prop",
                "player": "Reviewed Player", "player_id": "reviewed-player", "statistic": "receptions",
                "line": "3.5", "outcomes": ["over", "under"],
                "settlement": {"period": "FULL", "overtime": "included"},
                "extensions": {}}],
            "risk": risk}


def setup(*, ready=True):
    return {"scope": {"profile_count": 1}, "launch_scope": [{
        "venue": "novig", "environment": "production", "validation_pilot": True,
        "strategy_ids": ["native_positive_ev"],
        "settlement_policy": "novig_ioc_win_loss_halt_on_exception",
        "markets": [{"id": MARKET}],
    }], "risk_policy": {"source": "launch_profile", "applied": True,
        "values": {"max_native_order_attempts": 1, "max_order_stake_usd": "5"}},
        "pricing": {"reference_quality_mode": "trusted_provider",
            "normal_outcome_policy": "winner_only_binary", "reference_profile_mode": "strict",
            "min_ev": "0.015", "market_overrides": {},
            "trusted_pilot_policy": {"enabled": True, "owner_approved": True,
                "exact_market_id": MARKET, "uncertainty_margin_probability": "0.01",
                "max_debit_usd": "5", "max_loss_usd": "5", "provider_id": "parlay_rest",
                "trusted_books": ["fanduel", "draftkings", "caesars"],
                "min_distinct_books": 3, "accept_unknown_active_status": True,
                "accept_unknown_quote_observation_age": True,
                "max_indicator_age_ms": 30000, "independent_crosscheck": "two_distinct_books",
                "approval_evidence": "owner approved scoped test policy"}},
        "safe_to_arm": ready, "activation": {"available": ready, "scope_digest": DIGEST}}


def mock_server(monkeypatch, tmp_path: Path, *, ready=True, armed=False, expired=False, orders=None):
    auth_file, status_file = tmp_path / "authorization.json", tmp_path / "status.json"
    auth_file.write_text(json.dumps(authorization(expired=expired)), encoding="utf-8")
    monkeypatch.setattr(gate, "AUTH_PATH", auth_file)
    monkeypatch.setattr(gate, "STATUS_PATH", status_file)
    monkeypatch.setattr(gate, "LOCK_PATH", tmp_path / "gate.lock")
    current = state(armed=armed)
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/control":
            body = json.loads(request.content)
            if body["auto_trade"]:
                assert body["expected_launch_digest"] == DIGEST
                assert body["authorization_expires_at"] == authorization()["expires_at"]
            current["auto_trade"] = body["auto_trade"]
            current["kill_switch"] = body["kill_switch"]
            current["native_authorization_expires_at"] = body.get("authorization_expires_at")
            return httpx.Response(200, json=current)
        result = {"/api/state": current, "/api/native/orders": {"orders": orders or []},
                  "/api/launch/setup": setup(ready=ready)}
        return httpx.Response(200, json=result[request.url.path])
    monkeypatch.setattr(gate.httpx, "Client", lambda **kwargs: HTTPX_CLIENT(
        transport=httpx.MockTransport(handle), **kwargs))
    return current, calls, status_file


def test_pending_launch_keeps_emergency_stop_and_never_posts(monkeypatch, tmp_path):
    current, calls, status_file = mock_server(monkeypatch, tmp_path, ready=False)
    assert gate.tick(now=NOW) == "waiting_for_live_checks"
    assert ("POST", "/api/control") not in calls
    assert current["kill_switch"] is True
    assert json.loads(status_file.read_text())["phase"] == "pending"


def test_ready_scope_arms_once_and_durable_attempt_disarms(monkeypatch, tmp_path):
    current, calls, _ = mock_server(monkeypatch, tmp_path)
    assert gate.tick(now=NOW) == "armed"
    assert current["auto_trade"] is True and current["kill_switch"] is False
    assert calls.count(("POST", "/api/control")) == 1
    assert gate.tick(now=NOW) == "armed"
    assert calls.count(("POST", "/api/control")) == 1
    # A manual emergency stop is never undone by a later scheduled tick.
    current["auto_trade"], current["kill_switch"] = False, True
    assert gate.tick(now=NOW) == "interrupted"
    assert calls.count(("POST", "/api/control")) == 1
    # A restarted controller sees the durable order inventory before any arm.
    current, calls, status = mock_server(monkeypatch, tmp_path, armed=True,
                                         orders=[{"id": "durable-attempt", "status": "unknown"}])
    assert gate.tick(now=NOW) == "consumed"
    assert current["auto_trade"] is False and current["kill_switch"] is True
    assert json.loads(status.read_text())["phase"] == "consumed"


def test_expiry_disarms_but_does_not_stop_research(monkeypatch, tmp_path):
    current, calls, status = mock_server(monkeypatch, tmp_path, armed=True, expired=True)
    assert gate.tick(now=NOW) == "expired"
    assert current["auto_trade"] is False and current["kill_switch"] is True
    assert ("POST", "/api/control") in calls
    assert json.loads(status.read_text())["phase"] == "expired"


def test_unapproved_or_wrong_market_never_arms(monkeypatch, tmp_path):
    current, calls, _ = mock_server(monkeypatch, tmp_path)
    original = gate._get
    def read(client, path):
        value = original(client, path)
        if path == "/api/launch/setup":
            value["pricing"]["trusted_pilot_policy"]["exact_market_id"] = "another-game"
        return value
    monkeypatch.setattr(gate, "_get", read)
    assert gate.tick(now=NOW) == "waiting_for_approved_source_and_market_policy"
    assert ("POST", "/api/control") not in calls
    assert current["kill_switch"] is True


def test_selected_market_override_is_checked_without_blocking_other_sports():
    current = state()
    policy = setup()
    policy["pricing"]["market_overrides"] = {
        "basketball:moneyline": {"reference_quality_mode": "strict_verified"},
        "football:player_prop": {"reference_quality_mode": "trusted_provider"},
    }
    assert gate._scope(policy, current)[0] == "ready"
    policy["pricing"]["market_overrides"]["football:player_prop"]["min_ev"] = "0.01"
    assert gate._scope(policy, current)[0] == "waiting_for_approved_source_and_market_policy"


def test_reviewed_dynamic_nfl_prop_scope_can_reach_existing_launch_gate():
    current, launch = state(), setup()
    policy = launch["pricing"]["trusted_pilot_policy"]
    policy["exact_market_id"] = None
    policy["market_scope"] = {"venue": "novig", "sport": "football", "league": "NFL",
        "market_type": "player_prop", "statistics": ["receptions", "receiving_yards"],
        "period": "FULL", "reviewed_normal_rule_id": "rule-nfl-2026"}
    current["markets"][0]["extensions"] = {
        "reviewed_normal_rule_id": "rule-nfl-2026", "normal_outcome_reviewed": True}
    assert gate._scope(launch, current) == ("ready", DIGEST)

    current["markets"][0]["extensions"]["research_only"] = True
    assert gate._scope(launch, current)[0] == "waiting_for_approved_source_and_market_policy"
    del current["markets"][0]["extensions"]["research_only"]
    current["markets"][0]["line"] = "4"
    assert gate._scope(launch, current)[0] == "waiting_for_approved_source_and_market_policy"
    current["markets"][0]["line"] = "3.5"
    launch["launch_scope"][0]["venue"] = "kalshi"
    launch["launch_scope"][0]["settlement_policy"] = gate.SETTLEMENT["kalshi"]
    assert gate._scope(launch, current)[0] == "waiting_for_approved_source_and_market_policy"


def test_conditional_prophetx_family_pilot_waits_for_real_market_and_binds_approval():
    current, launch = state(), setup()
    market = current["markets"][0]
    market.update(sport="basketball", league="NBA", market_type="moneyline",
        player=None, player_id=None, statistic=None, line=None,
        participants=["Atlanta Hawks", "Charlotte Hornets"],
        outcomes=["Atlanta Hawks", "Charlotte Hornets"],
        settlement={"period": "FULL", "overtime": "included"},
        extensions={"reviewed_normal_rule_id": "reviewed-nba-rule",
                    "normal_outcome_reviewed": True})
    scope = {"venue": "prophetx", "sport": "basketball", "league": "NBA",
        "market_type": "moneyline", "statistics": [], "period": "FULL",
        "reviewed_normal_rule_id": "reviewed-nba-rule"}
    profile = launch["launch_scope"][0]
    profile.update(venue="prophetx", account_fingerprint="a" * 64,
        settlement_policy=gate.SETTLEMENT["prophetx"], family_rule={
            "scope": scope, "expires_at": (NOW+timedelta(hours=2)).isoformat()})
    policy = launch["pricing"]["trusted_pilot_policy"]
    policy.update(exact_market_id=None, market_scope=scope, provider_id="parlay_game_ws",
        trusted_books=["fanduel", "draftkings", "caesars", "bet365"],
        reference_weights_percent={name: "25" for name in
            ("fanduel", "draftkings", "caesars", "bet365")},
        recurring_authorization=True, maximum_native_attempts=None,
        automatic_followups=True, max_debit_usd="25", max_loss_usd="25")
    launch["pricing"]["live_multi_book_enabled"] = True
    auth = {**authorization(), "venues": ["prophetx"],
        "conditional_family_pilot": True, "launch_scope_digest": DIGEST,
        "account_fingerprint": "a" * 64}
    assert gate._authorization(auth, NOW)[1] == auth["authorization_id"]
    assert gate._scope(launch, current, auth, NOW) == ("ready", DIGEST)
    launch["safe_to_arm"] = False
    launch["activation"]["available"] = False
    launch["launch_scope"][0]["markets"] = []
    assert gate._scope(launch, current, auth, NOW)[0] == "waiting_for_qualifying_family_market"
    launch["launch_scope"][0]["markets"] = [{"id": MARKET}]
    launch["safe_to_arm"] = True
    launch["activation"]["available"] = True
    market["extensions"]["research_only"] = True
    assert gate._scope(launch, current, auth, NOW)[0] == "waiting_for_approved_source_and_market_policy"
    del market["extensions"]["research_only"]
    assert gate._scope(launch, current, {**auth, "launch_scope_digest": "x"*64}, NOW)[0] == \
        "conditional_pilot_profile_changed"
    with pytest.raises(ValueError):
        gate._authorization({**auth, "venues": ["kalshi"]}, NOW)


def test_check_only_and_invalid_limits_make_zero_control_calls(monkeypatch, tmp_path):
    _, calls, _ = mock_server(monkeypatch, tmp_path)
    assert gate.tick(now=NOW, check_only=True) == "ready"
    assert ("POST", "/api/control") not in calls
    unsafe = state()
    unsafe["risk"]["max_native_order_attempts"] = 2
    assert not gate._risk_fits(unsafe)
    with pytest.raises(ValueError):
        gate._local_url("https://example.org")
    with pytest.raises(ValueError):
        gate._authorization({**authorization(), "max_loss_usd": "6"}, NOW)


def test_removed_authorization_disarms_an_armed_process(monkeypatch, tmp_path):
    current, calls, status = mock_server(monkeypatch, tmp_path, armed=True)
    gate.AUTH_PATH.unlink()
    assert gate.tick(now=NOW) == "disabled"
    assert current["auto_trade"] is False and current["kill_switch"] is True
    assert ("POST", "/api/control") in calls
    assert json.loads(status.read_text())["phase"] == "disabled"


def test_temporary_live_quote_gap_does_not_rearm_or_place(monkeypatch, tmp_path):
    current, calls, _ = mock_server(monkeypatch, tmp_path, ready=False, armed=True)
    assert gate.tick(now=NOW) == "armed"
    assert ("POST", "/api/control") not in calls
    # Per-order preflight in the existing OMS still rejects a stale quote.
