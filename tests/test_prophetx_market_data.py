"""ProphetX read-only production-data path; fixtures never place orders."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from evplatform.core.native_runtime import NativeRuntime
from evplatform.venues.prophetx import ProphetXClient, ProphetXSettings
from evplatform.venues.prophetx_market_data import ProphetXMarketData


NOW = datetime(2026, 10, 23, 5, 15, tzinfo=timezone.utc)


def catalog(status="active"):
    return {"data": {"123": [
        {"id": 219, "name": "Moneyline", "sub_type": "moneyline", "status": status,
         "selections": [[{"strike_id": "home-strike", "outcome_id": 4, "name": "Home"}],
                        [{"strike_id": "away-strike", "outcome_id": 5, "name": "Away"}]]},
        {"id": 300, "name": "Player Double Double", "sub_type": "player_to_record_a_double_double",
         "status": "active", "selections": [[{"strike_id": "prop-yes", "outcome_id": 1, "name": "Yes"}],
                                               [{"strike_id": "prop-no", "outcome_id": 2, "name": "No"}]]},
    ]}}


def frame(market=219, *, subtype=None, sequence=1, op="u"):
    if market == 219:
        selections = [[{"strike_id": "home-strike", "outcome_id": 4, "price": -132,
                        "value": "1387.08", "quantity": "1536.62", "updated_at": 1792731906663622000},
                       {"strike_id": "home-strike", "outcome_id": 4, "price": -135,
                        "value": "12.50", "quantity": "900.00", "updated_at": 1792731906663622000}],
                      [{"strike_id": "away-strike", "outcome_id": 5, "price": 120,
                        "value": "20.00", "quantity": "45.00", "updated_at": 1792731906663622000}]]
    else:
        selections = [[{"strike_id": "prop-yes", "outcome_id": 1, "price": 300,
                        "value": "3.25", "quantity": "500.00", "updated_at": 1792731906663622000}],
                      [{"strike_id": "prop-no", "outcome_id": 2, "price": -400,
                        "value": "10.00", "updated_at": 1792731906663622000}]]
    return {"event": "v2.market_selections", "scope": {"event_id": 123, **({"sub_type": subtype} if subtype else {})},
            "envelope": {"op": op}, "data": {"sport_event_id": 123, "market_id": market,
            "info": {"id": market, "status": "active", "sequence_number": sequence, "selections": selections}}}


def seeded():
    data = ProphetXMarketData()
    data.replace_catalog([catalog()], ["123"], observed_at=NOW)
    return data


def test_talaria_value_is_usd_stake_not_legacy_quantity_and_multiple_levels():
    data = seeded()
    assert data.snapshot(now=NOW, connected=True)["levels"] == []  # REST identity alone is not depth
    data.apply(frame(), received_at=NOW)
    view = data.snapshot(now=NOW + timedelta(seconds=1), connected=True)
    assert view["current_game_markets"] == 1
    assert len(view["levels"]) == 3
    home = next(row for row in view["levels"] if row["american_price"] == "-132")
    assert home["available_stake_usd"] == "1387.08"
    assert home["executable_stake_usd"] == "1387.08"
    assert home["gross_payout_per_usd_stake"] == str(Decimal(1) + Decimal(100) / 132)
    assert home["updated_at_raw"] == 1792731906663622000
    assert home["research_only"] and home["market_status"] == "active"
    assert {row["strike_id"] for row in view["levels"]} == {"home-strike", "away-strike"}


def test_exact_market_filter_runs_before_level_limit_without_refreshing_book():
    data = seeded()
    data.apply(frame(), received_at=NOW)
    view = data.snapshot(now=NOW + timedelta(seconds=2), connected=True, limit=1,
                         event_id="123", market_id="219", strike_id="home-strike")
    assert view["filtered_level_count"] == 2
    assert len(view["levels"]) == 1
    assert view["levels"][0]["strike_id"] == "home-strike"
    assert view["levels"][0]["executable_stake_usd"] == "1387.08"
    assert view["current_game_markets"] == 1
    assert data.snapshot(now=NOW + timedelta(seconds=31), connected=True, limit=1,
                         event_id="123", market_id="219", strike_id="home-strike")["levels"][0][
                             "executable_stake_usd"] == "0"


def test_prop_scope_must_match_catalog_and_exact_strike_identity():
    data = seeded()
    data.apply(frame(300, subtype="player_to_record_a_double_double"), received_at=NOW)
    assert data.snapshot(now=NOW, connected=True)["current_player_prop_markets"] == 1
    assert data.snapshot(now=NOW, connected=True, limit=1)["current_player_prop_samples"][0]["executable_stake_usd"] != "0"
    wrong_scope = frame(300, subtype="player_total_points")
    data.apply(wrong_scope, received_at=NOW)
    assert data.rejected_frames == 1
    assert data.snapshot(now=NOW, connected=True)["current_player_prop_markets"] == 0
    data.replace_catalog([catalog()], ["123"], observed_at=NOW)
    wrong_strike = frame(300, subtype="player_to_record_a_double_double")
    wrong_strike["data"]["info"]["selections"][0][0]["strike_id"] = "unreviewed"
    data.apply(wrong_strike, received_at=NOW)
    assert data.rejected_frames == 2
    assert data.snapshot(now=NOW, connected=True)["current_player_prop_markets"] == 0


def test_replacement_placeholder_delete_disconnect_and_expiry_remove_executable_depth():
    data = seeded()
    data.apply(frame(), received_at=NOW)
    assert data.snapshot(now=NOW + timedelta(seconds=31), connected=True)["levels"][0]["executable_stake_usd"] == "0"
    assert data.snapshot(now=NOW, connected=False)["levels"][0]["executable_stake_usd"] == "0"
    placeholder = frame(sequence=2)
    placeholder["data"]["info"]["selections"] = [[{"strike_id": "home-strike", "value": 0, "price": 0}], []]
    data.apply(placeholder, received_at=NOW)
    assert data.snapshot(now=NOW, connected=True)["levels"] == []
    data.apply(frame(sequence=3), received_at=NOW)
    data.apply(frame(sequence=4, op="d"), received_at=NOW)
    assert data.snapshot(now=NOW, connected=True)["levels"] == []
    data.apply(frame(sequence=5), received_at=NOW)
    data.invalidate()
    assert data.snapshot(now=NOW, connected=True)["levels"] == []


def test_suspended_market_and_status_change_block_depth():
    data = ProphetXMarketData()
    data.replace_catalog([catalog("suspended")], ["123"], observed_at=NOW)
    data.apply(frame(), received_at=NOW)
    assert all(row["executable_stake_usd"] == "0" for row in data.snapshot(now=NOW, connected=True)["levels"])
    data.replace_catalog([catalog()], ["123"], observed_at=NOW)
    assert not data.books  # changed status cannot inherit earlier levels
    data.apply(frame(), received_at=NOW)
    data.apply({"event": "v2.market", "data": {"info": {"status": "suspended"}}}, received_at=NOW)
    assert not data.books
    assert data.catalog_at is None


def test_explicitly_suspended_selection_is_not_executable():
    payload = catalog()
    payload["data"]["123"][0]["selections"][0][0]["status"] = "suspended"
    data = ProphetXMarketData()
    data.replace_catalog([payload], ["123"], observed_at=NOW)
    data.apply(frame(), received_at=NOW)
    rows = data.snapshot(now=NOW, connected=True)["levels"]
    assert all(row["executable_stake_usd"] == "0" for row in rows if row["strike_id"] == "home-strike")
    assert any(row["executable_stake_usd"] == "20.00" for row in rows if row["strike_id"] == "away-strike")


def test_catalog_without_selection_rows_accepts_scoped_stream_identity_for_research_only():
    payload = catalog()
    payload["data"]["123"][0]["selections"] = None
    data = ProphetXMarketData()
    data.replace_catalog([payload], ["123"], observed_at=NOW)
    data.apply(frame(), received_at=NOW)
    view = data.snapshot(now=NOW, connected=True)
    assert view["current_game_markets"] == 1
    assert all(row["identity_evidence"] == "scoped_v2_stream_only" and row["research_only"]
               for row in view["levels"])
    stopped = frame(sequence=2)
    stopped["data"]["info"]["status"] = "suspended"
    data.apply(stopped, received_at=NOW)
    assert all(row["executable_stake_usd"] == "0" for row in data.snapshot(now=NOW, connected=True)["levels"])


def test_rest_prop_seed_preserves_price_and_value_without_claiming_live_depth():
    payload = catalog()
    payload["data"]["123"][1]["selections"][0][0].update({
        "price": 300, "value": "3.25", "quantity": "500", "updated_at": 1792731906663622000})
    data = ProphetXMarketData()
    data.replace_catalog([payload], ["123"], observed_at=NOW)
    view = data.snapshot(now=NOW, connected=True)
    assert view["rest_player_prop_market_count"] == 1
    assert view["current_player_prop_markets"] == 0
    assert view["levels"] == []
    sample = view["rest_player_prop_samples"][0]
    assert sample["american_price"] == "300" and sample["rest_advertised_stake_usd"] == "3.25"
    assert sample["gross_payout_per_usd_stake"] == "4"
    assert sample["executable_stake_usd"] == "0" and not view["rest_price_observation_verified"]


def test_native_research_read_uses_the_existing_monitor_and_never_calls_placement():
    client = ProphetXClient(ProphetXSettings(environment="production"))
    client.public_market_data.replace_catalog([catalog()], ["123"], observed_at=NOW)
    client.public_market_data.apply(frame(), received_at=NOW)
    client.health_check = lambda: {"streaming": True}
    monitor = SimpleNamespace(client=client, task=SimpleNamespace(done=lambda: False))
    engine = SimpleNamespace(registry=SimpleNamespace(records={"prophetx": SimpleNamespace(
        enabled=True, plugin=monitor)}))
    view = NativeRuntime.prophetx_research_books(SimpleNamespace(engine=engine), limit=10)
    assert view["available"] and view["read_only"] and not view["execution_mapping_verified"]
    assert view["levels"] and view["levels"][0]["strike_id"]
