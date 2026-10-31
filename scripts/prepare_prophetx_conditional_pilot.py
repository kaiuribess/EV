"""Prepare a disabled, family-scoped ProphetX first-order review package.

This never changes the running launch profile, authorization, or order ledger.
The owner must approve the separate bounded pilot before an operator applies
the draft, records current account/risk evidence, and starts its one-shot gate.
"""
from __future__ import annotations

import argparse
import json
from datetime import timezone
from decimal import Decimal
from pathlib import Path

from evplatform.core.native_runtime import build_fingerprint
from evplatform.core.readiness import LaunchProfile


CAP = Decimal("5")
MONEY_CAPS = ("max_order_stake_usd", "max_event_exposure_usd",
    "max_player_exposure_usd", "max_venue_exposure_usd",
    "max_correlated_exposure_usd", "max_total_exposure_usd",
    "daily_loss_limit_usd", "max_native_daily_debit_usd")


def prepare(current: LaunchProfile) -> dict:
    rule = current.family_rule
    if (current.venue != "prophetx" or current.environment != "production"
            or current.validation_pilot or rule is None
            or rule.scope.market_type != "moneyline" or rule.scope.league != "NBA"
            or rule.scope.period != "FULL" or current.min_reference_books < 3
            or current.settlement_policy != "prophetx_funded_cash_win_loss_halt_on_exception"
            or not ((current.limits.stake_sizing == "quarter_kelly"
                     and current.limits.available_cash_only
                     and current.limits.cash_policy == "verified_current")
                    or (current.limits.stake_sizing == "capped"
                        and not current.limits.available_cash_only
                        and current.limits.cash_policy == "prophetx_exchange_rejection"))):
        raise ValueError("A reviewed recurring ProphetX NBA moneyline family profile is required")
    limits = current.limits.model_dump(mode="json")
    limits.update({name: str(min(CAP, Decimal(limits[name]))) for name in MONEY_CAPS})
    limits.update(max_native_order_attempts=1, max_open_orders=1,
                  max_orders_per_minute=1)
    payload = current.model_dump(mode="json")
    payload.update(build_id=build_fingerprint(), validation_pilot=True,
                   limits=limits, evidence=[])
    pilot = LaunchProfile.model_validate(payload)
    return {"schema_version": 1, "approved": False,
        "purpose": "One first ProphetX OMS order under the reviewed NBA full-game moneyline family",
        "launch_profile": pilot.model_dump(mode="json"),
        "authorization_template": {
            "schema_version": 1, "authorization_id": None,
            "issued_at": None, "expires_at": None,
            "conditional_family_pilot": True,
            "venues": ["prophetx"],
            "account_fingerprint": pilot.account_fingerprint,
            "launch_scope_digest": pilot.digest(),
            "max_upfront_debit_usd": "5", "max_loss_usd": "5",
            "minimum_ev": "0.015", "uncertainty_probability": "0.01",
            "maximum_native_attempts": 1,
        },
        "source_policy": "Existing approved ProphetX NBA moneyline three-book policy; no change",
        "rule_expires_at": rule.expires_at.astimezone(timezone.utc).isoformat(),
        "pilot_lifecycle_evidence": "Deferred until the first genuine OMS attempt; no certificate is asserted",
        "activation_requirements": [
            "Separate owner approval of this exact profile digest and one-attempt scope",
            "Fresh authenticated account and risk authorization evidence bound to the pilot profile",
            "Current exact family mapping, three eligible books, executable depth and location check",
        ]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path("data/launch_profile.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("data/prophetx_conditional_pilot_draft.json"))
    args = parser.parse_args()
    draft = prepare(LaunchProfile.model_validate_json(args.profile.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(draft, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    print(json.dumps({"approved": False, "venue": "prophetx",
        "profile_digest": draft["authorization_template"]["launch_scope_digest"],
        "rule_expires_at": draft["rule_expires_at"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
