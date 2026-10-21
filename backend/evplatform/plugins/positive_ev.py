from ..core.config import PricingConfig
from ..core.plugins import Manifest, StrategyPlugin
from ..core.pricing import NoSignal, fair_value, opportunity


class PositiveEV(StrategyPlugin):
    config_model = PricingConfig
    manifest = Manifest(
        name="positive_ev", kind="strategy", description="Independent, fee-aware sharp reference consensus",
        sports=["*"], markets=["moneyline", "spread", "total", "player_prop"],
        input_schemas=["odds.quote@1", "exchange.book@1", "sports.market@1"],
        output_schemas=["trade.candidate@1"],
        capabilities={"devig": ["proportional", "power"], "payout": "$1 binary",
                      "direct_order_access": False, "different_line_model": False},
        config_schema=PricingConfig.model_json_schema(),
    )

    async def evaluate(self, event, view):
        market = view["market"]
        if market.extensions.get("paper_only"):
            return []
        config = view["config"]
        fair = fair_value(market, view["quotes"], config)
        candidates, errors = [], []
        for book in view["books"]:
            try:
                candidates.append(opportunity(market, fair, book, config))
            except NoSignal as exc:
                errors.append(str(exc))
        if errors and not candidates:
            raise NoSignal("; ".join(dict.fromkeys(errors)))
        return candidates

    def revalidate(self, candidate, view):
        if view["market"].extensions.get("paper_only"):
            raise NoSignal("Paper-only assumed markets require their dedicated strategy")
        fair = fair_value(view["market"], view["quotes"], view["config"])
        return opportunity(view["market"], fair, view["book"], view["config"])

