"""The owner helper may inspect a trade without opening a placement path."""
from types import SimpleNamespace

import httpx

from scripts import owner_nba_pilot as pilot


def test_owner_dry_run_uses_only_local_reads_and_unsigned_preview(monkeypatch, capsys):
    calls = []
    responses = {
        ("GET", "/api/state"): {
            "mode": "production", "sample_feed_enabled": False,
            "auto_trade": False, "kill_switch": True,
        },
        ("GET", "/api/launch/setup"): {
            "safe_to_arm": True,
            "launch_scope": [{"venue": "kalshi", "validation_pilot": True,
                              "markets": [{"id": "nba-den-okc"}]}],
            "activation": {"scope_digest": "a" * 64},
            "risk_policy": {"applied": True},
        },
        ("GET", "/api/native/orders"): {"orders": []},
        ("POST", "/api/native/manual/preview"): {
            "unsigned": True, "profile_digest": "a" * 64,
            "created_at": "2026-10-22T18:00:00Z", "expires_at": "2026-10-22T18:01:30Z",
            "build_id": "b" * 64, "account_fingerprint_suffix": "12345678",
            "market": {"event_name": "Denver @ Oklahoma City", "settlement": {}},
            "book_source_time": "2026-10-22T18:00:00Z", "executable_depth": [],
            "risk_decisions": {"non_arming_checks_passed": True},
            "candidate": {
                "outcome": "Denver Nuggets", "references": [],
                "fair_probability": "0.30", "expected_profit_usd": "0.02",
                "roi": "0.02", "sizing": {},
                "intent": {"instrument": {"instrument_id": "KXNBAGAME-DEN",
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
    result = pilot.trade(SimpleNamespace(max_cost_usd="1", submit=False,
                                         market_id="nba-den-okc", outcome="Denver Nuggets"))
    assert result == 0
    assert calls == [
        ("GET", "/api/state"), ("GET", "/api/launch/setup"),
        ("GET", "/api/native/orders"), ("POST", "/api/native/manual/preview"),
    ]
    assert "DRY RUN ONLY" in capsys.readouterr().out


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
        pilot.trade(SimpleNamespace(max_cost_usd="1", submit=False,
                                    market_id="nba-den-okc", outcome="Denver Nuggets"))
        assert False, "A missing launch profile must be rejected"
    except ValueError as exc:
        assert "stopped, reviewed" in str(exc)
    assert calls == [("GET", "/api/state"), ("GET", "/api/launch/setup")]
