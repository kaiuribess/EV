"""Read-only launch preparation facts, including monitors without launch profiles.

This report cannot authorize execution or convert old diagnostic files into
current account facts. All account/market evidence comes from the running engine;
the optional limits file is explicitly an unapplied operator draft.
"""
from __future__ import annotations

import os
from pathlib import Path

from .models import utcnow
from .readiness import LiveLimits, ReadinessBlocker


def _running(task):
    return task is not None and not task.done() and not task.cancelled()


def operator_limits_draft(path="data/operator_launch_limits.json"):
    """Only validated, nonsecret money-policy fields may leave this boundary."""
    try:
        target = Path(path)
        if not target.exists():
            return None, None
        if target.stat().st_size > 16384:
            return None, "The saved operator limits need review."
        return LiveLimits.model_validate_json(target.read_text(encoding="utf-8")), None
    except (OSError, ValueError):
        return None, "The saved operator limits need review."


def launch_setup(engine, *, limits_draft=None, limits_error=None):
    """Build an allowlisted dashboard report without network or disk I/O."""
    from ..plugins.external import NativeMonitor, ProviderBridge

    now = utcnow()
    native = engine.native
    profiles = native.profiles_by_venue()
    aggregate = native.report()
    accounts = []
    software_blockers = []
    mapper = native.mapper
    mappings = mapper.mappings if mapper is not None else ()
    real_reference_markets = {
        market_id for market_id, observations in engine.quotes.items()
        if any(not quote.synthetic for quote in observations.values())
    }

    def current_book(book):
        age = book.age_ms(now)
        return bool(book.synchronized and not book.synthetic and book.status == "active"
                    and book.asks and age is not None and 0 <= age <= engine.settings.pricing.max_book_age_ms)

    inputs = [{"provider": name, "enabled": record.enabled,
        "authenticated": bool(record.enabled and _running(record.plugin.task)
                              and record.health.get("authenticated") is True),
        "mapped_quotes": record.plugin.mapper.mapped_count if record.plugin.mapper else 0,
        "quarantined": len(record.plugin.mapper.quarantine) if record.plugin.mapper else 0}
        for name, record in engine.registry.records.items() if isinstance(record.plugin, ProviderBridge)]
    for venue, record in engine.registry.records.items():
        plugin = record.plugin
        # NativeMonitor carries the provider-specific secret/config boundary.
        # Newly registered native monitors appear without a name allowlist.
        if not isinstance(plugin, NativeMonitor):
            continue
        client = plugin.client
        profile = profiles.get(venue)
        health = record.health
        running = _running(plugin.task)
        configured_environment = record.config.get("environment", "sandbox")
        runtime_environment = getattr(getattr(client, "settings", None), "environment", None)
        try:
            names = plugin.credential_names(record.config)
            credentials_configured = bool(names) and all(bool(os.getenv(name)) for name in names.values())
        except (ValueError, TypeError):
            credentials_configured = False
        snapshot = native.account_snapshots.get(venue)
        snapshot_matches = bool(snapshot and snapshot.venue == venue
            and snapshot.environment == runtime_environment and snapshot.identity_verified)
        # Only the runtime's fenced, fresh account assessment qualifies as
        # current. Do not expose stale cash as a spendable balance.
        facts = native.runtime_facts(venue) if profile else None
        current = bool(snapshot_matches and facts and facts.account_identity_verified)
        report = native.report(venue) if profile else None
        blockers = list(report.blockers) if report else [ReadinessBlocker(
            code="profile.missing", category="authorization", scope=venue,
            message="This account has no reviewed launch profile",
            action="Bind verified account identity, exact markets, pricing, limits and genuine evidence before activation")]
        policies = plugin.manifest.capabilities.get("supported_settlement_policies", [])
        policies = policies if isinstance(policies, list) and all(isinstance(item, str) for item in policies) else []
        if not policies or (profile is not None and profile.settlement_policy not in policies):
            blocker = ReadinessBlocker(code="software.settlement_policy", category="technical", scope=venue,
                message="Native settlement reconciliation is not implemented for this venue" if not policies
                    else "The selected settlement policy is not supported by this venue adapter",
                action="Implement and validate actual payout and position-release accounting before using this venue"
                    if not policies else "Review the adapter's narrow supported settlement policies and retain genuine settlement evidence")
            blockers.append(blocker)
            software_blockers.append(blocker)
        mapped = {item.market.id for item in mappings if item.instrument.venue == venue}
        accounts.append({
            "venue": venue, "enabled": record.enabled,
            "configured_environment": configured_environment,
            "runtime_environment": runtime_environment,
            "credential_profile": record.config.get("credential_profile", "default"),
            "credentials_configured": credentials_configured,
            "authenticated": bool(record.enabled and running and health.get("authenticated") is True),
            "streaming": bool(record.enabled and running and health.get("streaming") is True),
            "monitor_running": running, "profile_configured": profile is not None,
            "verified_native_markets": len(mapped),
            "current_native_books": sum(current_book(book)
                for key, book in native.books.items() if key[1] == venue),
            "account_snapshot": {
                "available": snapshot is not None,
                "current": current,
                "identity_verified": current,
                "reconciled": bool(current and snapshot.reconciled),
                "cash_available_verified": bool(current and snapshot.cash_available_verified),
                "balance_usd": str(snapshot.balance_usd) if current and snapshot.cash_available_verified
                    and snapshot.balance_usd is not None else None,
                "observed_at": snapshot.observed_at.isoformat() if snapshot else None,
            },
            "settlement_policy": profile.settlement_policy if profile else None,
            "supported_settlement_policies": policies,
            "blockers": [item.model_dump(mode="json") for item in blockers],
        })

    selected = next(iter(profiles.values()), None)
    limits = selected.limits if selected else limits_draft
    applied = bool(selected and native.runtime_facts(selected.venue).configured_limits == selected.limits
                   and engine.settings.mode == selected.environment)
    risk_policy = {
        "source": "launch_profile" if selected else "saved_operator_draft" if limits_draft else "unconfigured",
        "values": limits.model_dump(mode="json") if limits else None,
        "applied": applied, "error": limits_error,
    }
    native_markets = {item.market.id for item in mappings}
    launch_scope = []
    for venue, profile in profiles.items():
        scoped_markets = []
        for market_id in profile.market_ids:
            market = engine.markets.get(market_id)
            name = market_id if market is None else " · ".join(str(value) for value in (
                market.event_name, market.market_type, market.player, market.statistic,
                market.line, market.settlement.period) if value is not None and value != "")
            scoped_markets.append({"id": market_id, "name": name})
        launch_scope.append({"venue": venue, "environment": profile.environment,
            "account_fingerprint": profile.account_fingerprint,
            "markets": scoped_markets, "strategy_ids": list(profile.strategy_ids),
            "provider_ids": list(profile.provider_ids), "settlement_policy": profile.settlement_policy})
    requirements = []

    def requirement(code, passed, title, detail, *, external=False):
        requirements.append({"code": code, "status": "passed" if passed else "pending",
            "title": title, "detail": detail, "external_evidence_required": external})

    requirement("scope.markets", bool(native_markets & real_reference_markets), "Verify matching real markets",
        "Match event, player, line and settlement rules across real sportsbook references and executable venue books.", external=True)
    requirement("scope.profiles", bool(profiles) and not native.profile_error, "Review the exact accounts and limits",
        "A saved limits draft is not an applied launch profile. Each selected account needs its own verified identity and market scope.")
    pricing = engine.settings.pricing
    reference_profiles_ready = True
    if pricing.reference_profile_mode == "strict":
        selected_references = [pricing.reference_profiles[name] for name in pricing.enabled_reference_profiles]
        reference_profiles_ready = bool(selected_references) and all(
            profile.live_execution_allowed and all(
                any(rule.verified and rule.key() == selector.key() and rule.bookmaker == book
                    for rule in pricing.profile_freshness_rules)
                for selector in profile.selectors for book in profile.weights_percent)
            for profile in selected_references)
        requirement("pricing.profiles", reference_profiles_ready, "Validate reference profiles for live execution",
            "Starting weights remain research settings. Validate each enabled profile and exact-source freshness policy; every current quote must also pass status, completeness, anchor and sensitivity checks.", external=True)
    relevant_software = [item for item in software_blockers if not profiles or item.scope in profiles]
    requirement("software.settlement", bool(accounts) and not relevant_software,
        "Complete venue settlement support",
        "Each selected venue needs implemented payout and position-release accounting. A connected monitor alone does not supply this capability.")
    selected_accounts = [item for item in accounts if item["profile_configured"]]
    all_accounts_current = (bool(profiles) and len(selected_accounts) == len(profiles)
                            and all(item["account_snapshot"]["current"]
                                    and item["account_snapshot"]["reconciled"]
                                    and item["account_snapshot"]["cash_available_verified"]
                                    for item in selected_accounts))
    requirement("account.current", all_accounts_current, "Verify spendable cash and existing exposure",
        "Use current authenticated account reads, private streams and the shared order ledger; balances are never pooled.", external=True)
    for kind, title, detail in (
        ("native_lifecycle", "Validate real order and fill handling", "Retain genuine acknowledgement, partial-fill, cancellation-race, unknown-outcome, fee and sequence-recovery evidence."),
        ("restart_recovery", "Validate recovery after restart", "Retain genuine durable-intent, open-order and position recovery evidence for this build."),
        ("settlement_reconciliation", "Validate actual settlement and payouts", "Verify native results, paid amounts, fees and position release for the selected settlement policy."),
        ("geographic_eligibility", "Verify account location eligibility", "Use the venue's official eligibility checks for the production account; an authenticated price feed is insufficient."),
        ("risk_authorization", "Confirm the reviewed trading scope", "Authorization must bind the exact accounts, markets, build, pricing and requested limits."),
    ):
        passed = bool(profiles) and not any(b.code == f"evidence.{kind}" for b in aggregate.blockers)
        if kind == "geographic_eligibility" and any(p.environment != "production" for p in profiles.values()):
            passed = False
        requirement(f"evidence.{kind}", passed, title, detail, external=kind != "risk_authorization")
    requirement("runtime.gate", aggregate.safe_to_arm, "Pass the current release checks",
        "Every technical and account check must pass together. Quotes, fees and account facts are checked again before each order.")
    native_mode = engine.settings.mode == "production"
    activation_available = bool(native_mode and engine.settings.live_enabled and aggregate.safe_to_arm and reference_profiles_ready)
    activation_reason = (
        "Current checks passed. Final activation remains an explicit operator action." if activation_available
        else "Finish the current launch checks before enabling real orders." if not aggregate.safe_to_arm or not reference_profiles_ready
        else "Start the reviewed production workspace with native execution configured, then repeat the live checks."
    )
    draft_blockers = []
    if not profiles:
        draft_blockers.append("No reviewed account and market scope is selected.")
    if not native_markets:
        draft_blockers.append("No verified native market mappings are loaded.")
    if not all_accounts_current:
        draft_blockers.append("Current account identity and reconciled inventory must be verified before preparing a launch profile.")
    return {
        "schema_version": 1, "checked_at": now.isoformat(), "environment": engine.settings.mode,
        "safe_to_arm": aggregate.safe_to_arm and reference_profiles_ready,
        "activation": {"available": activation_available,
            "scope_digest": aggregate.profile_digest,
            "requires_restart": not native_mode or not engine.settings.live_enabled,
            "action_label": "Enable live trading", "reason": activation_reason},
        "switches": {"live_enabled": engine.settings.live_enabled, "auto_trade": engine.risk.auto_trade,
                     "kill_switch": engine.risk.kill_switch},
        "scope": {"profile_count": len(profiles), "verified_native_markets": len(native_markets),
            "real_reference_markets": len(real_reference_markets), "native_candidates": len(native.candidates),
            "synthetic_candidates": sum(bool(item.synthetic) for item in engine.opportunities.values())},
        "risk_policy": risk_policy, "accounts": accounts, "requirements": requirements,
        "launch_scope": launch_scope,
        "software_blockers": [item.model_dump(mode="json") for item in software_blockers],
        "pricing": engine.settings.pricing.model_dump(mode="json"), "inputs": inputs,
        "profile_draft": {"can_prepare": False, "blockers": draft_blockers,
            "note": "This read-only report never creates a profile, changes permissions or issues evidence."},
    }
