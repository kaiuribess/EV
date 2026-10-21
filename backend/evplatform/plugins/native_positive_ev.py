"""Native-unit strategy; emits candidates only, never accesses an order router."""
from ..core.config import PricingConfig
from ..core.native_pricing import native_opportunity, native_revalidate_intent
from ..core.plugins import Manifest, StrategyPlugin
from ..core.pricing import NoSignal, fair_value


class NativePositiveEV(StrategyPlugin):
    config_model = PricingConfig
    manifest = Manifest(
        name="native_positive_ev", kind="strategy",
        description="Reviewed native-unit opportunities with conservative fee and settlement bounds",
        sports=["*"], markets=["moneyline", "spread", "total", "player_prop"],
        input_schemas=["odds.quote@1", "exchange.native_book@1", "sports.market@1"],
        output_schemas=["trade.native_candidate@1"],
        capabilities={"devig": ["proportional", "power"], "direct_order_access": False,
                      "quantity_units": ["fractional_contracts", "whole_cent_contracts", "usd_stake"],
                      "requires_reviewed_mapping": True, "requires_fee_bounds": True,
                      "requires_settlement_probability_bounds": True,
                      "synthetic_execution_allowed": False},
        config_schema=PricingConfig.model_json_schema(),
    )

    async def evaluate(self, event, view):
        fair = fair_value(view["market"], view["quotes"], view["config"])
        candidates, errors = [], []
        for book in view["native_books"]:
            try:
                candidates.append(native_opportunity(view["market"], fair, book, view["config"],
                    max_stake_usd=view["max_stake_usd"], mode=view["mode"],
                    stake_sizing=view.get("stake_sizing", "capped"), unit_size_usd=view.get("unit_size_usd", "5"),
                    available_cash_usd=view.get("account_cash", {}).get(book.instrument.venue)))
            except NoSignal as exc:
                errors.append(str(exc))
        if errors and not candidates:
            raise NoSignal("; ".join(dict.fromkeys(errors)))
        return candidates

    def revalidate(self, candidate, view):
        fair = fair_value(view["market"], view["quotes"], view["config"])
        return native_revalidate_intent(candidate.intent, view["market"], fair, view["native_book"],
            view["config"], remaining_quantity=view.get("remaining_quantity"),
            fees_already_paid_usd=view.get("fees_already_paid_usd", 0))
