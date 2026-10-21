"""Private stream/REST fences; entirely offline, never account certification."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import httpx
import time
from unittest.mock import AsyncMock

from evplatform.core.models import utcnow
from evplatform.venues.account_probe import NativeAccountSnapshot
from evplatform.venues.prophetx import ProphetXClient, ProphetXSettings
from evplatform.venues.prophetx_account import probe_prophetx_account
from evplatform.venues.prophetx_financial import ProphetXHistory, prophetx_fingerprint
from evplatform.venues.prophetx_private import ProphetXPrivateState
from evplatform.venues.reconciliation import ReconciliationError
from test_prophetx_financial import case, Client, NOW, USER, OID


def active(*, max_orders=10):
    clock = [100.0]
    channel = {"channel_name": "server-generated", "binding_events": [
        {"name": "orders-version-from-server", "type": "orders"},
        {"name": "health-version-from-server", "type": "private_system_signal"}]}
    client = SimpleNamespace(settings=SimpleNamespace(environment="production"), _ws=object(),
        _signin_confirmed=True, _authenticated_user_id=USER, channels={channel["channel_name"]: channel},
        _confirmed_channels={channel["channel_name"]}, health_check=lambda: {"authenticated": True})
    state = ProphetXPrivateState(max_orders=max_orders, monotonic=lambda: clock[0])
    client.private = state
    state.register([channel], user=USER, environment="production")
    return state, client, clock


def heartbeat(state, client):
    return state.observe({"channel": "server-generated", "event": "health-version-from-server", "data": {},
        "envelope": {"change_type": "private_system_signal", "timestamp": 1730000000000000000}}, client=client)


def order_frame(sequence=12, oid=OID):
    return {"channel": "server-generated", "event": "orders-version-from-server", "data": {"info": {
        "id": oid, "user_id": USER, "external_id": "client-order", "strike_id": "strike",
        "market_id": 1, "sport_event_id": 2, "outcome_id": 3,
        "status": "open", "matching_status": "fully_matched", "winning_status": "tbd",
        "quantity": Decimal("5"), "open_quantity": Decimal("0"), "total_filled_quantity": Decimal("5"),
        "filled_quantity": Decimal("1"), "profit": Decimal("6"), "price": Decimal("120"),
        "sequence_number": sequence}}}


def history(frame, now):
    row = deepcopy(frame["data"]["info"])
    row["order_id"] = row.pop("id")
    row["filled_quantity"] = row.pop("total_filled_quantity")
    row["updated_at"] = (now-timedelta(seconds=1)).isoformat()
    return ProphetXHistory((row,), now)


def certify(state, client, data=None):
    now = utcnow()
    captured = state.fence(client=client)
    watermark = state.reconcile_history(data or ProphetXHistory((), now),
        started_at=now, captured_fence=captured, client=client, observed_at=now)
    return NativeAccountSnapshot(venue="prophetx", environment="production", observed_at=now,
        account_fingerprint=prophetx_fingerprint("production", USER), identity_verified=True,
        reconciled=True, cash_available_verified=True, private_connection_generation=captured[0],
        private_revision=captured[1], private_as_of=watermark)


def test_heartbeat_never_establishes_an_inventory_baseline():
    state, client, _ = active()
    assert state.fence(client=client) is None
    assert heartbeat(state, client)
    assert state.fence(client=client) is not None
    assert not state.matches(None, client=client)
    assert state.matches(certify(state, client), client=client)


@pytest.mark.parametrize("field,value", [("_signin_confirmed", False), ("_ws", None),
    ("_authenticated_user_id", "different"), ("_confirmed_channels", set())])
def test_exact_account_and_subscription_confirmation_required(field, value):
    state, client, _ = active()
    heartbeat(state, client)
    snapshot = certify(state, client)
    setattr(client, field, value)
    assert not state.matches(snapshot, client=client)


def test_registration_is_based_on_types_not_channel_or_event_names():
    state, client, _ = active()
    assert heartbeat(state, client)
    assert state.fence(client=client)
    client.channels["server-generated"]["binding_events"][1]["type"] = "market_selections"
    assert state.fence(client=client) is None


@pytest.mark.parametrize("change", ["no_orders", "duplicate", "public_scope", "extra_type"])
def test_ambiguous_private_registration_never_provides_a_fence(change):
    state, client, _ = active()
    rows = deepcopy(list(client.channels.values()))
    if change == "no_orders": rows[0]["binding_events"] = rows[0]["binding_events"][1:]
    if change == "duplicate": rows.append(deepcopy(rows[0]))
    if change == "public_scope": rows[0]["scope"] = {"event_id": 123}
    if change == "extra_type": rows[0]["binding_events"].append({"name": "other", "type": "other"})
    state.register(rows, user=USER, environment="production")
    assert state.invalid and state.fence(client=client) is None


def test_heartbeat_gap_and_reconnect_cannot_revive_old_snapshot():
    state, client, clock = active()
    heartbeat(state, client)
    snapshot = certify(state, client)
    clock[0] += 11
    heartbeat(state, client)
    assert not state.matches(snapshot, client=client)
    new = certify(state, client)
    assert state.matches(new, client=client)
    state.register(list(client.channels.values()), user=USER, environment="production")
    heartbeat(state, client)
    assert not state.matches(new, client=client)


def test_order_tokens_allow_gaps_but_updates_invalidate_baseline():
    state, client, _ = active()
    heartbeat(state, client)
    snapshot = certify(state, client)
    first = order_frame(12)
    assert state.observe(first, client=client)
    assert not state.matches(snapshot, client=client)
    second = order_frame(1709130007835319000)
    assert state.observe(second, client=client)
    now = utcnow()
    captured = state.fence(client=client)
    assert state.reconcile_history(history(second, now), started_at=now, captured_fence=captured,
                                   client=client, observed_at=now) == now
    assert not state.invalid  # Token gaps are not exchange sequence gaps.


@pytest.mark.parametrize("field,value", [("user_id", "other"), ("sequence_number", 1.5),
    ("sequence_number", True), ("price", 120.0), ("total_filled_quantity", "9"), ("id", "")])
def test_malformed_private_updates_invalidate_before_parsing(field, value):
    state, client, _ = active()
    heartbeat(state, client)
    snapshot = certify(state, client)
    frame = order_frame()
    frame["data"]["info"][field] = value
    assert not state.observe(frame, client=client)
    assert state.invalid and not state.matches(snapshot, client=client)


def test_conflicting_token_and_capacity_do_not_silently_evict_evidence():
    state, client, _ = active(max_orders=1)
    heartbeat(state, client)
    assert state.observe(order_frame(), client=client)
    frame = order_frame()
    frame["data"]["info"]["price"] = 125
    assert not state.observe(frame, client=client) and state.invalid
    state, client, _ = active(max_orders=1)
    heartbeat(state, client)
    state.observe(order_frame(), client=client)
    assert not state.observe(order_frame(13, "second"), client=client)
    assert state.invalid and list(state.orders) == [OID]


@pytest.mark.parametrize("change", ["lagging", "missing", "price", "filled", "future_update", "changed_fence"])
def test_history_must_cover_start_and_exact_latest_private_order(change):
    state, client, _ = active()
    heartbeat(state, client)
    frame = order_frame()
    state.observe(frame, client=client)
    now = utcnow()
    data = history(frame, now)
    captured = state.fence(client=client)
    if change == "lagging": data = ProphetXHistory(data.rows, now-timedelta(microseconds=1))
    if change == "missing": data = ProphetXHistory((), now)
    if change == "price": data.rows[0]["price"] = "125"
    if change == "filled": data.rows[0]["filled_quantity"] = "1"  # Per-fill != cumulative.
    if change == "future_update": data.rows[0]["updated_at"] = (now+timedelta(seconds=1)).isoformat()
    if change == "changed_fence": state.observe(order_frame(13), client=client)
    with pytest.raises(ReconciliationError):
        state.reconcile_history(data, started_at=now, captured_fence=captured, client=client, observed_at=now)
    assert state.baseline is None


async def test_account_probe_sets_local_fence_only_after_full_history(case):
    order, client, review = case
    snapshot = await probe_prophetx_account(client, [order], review=review, observed_at=NOW)
    assert snapshot.reconciled and snapshot.private_as_of == NOW
    assert client.private.matches(snapshot, client=client)
    client.private.reset()
    assert not client.private.matches(snapshot, client=client)


async def test_account_probe_blocks_old_reporting_watermark_even_if_recent(case):
    order, client, review = case
    original = client.request
    async def old(*args, **kwargs):
        value = await original(*args, **kwargs)
        value["last_synced_at"] = (NOW-timedelta(seconds=1)).isoformat()
        return value
    client.request = old
    snapshot = await probe_prophetx_account(client, [order], review=review, observed_at=NOW)
    assert not snapshot.reconciled and not snapshot.cash_available_verified and snapshot.private_as_of is None


async def test_account_probe_missing_heartbeat_does_not_certify_empty_history(case):
    order, client, review = case
    client.private.last_heartbeat = None
    snapshot = await probe_prophetx_account(client, [order], review=review, observed_at=NOW)
    assert not snapshot.reconciled and "heartbeat" in snapshot.reason


async def test_public_price_updates_do_not_invalidate_private_account_probe(case):
    order, client, review = case
    original = client.request
    async def public_update(*args, **kwargs):
        client._last_sequences["public-market"] = client._last_sequences.get("public-market", 0) + 100000
        return await original(*args, **kwargs)
    client.request = public_update
    snapshot = await probe_prophetx_account(client, [order], review=review, observed_at=NOW)
    assert snapshot.reconciled and client.private.matches(snapshot, client=client)


def test_failed_new_probe_does_not_leave_previous_baseline_usable():
    state, client, _ = active()
    heartbeat(state, client)
    snapshot = certify(state, client)
    assert state.matches(snapshot, client=client)
    state.begin_probe(client=client)
    assert not state.matches(snapshot, client=client)


def test_unknown_private_system_payload_does_not_refresh_heartbeat_or_baseline():
    state, client, _ = active()
    heartbeat(state, client)
    snapshot = certify(state, client)
    pulse = {"channel": "server-generated", "event": "health-version-from-server", "data": {"balance": "123"}}
    assert not state.observe(pulse, client=client)
    assert state.invalid and not state.matches(snapshot, client=client)


def test_non_object_registered_binding_is_rejected_without_a_false_private_fence():
    state, client, _ = active()
    channels = deepcopy(list(client.channels.values()))
    channels[0]["binding_events"].append("untrusted")
    state.register(channels, user=USER, environment="production")
    assert state.invalid and state.fence(client=client) is None


async def test_client_register_accept_and_disconnect_manage_private_fence():
    _, reference, _ = active()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Offline only"))) as http:
        client = ProphetXClient(ProphetXSettings(environment="production"), http)
        client._ws = SimpleNamespace(close=AsyncMock())
        client.authenticated, client._access_expires = True, time.time()+3600
        client._send_ws = AsyncMock()
        client.request = AsyncMock(return_value={"data": {"success": True, "channel_count": 1, "channel_limit": 4,
            "authorized_channel": list(reference.channels.values()),
            "authenticated": {"auth": "OFFLINE ONLY", "user_data": '{"id":"'+USER+'"}'}}})
        await client._register("OFFLINE-SOCKET")
        assert client.private.user == USER and client.private.fence(client=client) is None
        client._signin_confirmed, client.health = True, "streaming"
        client._confirmed_channels = {"server-generated"}
        pulse = {"channel": "server-generated", "event": "health-version-from-server", "data": {}}
        assert client.accept_message(pulse)
        assert pulse["private_event_type"] == "private_system_signal"
        snapshot = certify(client.private, client)
        update = order_frame()
        assert client.accept_message(update)
        assert update["private_event_type"] == "orders"
        assert not client.private.matches(snapshot, client=client)
        generation = client.private.generation
        await client.disconnect()
        assert client.private.generation > generation and client.private.fence(client=client) is None
