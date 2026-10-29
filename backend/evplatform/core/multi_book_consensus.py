"""Provider-trusted sportsbook consensus with separate research and live scopes.

Research remains isolated. The live scope requires an approved recurring
source policy and continues through the existing native OMS and risk gates.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .config import PricingConfig
from .models import Market, Quote, utcnow
from .pricing import NoSignal, fair_value, market_config
from .reference_profiles import (ProfileSelector, ReferenceProfile,
                                 trusted_quote_research_eligibility)

SOURCES = frozenset({"parlay_rest", "parlay_ws", "parlay_game_ws", "oddize_rest"})


def consensus_scope(market: Market, quotes: list[Quote], config: PricingConfig,
                    now: datetime | None = None) -> tuple[PricingConfig, str]:
    """Return an isolated pricing policy and an honest weighting description."""
    now = now or utcnow()
    scoped = market_config(config, market)
    if not scoped.multi_book_consensus_enabled:
        raise NoSignal("Multi-book consensus research is disabled")
    if market.status == "scheduled" and market.start_time > now:
        phase = "pregame"
    elif (market.status == "live" and market.start_time <= now
          and market.extensions.get("research_only") is True
          and market.extensions.get("inplay_price_after_start") is True):
        phase = "inplay"
    else:
        raise NoSignal("Market phase lacks supported sportsbook evidence")
    allowed = set(scoped.multi_book_consensus_books)
    eligible = {quote.bookmaker for quote in quotes
        if quote.provider in SOURCES and quote.bookmaker in allowed
        and quote.market_id == market.id and quote.market_fingerprint == market.identity()
        and set(quote.decimal_odds) == set(market.outcomes)
        and trusted_quote_research_eligibility(scoped, market, quote, now)[0]}
    if len(eligible) < 2:
        raise NoSignal("Fewer than two complete, eligible independent sportsbook pairs")
    # Research priors are reused only when they cover *every* contributing
    # book. Otherwise equal weights avoid inventing a ranking for new books.
    priors = [(name, scoped.reference_profiles[name]) for name in scoped.enabled_reference_profiles
        if any(selector.matches(market) for selector in scoped.reference_profiles[name].selectors)]
    if len(priors) > 1:
        raise NoSignal("Ambiguous research weight profiles")
    prior_name, prior = priors[0] if priors else (None, None)
    if prior is not None and eligible <= set(prior.weights_percent):
        raw = {book: prior.weights_percent[book] for book in sorted(eligible)}
        weighting = f"research_prior:{prior_name}"
    else:
        raw = {book: Decimal(1) for book in sorted(eligible)}
        weighting = "neutral_equal_books_unranked"
    total = sum(raw.values())
    weights = {book: value * 100 / total for book, value in raw.items()}
    # Decimal thirds do not sum to exactly 100 after finite division.
    last = next(reversed(weights))
    weights[last] += Decimal(100) - sum(weights.values())
    selector = ProfileSelector(sport=market.sport, league=market.league,
        market_type=market.market_type, statistic=market.statistic)
    profile = ReferenceProfile(selectors=(selector,), phase=phase, weights_percent=weights,
        required_any_anchor=(), min_distinct_books=2, status="research_only",
        live_execution_allowed=False, note="No mandatory sharp-book anchor; never execution approved",
        primary_devig=prior.primary_devig if prior else "power",
        sensitivity_devig=prior.sensitivity_devig if prior else "proportional")
    pilot = scoped.trusted_pilot_policy.model_copy(update={"enabled": False, "owner_approved": False})
    research = scoped.model_copy(update={
        "reference_profile_mode": "strict", "reference_quality_mode": "trusted_provider",
        "reference_profiles": {"multi_book_consensus": profile},
        "enabled_reference_profiles": ["multi_book_consensus"],
        "eligible_books": sorted(eligible), "required_books": [], "weights": weights,
        "min_references": 2, "trusted_pilot_policy": pilot, "market_overrides": {},
    })
    return research, weighting


def live_consensus_scope(market: Market, quotes: list[Quote], config: PricingConfig,
                         now: datetime | None = None) -> PricingConfig:
    """Same complete-pair consensus, under separately approved live weights.

    The research function above never carries this approval. A changing set of
    eligible books renormalizes only the owner's fixed weights; no new weight
    or bookmaker gains authority from discovery alone.
    """
    now = now or utcnow()
    scoped = market_config(config, market)
    policy = scoped.trusted_pilot_policy
    if (not scoped.live_multi_book_enabled or not scoped.multi_book_consensus_enabled
            or not policy.enabled or not policy.owner_approved
            or not policy.recurring_authorization or not policy.permits_market(market)
            or policy.reference_weights_percent is None):
        raise NoSignal("No approved recurring multi-book policy for this market")
    allowed = set(policy.trusted_books)
    eligible = {quote.bookmaker for quote in quotes
        if quote.provider in SOURCES and quote.bookmaker in allowed
        and quote.market_id == market.id and quote.market_fingerprint == market.identity()
        and set(quote.decimal_odds) == set(market.outcomes)
        and trusted_quote_research_eligibility(scoped, market, quote, now)[0]}
    if len(eligible) < policy.min_distinct_books:
        raise NoSignal("Too few current complete approved sportsbook pairs")
    raw = {book: policy.reference_weights_percent[book] for book in sorted(eligible)}
    total = sum(raw.values())
    weights = {book: value * 100 / total for book, value in raw.items()}
    last = next(reversed(weights))
    weights[last] += Decimal(100) - sum(weights.values())
    profile = ReferenceProfile(selectors=(ProfileSelector(sport=market.sport,
        league=market.league, market_type=market.market_type, statistic=market.statistic),),
        phase="inplay" if market.status == "live" else "pregame",
        weights_percent=weights, required_any_anchor=(),
        min_distinct_books=policy.min_distinct_books, status="approved_multi_book",
        live_execution_allowed=False, primary_devig="power",
        sensitivity_devig="proportional",
        note="Owner-approved scoped provider trust; final quote, account and OMS gates apply")
    return scoped.model_copy(update={"reference_profile_mode": "strict",
        "reference_quality_mode": "trusted_provider",
        "reference_profiles": {"approved_multi_book": profile},
        "enabled_reference_profiles": ["approved_multi_book"],
        "eligible_books": sorted(eligible), "required_books": [], "weights": weights,
        "min_references": policy.min_distinct_books, "market_overrides": {}})


def native_live_pricing(market: Market, quotes: list[Quote], config: PricingConfig,
                        now: datetime | None = None) -> PricingConfig:
    """Return the unmodified legacy policy or the explicit live consensus."""
    scoped = market_config(config, market)
    return (live_consensus_scope(market, quotes, config, now)
            if scoped.live_multi_book_enabled and scoped.trusted_pilot_policy.permits_market(market)
            else config)


def consensus_fair_value(market: Market, quotes: list[Quote], config: PricingConfig,
                         now: datetime | None = None):
    now = now or utcnow()
    research, weighting = consensus_scope(market, quotes, config, now)
    filtered = [quote for quote in quotes if quote.provider in SOURCES
                and quote.bookmaker in research.eligible_books]
    fair = fair_value(market, filtered, research, now)
    if fair.execution_eligible or fair.live_execution_allowed:
        raise NoSignal("Research consensus cannot authorize execution")
    return fair, research, weighting


def leave_one_book_out(fair) -> dict[str, dict[str, str]]:
    """Sensitivity to dropping one book, using already devigged evidence."""
    result = {}
    for removed in fair.references:
        remaining = [reference for reference in fair.references if reference is not removed]
        if not remaining:
            continue
        total = sum(Decimal(reference["weight"]) for reference in remaining)
        result[removed["bookmaker"]] = {outcome: str(sum(
            Decimal(reference["probabilities"][outcome]) * Decimal(reference["weight"])
            for reference in remaining) / total) for outcome in fair.probabilities}
    return result
