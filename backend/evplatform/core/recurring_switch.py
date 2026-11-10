"""Owner-operated, expiring ProphetX recurring authorization.

The switch renews only the already reviewed pregame family and monetary scope.
It never submits an order; the supervised recurring controller still performs
the release and the normal OMS still owns every submission.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .paths import runtime_data_file
from .readiness import LaunchEvidence, LaunchProfile, digest_json
from ..venues.prophetx_props import (NBA_PROP_STATISTICS, NFL_PROP_STATISTICS,
    reviewed_nba_prop_rule_tokens, reviewed_nfl_prop_rule_tokens)


NBA_PROP_SCOPE = tuple(sorted(NBA_PROP_STATISTICS))
NFL_PROP_SCOPE = tuple(sorted(NFL_PROP_STATISTICS))
NBA_PROP_SOURCE_EVIDENCE = (
    "Parlay full-game NBA player-prop keys and two-sided line payloads: "
    "https://parlay-api.com/docs/websocket/player-props; ordinary participation/void rules "
    "remain bound to the reviewed normal-outcome policy")
NBA_PROP_NATIVE_EVIDENCE = (
    "CFTC ProphetX NBA filing 61599 Appendix A: player-stat markets require participation "
    "and use official statistics including corrections within 48 hours; full-event markets "
    "include overtime unless a shorter period is specified; "
    "https://framerusercontent.com/assets/xFIxMV69o6oKsLMzIkdlbTdqrTc.pdf")
NFL_SOURCE_EVIDENCE = (
    "Parlay full-game NFL h2h and documented player-prop keys/period fields: "
    "https://parlay-api.com/docs/websocket/player-props; only exact complete opposing "
    "book pairs with period FULL enter the reviewed normal-outcome policy")
NFL_NATIVE_EVIDENCE = (
    "ProphetX CFTC filing 61599 Appendix A applies official-stat, participation and "
    "correction rules to player-stat markets; event-result markets include overtime "
    "unless a shorter period is named; exact NFL subtype/title/strike evidence is "
    "revalidated from V4/Talaria per fixture; "
    "https://framerusercontent.com/assets/xFIxMV69o6oKsLMzIkdlbTdqrTc.pdf")
COLLEGE_SOURCE_EVIDENCE = (
    "Parlay complete-board game h2h contract and canonical sport keys "
    "americanfootball_ncaaf and basketball_ncaab: https://parlay-api.com/docs; "
    "only exact complete full-game two-sided pairs enter the reviewed policy")
COLLEGE_NATIVE_EVIDENCE = (
    "ProphetX CFTC filing 61599 Appendix A applies event-result official-score rules "
    "and includes overtime unless a shorter period is named; exact tournament, event, "
    "two-team Moneyline subtype and strike identity are revalidated from V4/Talaria; "
    "https://framerusercontent.com/assets/xFIxMV69o6oKsLMzIkdlbTdqrTc.pdf")
TENNIS_SOURCE_EVIDENCE = (
    "Parlay complete-board game h2h contract and canonical tennis_atp/tennis_wta sport "
    "keys: https://parlay-api.com/docs; only exact complete full-match two-sided pairs "
    "enter the reviewed normal-completion policy")
TENNIS_NATIVE_EVIDENCE = (
    "ProphetX CFTC filing 61599 Appendix A event-result rules plus the authenticated V4 "
    "catalog's distinct unqualified match Moneyline and separately named set markets; "
    "only the unqualified two-player match winner is admitted and exceptional retirement "
    "settlement remains outside winner_only_binary; "
    "https://framerusercontent.com/assets/xFIxMV69o6oKsLMzIkdlbTdqrTc.pdf")

REVIEWED_ADDITIONAL_COMPETITIONS = (
    ("football", "NFL"), ("football", "NCAAF"),
    ("basketball", "NCAABM"), ("tennis", "ATP"), ("tennis", "WTA"),
)
REVIEWED_COMPETITION_SETS = {
    frozenset(), frozenset({("football", "NFL")}),
    frozenset(REVIEWED_ADDITIONAL_COMPETITIONS),
}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _reviewed_scope(engine, profile: LaunchProfile) -> None:
    rule = profile.family_rule
    policy = engine.settings.pricing.trusted_pilot_policy
    limits = profile.limits
    if (engine.settings.mode != "production" or profile.environment != "production"
            or profile.venue != "prophetx" or profile.validation_pilot
            or not profile.owner_approved_recurring_lifecycle_deferral
            or rule is None or rule.scope.venue != "prophetx"
            or (rule.scope.sport, rule.scope.league, rule.scope.market_type,
                rule.scope.period, rule.scope.phase) !=
                ("basketball", "NBA", "moneyline", "FULL", "pregame")
            or frozenset(rule.scope.additional_competitions) not in REVIEWED_COMPETITION_SETS
            or (rule.scope.additional_player_prop_statistics
                and tuple(sorted(rule.scope.additional_player_prop_statistics)) != NBA_PROP_SCOPE)
            or (rule.scope.additional_nfl_player_prop_statistics
                and tuple(sorted(rule.scope.additional_nfl_player_prop_statistics)) != NFL_PROP_SCOPE)
            or bool(rule.scope.additional_nfl_player_prop_statistics) !=
               (("football", "NFL") in rule.scope.additional_competitions)
            or rule.prophetx_fee_evidence_kind != "owner_approved_assumption"
            or rule.prophetx_assumed_winnings_profit_rate != Decimal("0.02")
            or profile.provider_ids != (("parlay_game_ws", "parlay_ws")
                if (rule.scope.additional_player_prop_statistics
                    or rule.scope.additional_nfl_player_prop_statistics)
                else ("parlay_game_ws",))
            or profile.strategy_ids != ("native_positive_ev",)
            or profile.min_reference_books < 3
            or profile.max_reference_age_ms > 30000
            or profile.settlement_policy != "prophetx_funded_cash_win_loss_halt_on_exception"
            or not policy.enabled or not policy.owner_approved
            or not policy.recurring_authorization or not policy.automatic_followups
            or policy.market_scope != rule.scope
            or policy.provider_id != "parlay_game_ws"
            or policy.player_prop_provider_id not in {None, "parlay_ws"}
            or policy.exact_market_id is not None
            or policy.uncertainty_margin_probability != Decimal("0.01")
            or policy.min_distinct_books < 3
            or set(policy.trusted_books) != {"fanduel", "draftkings", "caesars", "bet365"}
            or policy.reference_weights_percent is None
            or set(policy.reference_weights_percent.values()) != {Decimal("25")}
            or not policy.accept_unknown_active_status
            or not policy.accept_unknown_quote_observation_age
            or policy.max_indicator_age_ms != 30000
            or policy.independent_crosscheck != "two_distinct_books"
            or policy.fee_estimate_mode != "after_fees"
            or not policy.pregame_only
            or policy.max_debit_usd != Decimal("5")
            or policy.max_loss_usd != Decimal("5")
            or limits.cash_policy != "prophetx_exchange_rejection"
            or limits.available_cash_only or limits.stake_sizing != "capped"
            or limits.max_native_order_attempts is not None
            or limits.max_order_stake_usd > Decimal("5")
            or limits.max_total_exposure_usd > Decimal("20")
            or limits.max_native_daily_debit_usd is None
            or limits.max_native_daily_debit_usd > Decimal("20")
            or limits.daily_loss_limit_usd is None
            or limits.daily_loss_limit_usd > Decimal("20")
            or limits.max_open_orders != 1 or limits.max_orders_per_minute != 1
            or limits.unit_size_usd != Decimal("5")):
        raise ValueError("The saved ProphetX scope differs from the reviewed pregame recurring policy")
    if profile.pricing_digest != digest_json(engine.settings.pricing.model_dump(mode="json")):
        raise ValueError("Pricing changed; review the source policy before turning Live on")
    if (engine.settings.pricing.min_ev < Decimal("0.015")
            or engine.settings.pricing.normal_outcome_policy != "winner_only_binary"
            or engine.settings.pricing.reference_quality_mode != "trusted_provider"
            or engine.settings.pricing.trusted_max_delivery_age_ms > 30000):
        raise ValueError("The approved minimum EV threshold changed")
    if engine.native.mapper is None or profile.mapping_digest != engine.native.mapper.mapping_digest:
        raise ValueError("Native mapping rules changed; review them before turning Live on")


def _expanded_pregame_scope(scope):
    evidence = scope.source_normal_rule_evidence or ""
    if NBA_PROP_SOURCE_EVIDENCE not in evidence:
        evidence = f"{evidence}; {NBA_PROP_SOURCE_EVIDENCE}".strip("; ")
    if NFL_SOURCE_EVIDENCE not in evidence:
        evidence = f"{evidence}; {NFL_SOURCE_EVIDENCE}".strip("; ")
    if COLLEGE_SOURCE_EVIDENCE not in evidence:
        evidence = f"{evidence}; {COLLEGE_SOURCE_EVIDENCE}".strip("; ")
    if TENNIS_SOURCE_EVIDENCE not in evidence:
        evidence = f"{evidence}; {TENNIS_SOURCE_EVIDENCE}".strip("; ")
    return type(scope).model_validate({
        **scope.model_dump(mode="json"),
        "additional_competitions": REVIEWED_ADDITIONAL_COMPETITIONS,
        "additional_player_prop_statistics": NBA_PROP_SCOPE,
        "additional_nfl_player_prop_statistics": NFL_PROP_SCOPE,
        "reviewed_normal_rule_id": "PARLAY-PREGAME-MULTISPORT-FULL-MONEYLINE-PROPS-20261110",
        "source_normal_rule_evidence": evidence,
    })


def issue_prophetx_authorization(engine, expected_digest: str, *, now: datetime | None = None,
                                 authorization_path: Path | None = None) -> dict:
    """Record one explicit switch-on review for the same bounded family scope.

    Caller holds the controller lock. A genuine current authenticated account
    snapshot supplies account evidence; owner click supplies only authorization.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("An aware authorization time is required")
    native = engine.native
    profile = native.profile_for("prophetx")
    if native.portfolio is not None or len(native.profiles_by_venue()) != 1 or profile is None:
        raise ValueError("A single reviewed ProphetX account is required")
    if expected_digest != native.report("prophetx").profile_digest:
        raise ValueError("The reviewed launch scope changed; refresh before enabling")
    _reviewed_scope(engine, profile)
    snapshot = native.account_snapshots.get("prophetx")
    if (snapshot is None or not snapshot.identity_verified or not snapshot.reconciled
            or snapshot.account_fingerprint != profile.account_fingerprint
            or snapshot.environment != "production"
            or snapshot.observed_at.tzinfo is None
            or not 0 <= (now - snapshot.observed_at).total_seconds() < 600
            or snapshot.open_order_count is None or snapshot.position_count is None
            or native.account_probe_errors.get("prophetx")):
        raise ValueError("A current reconciled ProphetX account read is required")
    if (not engine.risk.external_exposure_bounded("prophetx")
            or engine.risk.kill_reason not in {"", "Operator emergency stop",
                "Native account inventory differs from the managed order ledger",
                "Persisted emergency stop; reset explicitly"}
            or any(order.venue == "prophetx" and not order.reconciliation_complete
                   for order in engine.risk.native_orders.values())
            or native.manager.invalidated_settlements):
        raise ValueError("Unresolved ProphetX inventory or settlement blocks Live")
    approval_id = str(uuid4())
    expires = now + timedelta(hours=24)
    expanded_scope = _expanded_pregame_scope(profile.family_rule.scope)
    updated_policy = type(engine.settings.pricing.trusted_pilot_policy).model_validate({
        **engine.settings.pricing.trusted_pilot_policy.model_dump(mode="json"),
        "market_scope": expanded_scope.model_dump(mode="json"),
        "player_prop_provider_id": "parlay_ws",
        "approval_evidence": (f"owner-live-switch:{approval_id}:NBA/NFL/NCAAF/NCAABM/"
                              "ATP/WTA moneylines and exact NBA/NFL full-game player props"),
    })
    updated_pricing = type(engine.settings.pricing).model_validate({
        **engine.settings.pricing.model_dump(mode="json"),
        "trusted_pilot_policy": updated_policy.model_dump(mode="json"),
    })
    updated_rule = type(profile.family_rule).model_validate({
        **profile.family_rule.model_dump(mode="json"),
        "scope": expanded_scope.model_dump(mode="json"),
        "rule_version": "PREGAME-MULTISPORT-MONEYLINE-AND-PROPS-20261110",
        "allowed_native_rule_texts": tuple(dict.fromkeys((
            *profile.family_rule.allowed_native_rule_texts,
            "NFL|moneyline|moneyline|default_full_game",
            "NCAAF|moneyline|moneyline|default_full_game",
            "NCAABM|moneyline|moneyline|default_full_game",
            "ATP|moneyline|moneyline|default_full_game",
            "WTA|moneyline|moneyline|default_full_game",
            *reviewed_nba_prop_rule_tokens(),
            *reviewed_nfl_prop_rule_tokens(),
        ))),
        "prophetx_native_settlement_evidence": NBA_PROP_NATIVE_EVIDENCE,
        "additional_native_settlement_evidence": {
            "football:NFL": NFL_NATIVE_EVIDENCE,
            "football:NCAAF": COLLEGE_NATIVE_EVIDENCE,
            "basketball:NCAABM": COLLEGE_NATIVE_EVIDENCE,
            "tennis:ATP": TENNIS_NATIVE_EVIDENCE,
            "tennis:WTA": TENNIS_NATIVE_EVIDENCE,
        },
        "approved_by": "local_owner_live_switch",
        "approval_evidence": f"owner-live-switch:{approval_id}",
        "approved_at": now, "expires_at": expires,
    })
    reviewed = profile.model_copy(update={
        "build_id": native.build_id, "family_rule": updated_rule, "evidence": (),
        "pricing_digest": digest_json(updated_pricing.model_dump(mode="json")),
        "provider_ids": ("parlay_game_ws", "parlay_ws"),
    })
    digest = reviewed.digest()
    scope_artifact = {
        "authorization_id": approval_id, "reviewed_at": now.isoformat(),
        "expires_at": expires.isoformat(), "prior_profile_digest": profile.digest(),
        "profile_digest": digest, "account_fingerprint": profile.account_fingerprint,
        "family_rule": updated_rule.model_dump(mode="json"),
        "limits": profile.limits.model_dump(mode="json"),
        "pricing_digest": reviewed.pricing_digest,
        "source_policy": updated_policy.model_dump(mode="json"),
    }
    account_artifact = {
        "authorization_id": approval_id, "source": "authenticated_native",
        "snapshot": snapshot.model_dump(mode="json"),
    }
    evidence = (
        LaunchEvidence(kind="account_access", venue="prophetx", environment="production",
            account_fingerprint=profile.account_fingerprint, profile_digest=digest,
            build_id=native.build_id, observed_at=snapshot.observed_at, expires_at=expires,
            verified_by="authenticated ProphetX account read", source="authenticated_native",
            artifact_sha256=digest_json(account_artifact), passed=True,
            checks=("identity_verified", "inventory_reconciled")),
        LaunchEvidence(kind="risk_authorization", venue="prophetx", environment="production",
            account_fingerprint=profile.account_fingerprint, profile_digest=digest,
            build_id=native.build_id, observed_at=now, expires_at=expires,
            verified_by="local_owner_live_switch", source="operator_authorization",
            artifact_sha256=digest_json(scope_artifact), passed=True),
    )
    updated = LaunchProfile.model_validate(
        reviewed.model_copy(update={"evidence": evidence}).model_dump(mode="json"))
    data_dir = engine.settings.data_dir
    if authorization_path is None:
        from scripts.watch_native_recurring import AUTH_PATH
        authorization_path = AUTH_PATH
    old = None
    if authorization_path.exists():
        old = json.loads(authorization_path.read_text(encoding="utf-8"))
        if not isinstance(old, dict) or not isinstance(old.get("authorization_id"), str):
            raise ValueError("Existing authorization is invalid; review it before renewal")
    audit_dir = runtime_data_file("recurring_switch_audit", data_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    _write_json(audit_dir / f"{approval_id}.prior-profile.json",
                profile.model_dump(mode="json"))
    _write_json(audit_dir / f"{approval_id}.scope.json", scope_artifact)
    _write_json(audit_dir / f"{approval_id}.account.json", account_artifact)
    if old is not None:
        _write_json(audit_dir / f"{approval_id}.retired-authorization.json", old)
    profile_path = runtime_data_file("launch_profile.json", data_dir)
    _write_json(profile_path, updated.model_dump(mode="json"))
    native.profile = updated
    engine.settings.pricing = updated_pricing
    if (getattr(engine, "registry", None) is not None
            and "positive_ev" in engine.registry.records):
        engine.registry.configure("positive_ev", updated_pricing.model_dump(mode="json"))
    authorization = {
        "schema_version": 1, "authorization_id": approval_id,
        "issued_at": now.isoformat(), "expires_at": expires.isoformat(),
        "launch_scope_digest": native.report("prophetx").profile_digest,
        "venues": ["prophetx"], "maximum_native_attempts": None,
        "unit_size_usd": "5", "minimum_ev": "0.015",
        "uncertainty_probability": "0.01", "cash_policy": "prophetx_exchange_rejection",
        "cash_only": False, "stake_sizing": "capped",
    }
    _write_json(authorization_path, authorization)
    return authorization
