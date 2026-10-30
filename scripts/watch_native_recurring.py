"""Lease a reviewed recurring native strategy through the existing control gate.

The production supervisor calls this once per minute only when a separate,
scope-bound recurring authorization exists. Research continues after expiry or
an emergency stop. This controller never constructs or submits an order.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from evplatform.core.config import TrustedPilotPolicy
from evplatform.core.models import Market
try:
    from scripts import watch_native_one_shot as shared
except ModuleNotFoundError:
    # The scheduled supervisor invokes this file directly, so sys.path[0] is
    # scripts/ rather than the repository root used by pytest imports.
    import watch_native_one_shot as shared


AUTH_PATH = shared.ROOT / "data" / "native_recurring_authorization.json"
STATUS_PATH = shared.ROOT / "data" / "native_recurring_status.json"
MIN_EV = Decimal("0.015")
UNCERTAINTY = Decimal("0.01")


def _authorization(data: dict, now: datetime) -> tuple[str, datetime, str]:
    issued, expires = shared._time(data.get("issued_at")), shared._time(data.get("expires_at"))
    identity, digest = data.get("authorization_id"), data.get("launch_scope_digest")
    if (data.get("schema_version") != 1 or not isinstance(identity, str) or len(identity) < 16
            or not isinstance(digest, str) or len(digest) != 64
            or issued is None or expires is None or issued > now
            or not issued < expires <= issued + timedelta(hours=24)
            or data.get("maximum_native_attempts") is not None
            or data.get("cash_only") is not True or data.get("stake_sizing") != "quarter_kelly"
            or shared._money(data.get("unit_size_usd")) != Decimal("5")
            or shared._money(data.get("minimum_ev")) != MIN_EV
            or shared._money(data.get("uncertainty_probability")) != UNCERTAINTY
            or not isinstance(data.get("venues"), list) or len(data["venues"]) != 1
            or len(set(data["venues"])) != len(data["venues"])
            or not set(data["venues"]) <= shared.VENUES):
        raise ValueError("recurring_authorization_invalid")
    return identity, expires, digest


def _scope(setup: dict, state: dict, authorized_digest: str, venues: set[str]) -> str:
    if (state.get("mode") != "production" or state.get("sample_feed_enabled") is not False
            or state.get("live_enabled") is not True):
        return "production_workspace_unavailable"
    risk = state.get("risk") or {}
    if (risk.get("max_native_order_attempts") is not None
            or risk.get("available_cash_only") is not True
            or risk.get("stake_sizing") != "quarter_kelly"
            or risk.get("live_stake_authorized") is not True
            or shared._money(risk.get("unit_size_usd")) != Decimal("5")):
        return "recurring_cash_kelly_scope_unavailable"
    all_profiles = setup.get("launch_scope") or []
    profiles = [p for p in all_profiles if p.get("venue") in venues]
    policy_state = setup.get("risk_policy") or {}
    limits = policy_state.get("values") or {}
    if (len(profiles) != 1 or len(all_profiles) != (setup.get("scope") or {}).get("profile_count")
            or policy_state.get("source") not in {"launch_profile", "launch_portfolio"}
            or policy_state.get("applied") is not True
            or limits.get("max_native_order_attempts") is not None
            or limits.get("available_cash_only") is not True
            or limits.get("stake_sizing") != "quarter_kelly"
            or shared._money(limits.get("unit_size_usd")) != Decimal("5")):
        return "waiting_for_reviewed_recurring_launch_profile"
    monetary_caps = ("max_order_stake_usd", "max_event_exposure_usd",
        "max_player_exposure_usd", "max_venue_exposure_usd",
        "max_correlated_exposure_usd", "max_total_exposure_usd",
        "daily_loss_limit_usd", "max_native_daily_debit_usd")
    if any((cap := shared._money(limits.get(name))) is None or cap <= 0
           for name in monetary_caps):
        return "waiting_for_reviewed_recurring_risk_limits"
    if (not isinstance(limits.get("max_open_orders"), int)
            or limits["max_open_orders"] < 1
            or not isinstance(limits.get("max_orders_per_minute"), int)
            or limits["max_orders_per_minute"] < 1):
        return "waiting_for_reviewed_recurring_risk_limits"
    if any(p.get("venue") not in venues or p.get("environment") != "production"
           or p.get("validation_pilot") is True
           or p.get("strategy_ids") != ["native_positive_ev"]
           or not isinstance(p.get("markets"), list)
           or (not p["markets"] and p.get("family_rule") is None) for p in profiles):
        return "launch_scope_outside_recurring_authorization"
    for profile in profiles:
        family = profile.get("family_rule")
        if family is not None and (not isinstance(family, dict)
                or not isinstance(family.get("scope"), dict)
                or family["scope"].get("venue") != profile["venue"]
                or shared._time(family.get("approved_at")) is None
                or (expiry := shared._time(family.get("expires_at"))) is None
                or expiry <= datetime.now(timezone.utc)):
            return "reviewed_family_rule_expired_or_changed"
    pricing = setup.get("pricing") or {}
    try:
        policy = TrustedPilotPolicy.model_validate(pricing.get("trusted_pilot_policy") or {})
    except ValueError:
        return "waiting_for_recurring_source_review"
    if (not policy.enabled or not policy.owner_approved or not policy.recurring_authorization
            or not policy.automatic_followups or policy.maximum_native_attempts is not None
            or shared._money(policy.uncertainty_margin_probability) != UNCERTAINTY):
        return "waiting_for_recurring_source_review"
    if any(profile.get("family_rule") is not None for profile in profiles):
        if (pricing.get("live_multi_book_enabled") is not True
                or policy.reference_weights_percent is None
                or policy.market_scope is None
                or any(profile["family_rule"].get("scope") !=
                       policy.market_scope.model_dump(mode="json")
                       for profile in profiles)):
            return "waiting_for_recurring_source_review"
    rows = state.get("markets") or []
    if not isinstance(rows, list):
        return "selected_market_not_in_current_canonical_state"
    by_id: dict[str, list[dict]] = {}
    for row in rows:
        if isinstance(row, dict):
            by_id.setdefault(row.get("id"), []).append(row)
    selected = [item.get("id") for profile in profiles for item in profile["markets"]]
    if len(selected) != len(set(selected)):
        return "selected_market_not_in_current_canonical_state"
    no_selected_family = not selected and all(
        profile.get("family_rule") is not None for profile in profiles)
    for profile in profiles:
        for item in profile["markets"]:
            matched = by_id.get(item.get("id"), [])
            if len(matched) != 1:
                return "selected_market_not_in_current_canonical_state"
            try:
                market = Market.model_validate(matched[0])
            except ValueError:
                return "selected_market_not_in_current_canonical_state"
            if not policy.permits_market(market) or (policy.market_scope is not None
                    and profile["venue"] != policy.market_scope.venue):
                return "waiting_for_recurring_source_review"
            override = (pricing.get("market_overrides") or {}).get(
                f"{market.sport}:{market.market_type}", {})
            if not isinstance(override, dict):
                return "waiting_for_recurring_source_review"
            resolved = {**pricing, **override}
            if (resolved.get("reference_quality_mode") != "trusted_provider"
                    or resolved.get("normal_outcome_policy") != "winner_only_binary"
                    or resolved.get("reference_profile_mode") != "strict"
                    or resolved.get("trusted_pilot_policy") != pricing.get("trusted_pilot_policy")
                    or (minimum := shared._money(resolved.get("min_ev"))) is None
                    or minimum < MIN_EV):
                return "waiting_for_recurring_source_review"
    activation = setup.get("activation") or {}
    venue_activation = (activation.get("by_venue") or {}).get(next(iter(venues)), {})
    if venue_activation.get("scope_digest") != authorized_digest:
        return "reviewed_launch_digest_changed"
    # An already armed family lease survives a quiet market interval. The
    # application still rejects every order until a fresh exact family market
    # passes its own readiness and final dispatch checks.
    if no_selected_family:
        return "waiting_for_live_checks"
    if venue_activation.get("available") is not True:
        return "waiting_for_live_checks"
    return "ready"


def _status(phase: str, detail: str, identity: str | None = None,
            *, arm_attempted: bool = False) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = {}
    try:
        previous = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    attempted = arm_attempted or (previous.get("authorization_id") == identity
                                 and previous.get("arm_attempted") is True)
    payload = {"phase": phase, "detail": detail, "authorization_id": identity,
               "arm_attempted": attempted, "updated_at": datetime.now(timezone.utc).isoformat()}
    fd, name = tempfile.mkstemp(prefix="native-recurring-", suffix=".tmp", dir=STATUS_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.write("\n")
        os.replace(name, STATUS_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def tick(url: str = "http://127.0.0.1:8002", *, check_only: bool = False,
         now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    with shared._single_controller(), httpx.Client(base_url=shared._local_url(url), timeout=8) as client:
        state = shared._get(client, "/api/state")
        if state.get("mode") != "production":
            raise ValueError("Port 8002 is not the production scanner")

        def disarm():
            if not check_only and (state.get("auto_trade") is True or state.get("kill_switch") is not True):
                shared._control(client, arm=False)

        if not AUTH_PATH.exists():
            disarm()
            _status("disabled", "No recurring authorization is saved")
            return "disabled"
        if shared.AUTH_PATH.exists():
            disarm()
            _status("blocked", "One-shot and recurring authorizations cannot coexist")
            return "blocked"
        try:
            saved = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
            identity, expires, digest = _authorization(saved, now)
        except (OSError, json.JSONDecodeError, ValueError):
            disarm()
            _status("blocked", "Saved recurring authorization is invalid")
            return "blocked"
        if now >= expires:
            disarm()
            _status("expired", "Authorization expired; research scanning continues", identity)
            return "expired"
        try:
            setup = shared._get(client, "/api/launch/setup")
            orders = shared._get(client, "/api/native/orders").get("orders")
        except (httpx.HTTPError, ValueError):
            disarm()
            raise
        if not isinstance(orders, list):
            disarm()
            raise ValueError("Native attempt inventory is unavailable")
        if any(order.get("status") in {"intent", "submitting", "unknown", "cancel_pending"}
               for order in orders if isinstance(order, dict)):
            disarm()
            _status("blocked", "An unresolved native transmission needs reconciliation", identity)
            return "blocked"
        phase = _scope(setup, state, digest, set(saved["venues"]))
        running_expiry = shared._time(state.get("native_authorization_expires_at"))
        if state.get("auto_trade") is True:
            if (state.get("kill_switch") is True or running_expiry != expires
                    or set(state.get("native_enabled_venues") or []) != set(saved["venues"])
                    or phase not in {"ready", "waiting_for_live_checks"}):
                disarm()
                _status("blocked", "Running scope or authorization changed", identity)
                return "blocked"
            _status("armed", "Monitoring reviewed recurring markets", identity, arm_attempted=True)
            return "armed"
        if phase != "ready":
            disarm()
            _status("pending", phase, identity)
            return phase
        try:
            previous = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if previous.get("authorization_id") == identity and previous.get("arm_attempted") is True:
            disarm()
            _status("interrupted", "Prior arming ended; a new owner authorization is required", identity)
            return "interrupted"
        if check_only:
            _status("ready", "Reviewed launch gate passes; no control call made", identity)
            return "ready"
        _status("arming", "One bounded recurring activation attempted", identity, arm_attempted=True)
        try:
            updated = shared._control(client, arm=True, digest=digest, expires=expires,
                                      native_venues=saved["venues"])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            _status("pending", "Readiness changed before activation", identity)
            return "pending"
        if updated.get("auto_trade") is not True or updated.get("kill_switch") is not False:
            raise ValueError("Live activation was not acknowledged")
        _status("armed", "Monitoring reviewed recurring markets", identity)
        return "armed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        tick(args.url, check_only=args.check_only)
    except (ValueError, OSError, httpx.HTTPError) as exc:
        _status("error", type(exc).__name__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
