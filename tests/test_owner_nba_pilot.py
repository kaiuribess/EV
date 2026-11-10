"""The owner helper may inspect a trade without opening a placement path."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import pytest

import httpx

from scripts import owner_nba_pilot as pilot


def test_owner_dry_run_uses_only_local_reads_and_unsigned_preview(monkeypatch, capsys, tmp_path):
    calls = []
    responses = {
        ("GET", "/api/state"): {
            "mode": "production", "sample_feed_enabled": False,
            "auto_trade": False, "kill_switch": True,
        },
        ("GET", "/api/launch/setup"): {
            "safe_to_arm": True,
            "launch_scope": [{"venue": "kalshi", "validation_pilot": True,
                              "markets": [{"id": "nba-den-okc", "event_id": "nba-20261022-den-okc",
                                "native_selections": [{"ticker": "KXNBAGAME-DEN",
                                                       "outcome": "Denver Nuggets"}]}]}],
            "activation": {"scope_digest": "a" * 64},
            "risk_policy": {"applied": True},
        },
        ("GET", "/api/native/orders"): {"orders": []},
        ("POST", "/api/native/manual/preview"): {
            "unsigned": True, "profile_digest": "a" * 64,
            "proposal_digest": "c" * 64, "max_cost_usd": "1",
            "created_at": "2026-10-22T18:00:00Z", "expires_at": "2026-10-22T18:01:30Z",
            "build_id": "b" * 64, "account_fingerprint_suffix": "12345678",
            "market": {"id": "nba-den-okc", "event_id": "nba-20261022-den-okc",
                       "event_name": "Denver @ Oklahoma City", "settlement": {}},
            "book_source_time": "2026-10-22T18:00:00Z", "executable_depth": [],
            "risk_decisions": {"non_arming_checks_passed": True},
            "candidate": {
                "outcome": "Denver Nuggets", "references": [],
                "fair_probability": "0.30", "expected_profit_usd": "0.02",
                "roi": "0.02", "sizing": {},
                "intent": {"instrument": {"instrument_id": "KXNBAGAME-DEN",
                                           "canonical_market_id": "nba-den-okc",
                                           "native_outcome": "yes", "payout_per_unit_usd": "1"},
                           "limit_price": "0.27", "quantity": "1",
                           "reserved_cost_usd": "0.29", "max_fee_usd": "0.02"},
            },
        },
    }

    def handler(request):
        key = (request.method, request.url.path)
        calls.append(key)
        assert request.url.host == "127.0.0.1"
        assert key in responses, f"Dry run unexpectedly called {key}"
        return httpx.Response(200, json=responses[key])

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(pilot.httpx, "Client", lambda **kwargs:
                        real_client(transport=transport, **kwargs))
    path = tmp_path / "proposal.json"
    result = pilot.trade(SimpleNamespace(max_cost_usd="1", event_id="nba-20261022-den-okc",
        native_ticker="KXNBAGAME-DEN", outcome="Denver Nuggets", proposal_file=path))
    assert result == 0
    assert calls == [
        ("GET", "/api/state"), ("GET", "/api/launch/setup"),
        ("GET", "/api/native/orders"), ("POST", "/api/native/manual/preview"),
    ]
    assert "DRY RUN ONLY" in capsys.readouterr().out
    assert __import__("json").loads(path.read_text())["proposal_digest"] == "c" * 64


def test_owner_helper_rejects_unreviewed_scope_before_preview(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"mode": "production", "sample_feed_enabled": False,
                                         "auto_trade": False, "kill_switch": True})

    real_client = httpx.Client
    monkeypatch.setattr(pilot.httpx, "Client", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(handler), **kwargs))
    try:
        pilot.trade(SimpleNamespace(max_cost_usd="1", event_id="nba-20261022-den-okc",
            native_ticker="KXNBAGAME-DEN", outcome="Denver Nuggets",
            proposal_file=__import__("pathlib").Path("unused.json")))
        assert False, "A missing launch profile must be rejected"
    except ValueError as exc:
        assert "stopped, reviewed" in str(exc)
    assert calls == [("GET", "/api/state"), ("GET", "/api/launch/setup")]


def test_exact_selection_is_independent_of_launch_scope_order():
    other = {"id": "other", "event_id": "another-event",
             "native_selections": [{"ticker": "OTHER", "outcome": "Denver Nuggets"}]}
    selected = {"id": "nba-den-okc", "event_id": "nba-20261022-den-okc",
                "native_selections": [{"ticker": "KXNBAGAME-DEN", "outcome": "Denver Nuggets"}]}
    for markets in ([other, selected], [selected, other]):
        setup = {"launch_scope": [{"venue": "kalshi", "markets": markets}]}
        assert pilot.select_market(setup, "nba-20261022-den-okc", "KXNBAGAME-DEN",
                                   "Denver Nuggets") == "nba-den-okc"
        with pytest.raises(ValueError, match="exactly one"):
            pilot.select_market(setup, "nba-20261022-den-okc", "OTHER", "Denver Nuggets")
    with pytest.raises(ValueError, match="exactly one"):
        pilot.select_market({"launch_scope": [{"venue": "kalshi", "markets": [selected, selected]}]},
            "nba-20261022-den-okc", "KXNBAGAME-DEN", "Denver Nuggets")


def test_expired_saved_proposal_fails_before_control_or_placement(monkeypatch, tmp_path):
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps({"expires_at": "2020-01-01T00:00:00Z",
        "candidate": {"outcome": "Denver Nuggets", "references": [], "fair_probability": "0.3",
            "expected_profit_usd": "0", "roi": "0", "sizing": {},
            "intent": {"instrument": {"instrument_id": "KX-DEN", "native_outcome": "yes",
                                      "payout_per_unit_usd": "1"}, "limit_price": "0.3", "quantity": "1",
                       "reserved_cost_usd": "0.3", "max_fee_usd": "0"}},
        "proposal_digest": "a" * 64, "created_at": "2020-01-01T00:00:00Z",
        "build_id": "b", "profile_digest": "c", "account_fingerprint_suffix": "12345678",
        "market": {"event_id": "nba-event", "event_name": "Game", "settlement": {}},
        "book_source_time": None, "executable_depth": [], "max_cost_usd": "1",
        "risk_decisions": {}, "unsigned": True}))
    monkeypatch.setattr(pilot.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(pilot.httpx, "Client", lambda **_: pytest.fail("Expired proposal contacted production"))
    with pytest.raises(ValueError, match="expired"):
        pilot.submit_saved(SimpleNamespace(proposal_file=path))


def test_atomic_local_proposal_claim_allows_only_one_windows_invocation(tmp_path):
    path = tmp_path / "proposal.json"
    def attempt(_):
        try:
            pilot._claim_attempt(path, "a" * 64)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    assert Path(str(path) + ".attempted").read_text().strip() == "a" * 64
