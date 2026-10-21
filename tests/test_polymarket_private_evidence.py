"""Offline wire-shape collection; never order or paid settlement evidence."""
import asyncio
from types import SimpleNamespace

import pytest

from evplatform.venues.polymarket_us_private_evidence import capture_private_evidence, private_field_presence


class ReadOnlyClient:
    venue_id = "polymarket_us"
    authenticated = True
    settings = SimpleNamespace(environment="production")
    market_slugs = []
    private_updates = True

    def __init__(self, frames, *, wait=False):
        self.frames, self.wait, self.closed = frames, wait, False

    async def stream_updates(self):
        try:
            for frame in self.frames:
                yield frame
            if self.wait:
                await asyncio.Event().wait()
        finally:
            self.closed = True


def frame(**changes):
    return {"type": "polymarket_us_monitor", "channel": "private", "kind": "data", "payload": {
        "accountBalancesUpdate": {"balanceChange": {"id": "OFFLINE-ledger-id", "accountName": "OFFLINE-account",
            "modifiedSecurityId": "", "entryType": "LEDGER_ENTRY_TYPE_RESOLUTION", **changes}}}}


async def test_sdk_ids_are_retained_privately_but_summary_contains_only_presence():
    client = ReadOnlyClient([frame()])
    result = await capture_private_evidence(client)
    assert client.closed
    assert result["frames"][0]["frame"]["payload"]["accountBalancesUpdate"]["balanceChange"]["id"] == "OFFLINE-ledger-id"
    summary = result["summary"]
    assert summary["field_presence"]["accountBalancesUpdate.id:nonempty_string"] == 1
    assert summary["field_presence"]["accountBalancesUpdate.modifiedSecurityId:blank"] == 1
    assert "OFFLINE-account" not in str(summary)
    assert not summary["cash_posting_join_verified"] and not summary["replay_coverage_verified"]
    assert summary["orders_submitted"] == 0


@pytest.mark.parametrize("values,status", [({}, "absent"), ({"id": None}, "null"),
    ({"id": ""}, "blank"), ({"id": 4}, "invalid_type_or_blank"), ({"id": " "}, "invalid_type_or_blank")])
def test_no_identity_inferred_from_absent_blank_or_malformed_fields(values, status):
    result = private_field_presence({"accountBalancesUpdate": {"balanceChange": values}})
    assert result["accountBalancesUpdate.id"] == status


async def test_frame_limit_closes_stream_and_does_not_imply_complete_history():
    client = ReadOnlyClient([frame(), frame()])
    result = await capture_private_evidence(client, max_frames=1)
    assert result["summary"]["stopped"] == "frame_bound"
    assert len(result["frames"]) == 1 and client.closed
    assert not result["summary"]["replay_coverage_verified"]


async def test_byte_limit_does_not_store_partial_frame():
    client = ReadOnlyClient([frame(description="x"*2048)])
    result = await capture_private_evidence(client, max_bytes=1024)
    assert result["summary"]["stopped"] == "byte_bound" and not result["frames"] and client.closed


async def test_timeout_closes_waiting_stream():
    client = ReadOnlyClient([frame()], wait=True)
    result = await capture_private_evidence(client, seconds=1)
    assert result["summary"]["stopped"] == "time_bound" and client.closed


async def test_disconnect_evidence_retained_not_hidden_by_a_later_snapshot():
    client = ReadOnlyClient([{"type": "transport_invalidated"}, frame()])
    result = await capture_private_evidence(client)
    assert result["summary"]["counts"]["transport_invalidated"] == 1
    assert not result["summary"]["replay_coverage_verified"]


async def test_capture_retains_only_subscription_status_before_teardown():
    client = ReadOnlyClient([frame()])
    client.health_check = lambda: {"private_subscription_evidence": {
        "SUBSCRIPTION_TYPE_ORDER": "data_observed", "SUBSCRIPTION_TYPE_POSITION": "awaiting_data",
        "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE": "secret-account-id", "private": "private-secret"}}
    result = await capture_private_evidence(client)
    assert result["summary"]["subscription_evidence"] == {
        "SUBSCRIPTION_TYPE_ORDER": "data_observed", "SUBSCRIPTION_TYPE_POSITION": "awaiting_data"}
    assert "secret" not in str(result["summary"])


@pytest.mark.parametrize("change", [{"authenticated": False}, {"market_slugs": ["outside-scope"]},
    {"settings": SimpleNamespace(environment="sandbox")}, {"private_updates": False}])
async def test_invalid_account_or_subscription_scope_rejected_before_read(change):
    client = ReadOnlyClient([])
    for key, value in change.items():
        setattr(client, key, value)
    with pytest.raises(ValueError):
        await capture_private_evidence(client)
    assert not client.closed
