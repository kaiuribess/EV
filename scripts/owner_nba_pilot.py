"""Owner-operated, one-attempt Kalshi NBA pilot through the existing local OMS.

Prepare saves a short-lived unsigned proposal. The separate submit command
requires an interactive typed confirmation; this script is never used by the
automatic execution worker.
It does not contain credentials or call a venue placement endpoint directly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

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
            or len(scopes[0].get("markets") or []) != 1
            or not setup.get("activation", {}).get("scope_digest")
            or setup.get("risk_policy", {}).get("applied") is not True):
        raise ValueError("The stopped, reviewed, one-attempt Kalshi production pilot is unavailable")
    if get(client, "/api/native/orders").get("orders"):
        raise ValueError("A native order attempt already exists; reconcile it, never submit another pilot")
    return setup


def select_market(setup: dict, event_id: str, native_ticker: str, outcome: str) -> str:
    """An array reorder or same-name game cannot redirect the owner's order."""
    matches = [market["id"] for scope in setup.get("launch_scope", [])
        if scope.get("venue") == "kalshi"
        for market in scope.get("markets", []) if market.get("event_id") == event_id
        for native in market.get("native_selections", [])
        if native.get("ticker") == native_ticker and native.get("outcome") == outcome]
    if len(matches) != 1:
        raise ValueError("Select exactly one reviewed event, native ticker and outcome")
    return matches[0]


def display(proposal: dict) -> dict:
    candidate = proposal["candidate"]
    intent = candidate["intent"]
    instrument = intent["instrument"]
    return {
        "proposal_digest": proposal["proposal_digest"],
        "created_at": proposal["created_at"], "expires_at": proposal["expires_at"],
        "build_id": proposal["build_id"], "profile_digest": proposal["profile_digest"],
        "account_suffix": proposal["account_fingerprint_suffix"],
        "event_id": proposal["market"]["event_id"],
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
        "owner_proposed_cap_usd": proposal["max_cost_usd"],
        "maximum_fee_usd": intent["max_fee_usd"],
        "estimated_profit_usd": candidate["expected_profit_usd"],
        "estimated_roi": candidate["roi"], "quarter_kelly": candidate["sizing"],
        "risk_decisions": proposal["risk_decisions"], "unsigned": proposal["unsigned"],
    }


def _matching_proposal(proposal: dict, setup: dict, event_id: str,
                       native_ticker: str, outcome: str) -> str:
    market_id = select_market(setup, event_id, native_ticker, outcome)
    instrument = proposal["candidate"]["intent"]["instrument"]
    if (proposal.get("unsigned") is not True or proposal.get("profile_digest") !=
            setup["activation"]["scope_digest"] or proposal["market"].get("id") != market_id
            or proposal["market"].get("event_id") != event_id
            or proposal["candidate"].get("outcome") != outcome
            or instrument.get("instrument_id") != native_ticker
            or instrument.get("canonical_market_id") != market_id
            or Decimal(proposal["candidate"]["intent"]["reserved_cost_usd"]) > Decimal(proposal["max_cost_usd"])
            or not isinstance(proposal.get("proposal_digest"), str)
            or len(proposal["proposal_digest"]) != 64):
        raise ValueError("Unsigned proposal, native selection or reviewed scope is inconsistent")
    return market_id


def _save_once(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("Proposal file already exists; choose a new path") from exc
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _claim_attempt(path: Path, proposal_digest: str) -> None:
    """Reserve this proposal once across concurrent local owner invocations."""
    claim = Path(str(path) + ".attempted")
    try:
        handle = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("Proposal already attempted; reconcile before another review") from exc
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(proposal_digest + "\n")


def trade(args) -> int:
    try:
        cap = Decimal(args.max_cost_usd)
    except InvalidOperation as exc:
        raise ValueError("Enter an exact positive dollar cap") from exc
    if not cap.is_finite() or cap <= 0:
        raise ValueError("Enter an exact positive dollar cap")
    with httpx.Client(base_url=BASE, timeout=20, trust_env=False, follow_redirects=False) as client:
        setup = stopped_pilot(client)
        market_id = select_market(setup, args.event_id, args.native_ticker, args.outcome)
        proposal = post(client, "/api/native/manual/preview", {
            "market_id": market_id, "venue": "kalshi", "outcome": args.outcome,
            "max_cost_usd": str(cap)})
        _matching_proposal(proposal, setup, args.event_id, args.native_ticker, args.outcome)
        if Decimal(proposal["max_cost_usd"]) != cap:
            raise ValueError("Proposed owner cap changed")
        _save_once(args.proposal_file, proposal)
        print(json.dumps(display(proposal), indent=2))
        print("DRY RUN ONLY: saved an unsigned proposal; zero arming and placement requests.")
    return 0


def submit_saved(args) -> int:
    if not sys.stdin.isatty():
        raise ValueError("Owner submission requires an interactive terminal")
    proposal = json.loads(args.proposal_file.read_text(encoding="utf-8"))
    shown = display(proposal)
    expires = datetime.fromisoformat(proposal["expires_at"].replace("Z", "+00:00"))
    if expires.utcoffset() is None or datetime.now(timezone.utc) >= expires:
        raise ValueError("Proposal expired; prepare a new one")
    event_id, ticker, outcome = shown["event_id"], shown["native_ticker"], shown["selected_team"]
    with httpx.Client(base_url=BASE, timeout=20, trust_env=False, follow_redirects=False) as client:
        setup = stopped_pilot(client)
        _matching_proposal(proposal, setup, event_id, ticker, outcome)
        print(json.dumps(shown, indent=2))
        phrase = (f"PLACE ONE {ticker} {outcome} {proposal['proposal_digest'][:12]} "
                  f"MAX ${proposal['max_cost_usd']}")
        print("To release the emergency stop and make ONE owner-operated order attempt, type:")
        print(phrase)
        if input("Confirmation: ").strip() != phrase:
            print("Not confirmed. No arming or order request sent.")
            return 2
        stopped_pilot(client)
        if datetime.now(timezone.utc) >= expires:
            raise ValueError("Proposal expired before owner confirmation; no arming request sent")
        # This durable claim is atomic across Windows processes. A timeout or
        # rejection retains it; only read-only reconciliation can resolve that.
        _claim_attempt(args.proposal_file, proposal["proposal_digest"])
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
                "proposal_digest": proposal["proposal_digest"],
                "approved_max_cost_usd": proposal["max_cost_usd"]})
            order = result.get("order") or {}
            print(json.dumps({"order_id": order.get("id"), "native_order_id": order.get("venue_order_id"),
                              "status": order.get("status"), "filled_quantity": order.get("filled_quantity"),
                              "final_validation_digest": result.get("final_validation_digest"),
                              "message": "Acknowledgement is not a fill or settlement; reconcile by order ID."}))
        finally:
            try:
                post(client, "/api/control", {"kill_switch": True})
            except (httpx.HTTPError, ValueError):
                print("Stop confirmation failed; inspect /api/state and native ledger immediately.", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Save an unsigned, exact proposal")
    prepare.add_argument("--event-id", required=True)
    prepare.add_argument("--native-ticker", required=True)
    prepare.add_argument("--outcome", required=True)
    prepare.add_argument("--max-cost-usd", required=True,
                         help="Proposed ceiling including fees; never a target stake")
    prepare.add_argument("--proposal-file", required=True, type=Path)
    submit = commands.add_parser("submit", help="Owner only: consume one saved proposal")
    submit.add_argument("--proposal-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        return trade(args) if args.command == "prepare" else submit_saved(args)
    except (ValueError, KeyError, TypeError, AttributeError, OSError, httpx.HTTPError) as exc:
        # Never print request URLs or headers; OddsPapi keys are query secrets.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else "Local control request failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
