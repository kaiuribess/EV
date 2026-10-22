"""Preconfigure the optional Owls Pinnacle reference without enabling requests.

Only changes local plugin settings. It never reads a key, contacts Owls, or
authorizes an order. Repeat once per local workspace/port.
"""
from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit

import httpx


PLUGIN = "owls_pinnacle_realtime"


def prepare(url: str) -> dict:
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("Expected a loopback HTTP workspace URL")
    base = url.rstrip("/")
    with httpx.Client(timeout=10) as client:
        response = client.get(f"{base}/api/state")
        response.raise_for_status()
        state = response.json()
        records = [item for item in state.get("plugins", [])
                   if item.get("manifest", {}).get("name") == PLUGIN]
        if len(records) != 1:
            raise RuntimeError("Restart this workspace with the current build")
        record = records[0]
        if record.get("enabled") is True:
            raise RuntimeError("Disable the Owls realtime provider before changing its polling scope")
        config = dict(record["config"])
        config.update({"sports": ["nba"], "bookmakers": ["pinnacle"],
                       "markets": ["h2h", "spreads", "totals"],
                       "poll_interval_seconds": 30.0, "max_requests_per_minute": 4,
                       "session_request_budget": 100000, "include_props": False,
                       "alternates": False})
        if config != record["config"]:
            changed = client.post(f"{base}/api/plugins/{PLUGIN}/config",
                                  headers={"X-EV-Control": "local"}, json={"config": config})
            changed.raise_for_status()
        confirm = client.get(f"{base}/api/state")
        confirm.raise_for_status()
        current = next(item for item in confirm.json().get("plugins", [])
                       if item.get("manifest", {}).get("name") == PLUGIN)
        if current.get("enabled") or current.get("config", {}).get("sports") != ["nba"]:
            raise RuntimeError("Optional provider did not remain disabled with the requested scope")
        return {"ok": True, "workspace": state.get("mode"), "plugin": PLUGIN,
                "configured_sport": "nba", "poll_interval_seconds": 30,
                "monthly_requests_if_continuous_approx": 86400,
                "enabled": False, "network_requests_to_owls": 0,
                "native_trading_armed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    args = parser.parse_args()
    try:
        result = prepare(args.url)
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "reason": str(exc)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
