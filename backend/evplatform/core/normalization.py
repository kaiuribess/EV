from dataclasses import dataclass

from .models import Market


@dataclass(frozen=True)
class VerifiedAlias:
    kind: str
    provider: str
    external_id: str
    canonical_id: str
    evidence: str


class MarketMatcher:
    """No fuzzy auto-linking. Unverified identities remain quarantined."""
    def __init__(self):
        self.aliases: dict[tuple[str, str, str], VerifiedAlias] = {}
        self.markets: dict[str, Market] = {}
        self.identities: dict[str, str] = {}
        self.quarantine: list[dict] = []

    def add_alias(self, alias: VerifiedAlias):
        if not alias.evidence.strip():
            raise ValueError("Verified aliases require evidence")
        key = (alias.kind, alias.provider, alias.external_id)
        if key in self.aliases and self.aliases[key].canonical_id != alias.canonical_id:
            raise ValueError("Conflicting alias")
        self.aliases[key] = alias

    def resolve(self, kind: str, provider: str, external_id: str):
        match = self.aliases.get((kind, provider, external_id))
        if match:
            return match.canonical_id
        self.quarantine.append({"kind": kind, "provider": provider, "external_id": external_id,
                                "reason": "Unverified identity"})
        return None

    def register(self, market: Market):
        if market.id in self.markets and self.markets[market.id].identity() != market.identity():
            raise ValueError("Canonical ID collision with incompatible market rules")
        existing = self.identities.get(market.identity())
        if existing:
            return existing
        self.markets[market.id] = market
        self.identities[market.identity()] = market.id
        return market.id

    @staticmethod
    def equivalent(first: Market, second: Market) -> bool:
        return first.identity() == second.identity()

