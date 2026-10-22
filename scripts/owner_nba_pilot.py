"""Owner-operated, one-attempt Kalshi NBA pilot through the existing local OMS.

Default mode is read-only dry-run. --submit requires an interactive typed
confirmation; this script is never used by the automatic execution worker.
It does not contain credentials or call a venue placement endpoint directly.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation

import httpx


BASE = "http://127.0.0.1:8002"
HEADERS = {"X-EV-Control": "local"}


def get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def post(client: httpx.Client, path: str, body: dict) -> dict:
    response = client.post(path, json=body, headers=HEADERS)
    response.raise_for_status()
    return response.json()


def stopped_pilot(client: httpx.Client) -> dict:
    state = get(client, "/api/state")
    setup = get(client, "/api/launch/setup")
    scopes = setup.get("launch_scope") or []
    if (state.get("mode") != "production" or state.get("sample_feed_enabled") is not False
            or state.get("auto_trade") is not False or state.get("kill_switch") is not True
            or setup.get("safe_to_arm") is not True or len(scopes) != 1
            or scopes[0].get("venue") != "kalshi" or scopes[0].get("validation_pilot") is not True
            or not setup.get("activation", {}).get("scope_digest")
            or setup.get("risk_policy", {}).get("applied") is not True):
        raise ValueError("The stopped, reviewed, one-attempt Kalshi production pilot is unavailable")
    if get(client, "/api/native/orders").get("orders"):
        raise ValueError("A native order attempt already exists; reconcile it, never submit another pilot")
    return setup


def display(proposal: dict) -> dict:
    candidate = proposal["candidate"]
    intent = candidate["intent"]
    instrument = intent["instrument"]
    return {
        "created_at": proposal["created_at"], "expires_at": proposal["expires_at"],
        "build_id": proposal["build_id"], "profile_digest": proposal["profile_digest"],
        "account_suffix": proposal["account_fingerprint_suffix"],
        "event": proposal["market"]["event_name"],
        "selected_team": candidate["outcome"], "native_ticker": instrument["instrument_id"],
        "native_outcome": instrument["native_outcome"], "settlement": proposal["market"]["settlement"],
        "book_source_time": proposal["book_source_time"],
        "executable_depth": proposal["executable_depth"],
        "reference_books": candidate["references"],
        "fair_probability": candidate["fair_probability"],
        "limit_price": intent["limit_price"], "quantity": intent["quantity"],
        "payout_per_contract_usd": instrument["payout_per_unit_usd"],
        "maximum_cost_including_fees_usd": intent["reserved_cost_usd"],
        "maximum_fee_usd": intent["max_fee_usd"],
        "estimated_profit_usd": candidate["expected_profit_usd"],
        "estimated_roi": candidate["roi"], "quarter_kelly": candidate["sizing"],
        "risk_decisions": proposal["risk_decisions"], "unsigned": proposal["unsigned"],
    }


def trade(args) -> int:
    try:
        cap = Decimal(args.max_cost_usd)
    except InvalidOperation as exc:
        raise ValueError("Enter an exact positive dollar cap") from exc
    if not cap.is_finite() or cap <= 0:
        raise ValueError("Enter an exact positive dollar cap")
    if args.submit and not sys.stdin.isatty():
        raise ValueError("Owner submission requires an interactive terminal")
    with httpx.Client(base_url=BASE, timeout=20, trust_env=False, follow_redirects=False) as client:
        setup = stopped_pilot(client)
        if args.market_id not in {market["id"] for market in setup["launch_scope"][0]["markets"]}:
            raise ValueError("Market is outside the reviewed one-order pilot scope")
        proposal = post(client, "/api/native/manual/preview", {
            "market_id": args.market_id, "venue": "kalshi", "outcome": args.outcome,
            "max_cost_usd": str(cap)})
        if (proposal.get("unsigned") is not True or proposal.get("profile_digest") !=
                setup["activation"]["scope_digest"]):
            raise ValueError("Unsigned proposal or reviewed scope is inconsistent")
        print(json.dumps(display(proposal), indent=2))
        if not args.submit:
            print("DRY RUN ONLY: zero order-placement requests; review expires unused.")
            return 0
        intent = proposal["candidate"]["intent"]
        ticker = intent["instrument"]["instrument_id"]
        phrase = f"PLACE ONE {ticker} MAX ${cap}"
        print("To release the emergency stop and make ONE owner-operated order attempt, type:")
        print(phrase)
        if input("Confirmation: ").strip() != phrase:
            print("Not confirmed. No arming or order request sent.")
            return 2
        # The normal control gate validates this exact reviewed profile while
        # explicitly keeping automatic trading disabled.
        stopped_pilot(client)
        try:
            post(client, "/api/control", {"auto_trade": False, "kill_switch": False,
                                          "expected_launch_digest": proposal["profile_digest"]})
            state = get(client, "/api/state")
            if state.get("auto_trade") is not False or state.get("kill_switch") is not False:
                raise ValueError("Manual-only arming did not take effect")
            # One POST, no transport retry. A timeout is UNKNOWN until read-only
            # reconciliation; the CLI never sends a replacement placement.
            result = post(client, "/api/native/manual/submit", {
                "review_token": proposal["review_token"],
                "terms_digest": proposal["terms_digest"],
                "approved_max_cost_usd": proposal["max_cost_usd"]})
            order = result.get("order") or {}
            print(json.dumps({"order_id": order.get("id"), "native_order_id": order.get("venue_order_id"),
                              "status": order.get("status"), "filled_quantity": order.get("filled_quantity"),
                              "message": "Acknowledgement is not a fill or settlement; reconcile by order ID."}))
        finally:
            try:
                post(client, "/api/control", {"kill_switch": True})
            except (httpx.HTTPError, ValueError):
                print("Stop confirmation failed; inspect /api/state and native ledger immediately.", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-id", required=True, help="Reviewed canonical market ID from /api/launch/setup")
    parser.add_argument("--outcome", required=True, help="Reviewed canonical selected-team outcome")
    parser.add_argument("--max-cost-usd", required=True, help="Owner-approved upper bound including fees")
    parser.add_argument("--submit", action="store_true", help="Owner only: typed confirmation and one real attempt")
    args = parser.parse_args()
    try:
        return trade(args)
    except (ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        # Never print request URLs or headers; OddsPapi keys are query secrets.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else "Local control request failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
