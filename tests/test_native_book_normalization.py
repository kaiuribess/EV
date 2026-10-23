from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from evplatform.core.models import utcnow
from evplatform.core.native_accounting import NativeInstrument, NativePriceBand
from evplatform.core.native_pricing import NativeFeeBound, instrument_fingerprint
from evplatform.venues.book_normalization import NativeBookMapper, NativeMarketMapping, NativeMappingFile
from evplatform.venues.kalshi import KalshiBook
from evplatform.venues.novig import NovigBook
from evplatform.venues.common import SequenceGap


def mapping(market, venue="kalshi", **kwargs):
    unit = {"kalshi": "contracts", "novig": "whole_cent_contracts", "prophetx": "usd_stake"}[venue]
    instrument = NativeInstrument(
        venue=venue, instrument_id={"kalshi": "TICKER", "novig": "native-market", "prophetx": "strike-home"}[venue],
        canonical_market_id=market.id, outcome="home", native_outcome={"kalshi": "yes", "novig": "outcome-home", "prophetx": "1"}[venue],
        quantity_unit=unit, price_format="american" if venue == "prophetx" else "probability",
        payout_per_unit_usd=None if venue == "prophetx" else "0.01" if venue == "novig" else "1",
        quantity_step="1" if venue == "novig" else "0.01", minimum_quantity="1",
        cash_quantum_usd="0.00001", price_bands=() if venue == "prophetx" else
        (NativePriceBand(minimum="0.001", maximum="0.999", step="0.001"),),
        settlement_rules_fingerprint=market.identity(), verified=True, source="fixture reviewed mapping",
    )
    fee = NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
                         per_unit_usd="0.001", per_fill_usd="0", fixed_order_usd="0", verified=True,
                         source="fixture fee cap", observed_at=utcnow()-timedelta(hours=1), valid_until=utcnow()+timedelta(hours=1))
    return NativeMarketMapping(market=market, instrument=instrument, fee_bound=fee,
                               opposing_outcome="outcome-away" if venue == "novig" else None,
                               source_event_id="12" if venue == "prophetx" else None,
                               source_market_id="22" if venue == "prophetx" else None, **kwargs)


def kalshi_snapshot(ts):
    return {"type": "orderbook_snapshot", "sid": 4, "seq": 2, "sending_ts_ms": ts,
            "msg": {"market_ticker": "TICKER", "yes_dollars_fp": [["0.40", "2.25"]],
                    "no_dollars_fp": [["0.50", "3.50"], ["0.55", "2.00"]]}}


def test_kalshi_preserves_fractional_depth_and_oldest_untouched_clock(market):
    m = mapping(market)
    mapper = NativeBookMapper([m])
    client = SimpleNamespace(book=KalshiBook())
    start = 1_800_000_000_000
    snapshot = kalshi_snapshot(start)
    client.book.apply(snapshot)
    first = mapper.process("kalshi", client, snapshot)[0][1]
    assert [level.quantity for level in first.asks] == [Decimal("3.50"), Decimal("2.00")]
    delta = {"type": "orderbook_delta", "sid": 4, "seq": 3, "sending_ts_ms": start+1100,
             "msg": {"market_ticker": "TICKER", "side": "no", "price_dollars": "0.50",
                     "delta_fp": "1.00", "ts_ms": start+1000}}
    client.book.apply(delta)
    second = mapper.process("kalshi", client, delta)[0][1]
    assert second.asks[0].quantity == Decimal("4.50")
    assert second.source_time == first.source_time
    assert second.provider_time > first.provider_time
    assert mapper.process("kalshi", client, delta) == []
    # Remove the untouched old rung; now the remaining source clock advances.
    delta = {**delta, "seq": 4, "msg": {**delta["msg"], "price_dollars": "0.55", "delta_fp": "-2.00"}}
    client.book.apply(delta)
    assert mapper.process("kalshi", client, delta)[0][1].source_time > first.source_time


def test_kalshi_gap_and_reconnect_require_new_snapshot(market):
    mapper = NativeBookMapper([mapping(market)])
    client = SimpleNamespace(book=KalshiBook())
    snapshot = kalshi_snapshot(1_800_000_000_000)
    client.book.apply(snapshot)
    mapper.process("kalshi", client, snapshot)
    gap = {"type": "orderbook_delta", "sid": 4, "seq": 9,
           "msg": {"market_ticker": "TICKER", "side": "no", "price_dollars": "0.50", "delta_fp": "1.00"}}
    with pytest.raises(SequenceGap):
        client.book.apply(gap)
    invalid = mapper.process("kalshi", client, gap)[0][1]
    assert not invalid.synchronized and not invalid.asks
    client.book.reset()
    client.book.apply(snapshot)
    recovered = mapper.process("kalshi", client, snapshot)[0][1]
    assert recovered.synchronized and recovered.revision > invalid.revision


def novig_snapshot(ts):
    return {"ts": ts, "snapshot": {"native-market": {
        "book": {"seq": 8, "orders": {"outcome-away": [
            {"order": "a", "price": ".335", "qty": 110}, {"order": "b", "price": ".335", "qty": 90}]}},
        "lifecycle": {"seq": 2, "status": "OPEN"}}}}


def test_novig_opposite_liquidity_aggregates_cent_contracts_without_refreshed_old_rows(market):
    mapper = NativeBookMapper([mapping(market, "novig")])
    client = SimpleNamespace(book=NovigBook())
    snapshot = novig_snapshot(1_800_000_000_000)
    client.book.apply(snapshot)
    first = mapper.process("novig", client, snapshot)[0][1]
    assert first.asks[0].price == Decimal(".665") and first.asks[0].quantity == 200
    delta = {"ts": 1_800_000_001_000, "delta": {"native-market": {"book": {"seq": 9, "deltas": [
        {"kind": "remove", "order": "a", "reason": "fill"},
        {"kind": "add", "order": "a", "outcome": "outcome-away", "price": ".335", "qty": 50}]}}}}
    client.book.apply(delta)
    second = mapper.process("novig", client, delta)[0][1]
    assert second.asks[0].quantity == 140 and second.source_time == first.source_time
    assert mapper.process("novig", client, delta) == []
    lifecycle = {"ts": 1_800_000_002_000, "delta": {"native-market": {"lifecycle": {"seq": 3, "deltas": ["CLOSE"]}}}}
    client.book.apply(lifecycle)
    closed = mapper.process("novig", client, lifecycle)[0][1]
    assert closed.status == "closed" and closed.revision > second.revision and not closed.asks


def test_novig_remaining_update_refreshes_only_the_changed_depth_clock(market):
    mapper = NativeBookMapper([mapping(market, "novig")])
    client = SimpleNamespace(book=NovigBook())
    snapshot = novig_snapshot(1_800_000_000_000)
    client.book.apply(snapshot)
    first = mapper.process("novig", client, snapshot)[0][1]
    update_a = {"ts": 1_800_000_001_000, "delta": {"native-market": {"book": {"seq": 9,
        "deltas": [{"kind": "update", "orderId": "a", "remaining": 50}]}}}}
    client.book.apply(update_a)
    second = mapper.process("novig", client, update_a)[0][1]
    assert second.asks[0].quantity == 140 and second.source_time == first.source_time
    update_b = {"ts": 1_800_000_002_000, "delta": {"native-market": {"book": {"seq": 10,
        "deltas": [{"kind": "update", "orderId": "b", "remaining": 30}]}}}}
    client.book.apply(update_b)
    third = mapper.process("novig", client, update_b)[0][1]
    assert third.asks[0].quantity == 80
    assert third.source_time > first.source_time


def prophet_frame(updated_at=1_800_000_000, seq=100):
    return {"event": "v2.market_selections", "scope": {"event_id": 12}, "envelope": {"op": "u"},
            "data": {"sport_event_id": 12, "market_id": 22, "info": {"id": 22, "sequence_number": seq,
                     "selections": [[{"strike_id": "strike-home", "outcome_id": 1, "price": -132,
                                      "value": "1387.08", "quantity": "1536.62", "updated_at": updated_at}],
                                    [{"strike_id": "strike-away", "outcome_id": 2, "price": 120,
                                      "value": "20.00", "updated_at": updated_at}]]}}}


def test_prophetx_uses_actual_stake_and_keeps_undocumented_clock_unknown(market):
    mapper = NativeBookMapper([mapping(market, "prophetx")])
    book = mapper.process("prophetx", None, prophet_frame())[0][1]
    assert book.asks[0].quantity == Decimal("1387.08")
    assert book.asks[0].price == -132
    assert book.source_time is None and book.provider_time is None


def test_prophetx_explicit_clock_unit_scope_and_placeholder_withdrawal(market):
    m = mapping(market, "prophetx", source_timestamp_unit="seconds", timestamp_unit_evidence="reviewed fixture clock")
    mapper = NativeBookMapper([m])
    frame = prophet_frame()
    first = mapper.process("prophetx", None, frame)[0][1]
    assert first.source_time is not None
    assert mapper.process("prophetx", None, frame) == []
    replacement = prophet_frame(seq=101)
    replacement["data"]["info"]["selections"][0][0]["value"] = "0"
    assert not mapper.process("prophetx", None, replacement)[0][1].asks
    frame = prophet_frame(seq=102)
    frame["scope"] = {"event_id": 13}
    invalid = mapper.process("prophetx", None, frame)[0][1]
    assert invalid.status == "suspended" and not invalid.synchronized


def test_prophetx_zero_price_placeholder_and_delete_never_keep_old_depth(market):
    mapper = NativeBookMapper([mapping(market, "prophetx")])
    assert mapper.process("prophetx", None, prophet_frame())[0][1].asks
    placeholder = prophet_frame(seq=101)
    placeholder["data"]["info"]["selections"][0] = [{
        "strike_id": "strike-home", "outcome_id": 1, "price": 0, "value": 0}]
    assert mapper.process("prophetx", None, placeholder)[0][1].asks == ()
    assert mapper.process("prophetx", None, prophet_frame(seq=102))[0][1].asks
    deletion = {"event": "v2.market_selections", "scope": {"event_id": 12},
        "envelope": {"op": "d"}, "data": {"sport_event_id": 12, "market_id": 22,
        "info": {"id": 22, "sequence_number": 103, "selections": []}}}
    invalid = mapper.process("prophetx", None, deletion)[0][1]
    assert not invalid.synchronized and invalid.asks == ()


def test_prophetx_subtype_does_not_match_other_scope(market):
    mapper = NativeBookMapper([mapping(market, "prophetx", prop_sub_type="player_points")])
    assert mapper.process("prophetx", None, prophet_frame()) == []
    frame = prophet_frame(seq=101)
    frame["scope"]["sub_type"] = "player_points"
    assert mapper.process("prophetx", None, frame)


def test_metadata_suspension_and_malformed_update_invalidate_existing_books(market):
    mapper = NativeBookMapper([mapping(market, "prophetx")])
    mapper.process("prophetx", None, prophet_frame())
    invalid = mapper.process("prophetx", None, {"event": "v2.market", "data": {"info": {"status": "inactive"}}})
    assert invalid[0][1].status == "suspended"
    mapper.process("prophetx", None, prophet_frame())
    bad = prophet_frame(seq=101)
    bad["data"]["info"]["selections"][0][0]["value"] = "NaN"
    assert not mapper.process("prophetx", None, bad)[0][1].synchronized
    assert mapper.errors == 1


def test_verified_full_contract_and_fee_binding_required(market):
    m = mapping(market)
    with pytest.raises(ValidationError):
        NativeMarketMapping.model_validate({**m.model_dump(), "market": market.model_copy(update={"line": Decimal("3.5")})})
    with pytest.raises(ValidationError):
        NativeMarketMapping.model_validate({**m.model_dump(), "fee_bound": m.fee_bound.model_copy(update={"verified": False})})
    with pytest.raises(ValidationError):
        mapping(market, "prophetx", source_timestamp_unit="seconds")
    with pytest.raises(ValueError):
        NativeBookMapper([m, m])


def test_mapping_file_roundtrip_and_absent_is_empty(market, tmp_path):
    path = tmp_path / "native-mappings.json"
    assert NativeBookMapper.from_file(path).mappings == ()
    original = NativeBookMapper([mapping(market)])
    path.write_text(NativeMappingFile(mappings=original.mappings).model_dump_json(), encoding="utf-8")
    assert NativeBookMapper.from_file(path).mapping_digest == original.mapping_digest


def test_mapper_rejects_unapplied_new_frame(market):
    mapper = NativeBookMapper([mapping(market)])
    client = SimpleNamespace(book=KalshiBook())
    snapshot = kalshi_snapshot(1_800_000_000_000)
    client.book.apply(snapshot)
    mapper.process("kalshi", client, snapshot)
    unapplied = {"type": "orderbook_delta", "sid": 4, "seq": 3,
                 "msg": {"market_ticker": "TICKER", "side": "no", "price_dollars": "0.50", "delta_fp": "1.00"}}
    assert not mapper.process("kalshi", client, unapplied)[0][1].synchronized


def test_missing_source_clock_never_uses_receipt(market):
    mapper = NativeBookMapper([mapping(market)])
    client = SimpleNamespace(book=KalshiBook())
    snapshot = kalshi_snapshot(None)
    client.book.apply(snapshot)
    result = mapper.process("kalshi", client, snapshot, received_at=utcnow())[0][1]
    assert result.source_time is None and result.provider_time is None and result.received_at is not None


def test_extended_native_mapping_cli_validates_provider_identity(market, tmp_path, capsys):
    import importlib.util
    import json
    from pathlib import Path
    import sys
    from evplatform.integrations.normalization import MappingFile, VerifiedOfferMapping

    spec = importlib.util.spec_from_file_location("mapping_cli_test", Path(__file__).parents[1]/"scripts"/"validate_market_mappings.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    provider = MappingFile(mappings=[VerifiedOfferMapping(signature=char*64, market=market,
                          outcome_map={outcome: outcome for outcome in market.outcomes}, bookmaker=book,
                          verified=True, evidence="fixture reviewed source")
                         for char, book in (("a", "pinnacle"), ("b", "circa"))])
    provider_path, native_path = tmp_path/"provider.json", tmp_path/"native.json"
    provider_path.write_text(provider.model_dump_json(), encoding="utf-8")
    native = NativeMappingFile(mappings=(mapping(market, "novig"),))
    native_path.write_text(native.model_dump_json(), encoding="utf-8")
    assert module.main(["--provider", str(provider_path), "--native", str(native_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["native_mapping_digest"] == NativeBookMapper(native.mappings).mapping_digest
    changed = market.model_copy(update={"line": Decimal("9.5")})
    provider.mappings[:] = [row.model_copy(update={"market": changed}) for row in provider.mappings]
    provider_path.write_text(provider.model_dump_json(), encoding="utf-8")
    assert module.main(["--provider", str(provider_path), "--native", str(native_path)]) == 1
    assert "native.provider_fingerprint_mismatch" in capsys.readouterr().out
