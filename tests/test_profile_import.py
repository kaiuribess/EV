import copy
import json
from pathlib import Path

import pytest

from evplatform.core.profile_import import INITIAL_PROFILES, compile_priors


@pytest.fixture
def spec():
    return json.loads((Path(__file__).parents[1] / "config/book_weight_priors_2026-10-20.json").read_text())


def test_owner_profiles_preserved_and_execution_permissions_not_invented(spec):
    config = compile_priors(spec)
    assert len(config.reference_profiles) == 11
    assert config.enabled_reference_profiles == list(INITIAL_PROFILES)
    assert config.reference_profile_mode == "strict"
    assert config.profile_freshness_rules == []
    for name, profile in config.reference_profiles.items():
        assert profile.weights_percent == spec["profiles"][name]["weights_percent"]
        assert sum(profile.weights_percent.values()) == 100
        assert profile.live_execution_allowed is False
        assert profile.sensitivity_devig == "proportional"


def test_college_union_is_intersected_with_sport_vocabulary(spec):
    selectors = compile_priors(spec).reference_profiles["college_props_experimental"].selectors
    assert len(selectors) == 9
    assert all(s.statistic != "passing_yards" for s in selectors if s.league == "NCAABM")
    assert all(s.statistic != "points" for s in selectors if s.league == "NCAAF")


@pytest.mark.parametrize("change", ["live", "ttl", "unknown", "weight"])
def test_unsafe_or_unknown_spec_is_rejected(spec, change):
    spec = copy.deepcopy(spec)
    if change == "live":
        spec["profiles"]["nba_core_props"]["live_execution_allowed"] = True
    elif change == "ttl":
        spec["freshness_contract"]["ttl_seconds_by_provider_book_sport_market_phase"] = {"guessed": 5}
    elif change == "unknown":
        spec["profiles"]["nba_core_props"]["canonical_markets"].append("unknown")
    else:
        spec["profiles"]["nba_core_props"]["weights_percent"]["fanduel"] = 55
    with pytest.raises(ValueError):
        compile_priors(spec)
