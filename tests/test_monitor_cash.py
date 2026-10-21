"""Balance display tests use fake clients only; no account calls or credentials."""
import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from evplatform.core.models import utcnow
from evplatform.core.monitor_cash import MonitorCashService, observe_monitor_cash


KEY = "00000000-0000-4000-8000-000000000001"


class Client:
    def __init__(self, venue="novig", environment="sandbox", raw=None):
        self.venue_id = venue
        self.settings = SimpleNamespace(environment=environment, api_key=KEY, api_key_id=KEY)
        self.authenticated = True
        self.calls = []
        self.raw = {"balance": "25.00", "keyId": KEY} if raw is None else raw

    def health_check(self):
        return {"authenticated": self.authenticated}

    async def request(self, method, path, **kwargs):
        assert method == "GET"
        self.calls.append((method, path, kwargs))
        return self.raw

    async def fetch_balance(self):
        return await self.request("GET", "/v4/mm/get_balance")

    async def account_balances(self):
        return await self.request("GET", "/v1/account/balances")


async def test_no_profile_needed_one_read_no_inventory_and_no_kelly_authorization():
    client = Client()
    service = MonitorCashService(lambda: {"novig": client})
    assert service.state()["accounts"]["novig"]["reported_balance_usd"] is None
    report = await service.refresh("novig")
    row = report["accounts"]["novig"]
    assert row["reported_balance_usd"] == "25.00" and row["identity_verified"]
    assert row["environment"] == "sandbox" and row["money_type"] == "play"
    assert not row["cash_available_verified"] and not row["available_for_kelly"]
    assert client.calls == [("GET", f"/v3/account/subaccounts/{KEY}/balance", {})]
    assert KEY not in json.dumps(report)


async def test_primary_kalshi_cash_not_current_available_or_identity_certificate():
    client = Client("kalshi", "production", {"balance_dollars": "80.1234", "secret": "never display"})
    row = (await MonitorCashService(lambda: {"kalshi": client}).refresh())["accounts"]["kalshi"]
    assert row["reported_balance_usd"] == "80.1234" and not row["identity_verified"]
    assert row["source_clock_status"] == "missing" and row["money_type"] == "real"
    assert client.calls == [("GET", "/portfolio/balance", {"params": {"subaccount": 0}})]
    assert "never display" not in json.dumps(row)


async def test_prophetx_exposure_credit_and_unmatched_clock_do_not_certify_cash():
    client = Client("prophetx", "production", {"data": {"balance": "30.25", "gec_balance": "2",
        "unmatched_order_balance": "5", "unmatched_order_balance_status": "succeed",
        "unmatched_order_last_synced_at": utcnow().isoformat(), "access_token": "never display"}})
    client._signin_confirmed, client._authenticated_user_id = True, "private-user-identity"
    row = (await MonitorCashService(lambda: {"prophetx": client}).refresh())["accounts"]["prophetx"]
    assert row["reported_balance_usd"] == "30.25" and row["reported_exposure_credit_usd"] == "2"
    assert row["identity_status"] == "signed_private_session"
    assert row["source_clock_status"] == "missing" and row["unmatched_balance_clock"]["source_clock_status"] == "valid"
    assert not row["cash_available_verified"]
    assert "private-user-identity" not in json.dumps(row) and "never display" not in json.dumps(row)


async def test_polymarket_balance_and_buying_power_are_separate_reported_values():
    client = Client("polymarket_us", "production", {"balances": [{"currency": "USD",
        "currentBalance": "40", "buyingPower": "70", "balanceReservation": "2",
        "lastUpdated": utcnow().isoformat(), "pendingWithdrawals": [{"bank": "never display"}]}]})
    row = (await MonitorCashService(lambda: {"polymarket_us": client}).refresh())["accounts"]["polymarket_us"]
    assert row["reported_balance_usd"] == "40" and row["reported_buying_power_usd"] == "70"
    assert not row["identity_verified"] and not row["cash_available_verified"]
    assert "never display" not in json.dumps(row)
    assert len(client.calls) == 1


async def test_cooldown_does_not_refresh_timestamp_and_old_read_becomes_stale():
    client, clock, tick = Client(), [utcnow()], [10.0]
    service = MonitorCashService(lambda: {"novig": client}, now=lambda: clock[0], monotonic=lambda: tick[0])
    original = (await service.refresh())["accounts"]["novig"]["observed_at"]
    clock[0] += timedelta(seconds=20)
    tick[0] += 20
    row = (await service.refresh())["accounts"]["novig"]
    assert len(client.calls) == 1 and row["observed_at"] == original
    assert row["age_seconds"] == 20 and row["refresh_after_seconds"] == 10
    clock[0] += timedelta(seconds=60)
    tick[0] += 60
    assert service.state()["accounts"]["novig"]["status"] == "stale"
    assert service.state()["accounts"]["novig"]["refresh_eligible"]


async def test_simultaneous_refreshes_do_not_duplicate_account_reads():
    client, started, finish = Client(), asyncio.Event(), asyncio.Event()
    async def slow(*args, **kwargs):
        client.calls.append(1)
        started.set()
        await finish.wait()
        return client.raw
    client.request = slow
    service = MonitorCashService(lambda: {"novig": client})
    first = asyncio.create_task(service.refresh())
    await started.wait()
    await service.refresh()
    finish.set()
    await first
    assert len(client.calls) == 1


async def test_replaced_client_cannot_install_an_inflight_old_account_read():
    client, started, finish = Client(), asyncio.Event(), asyncio.Event()
    clients = {"novig": client}
    async def slow(*args, **kwargs):
        started.set()
        await finish.wait()
        return client.raw
    client.request = slow
    service = MonitorCashService(lambda: clients)
    pending = asyncio.create_task(service.refresh())
    await started.wait()
    clients["novig"] = Client(environment="production")
    finish.set()
    row = (await pending)["accounts"]["novig"]
    assert row["reported_balance_usd"] is None and row["money_type"] == "real"


async def test_cached_other_environment_hidden_and_disabled_client_never_reads():
    client, clients = Client(), {}
    clients["novig"] = client
    service = MonitorCashService(lambda: clients)
    await service.refresh()
    client.settings.environment = "production"
    assert service.state()["accounts"]["novig"]["reported_balance_usd"] is None
    clients.clear()
    await service.refresh()
    assert len(client.calls) == 1
    assert service.state()["accounts"]["novig"]["status"] == "not_connected"


async def test_lost_authentication_retains_historical_amount_but_not_identity():
    client = Client()
    service = MonitorCashService(lambda: {"novig": client})
    await service.refresh()
    client.authenticated = False
    row = (await service.refresh())["accounts"]["novig"]
    assert row["reported_balance_usd"] == "25.00" and row["status"] == "disconnected"
    assert not row["identity_verified"] and not row["refresh_eligible"]
    assert len(client.calls) == 1


@pytest.mark.parametrize("value", [True, None, -1, 1.25, "NaN", "Infinity"])
async def test_bad_money_cannot_be_displayed_as_valid_cash(value):
    client = Client(raw={"balance": value, "keyId": KEY})
    row = (await MonitorCashService(lambda: {"novig": client}).refresh())["accounts"]["novig"]
    assert row["status"] == "refresh_failed" and row["reported_balance_usd"] is None


async def test_failed_refresh_preserves_old_observation_without_exception_details():
    client, tick = Client(), [100.0]
    service = MonitorCashService(lambda: {"novig": client}, monotonic=lambda: tick[0])
    await service.refresh()
    before = service.state()["accounts"]["novig"]["observed_at"]
    async def fail(*args, **kwargs):
        raise RuntimeError("secret raw server response must never display")
    client.request = fail
    tick[0] += 40
    report = await service.refresh()
    row = report["accounts"]["novig"]
    assert row["status"] == "refresh_failed" and row["reported_balance_usd"] == "25.00"
    assert row["observed_at"] == before and "secret raw" not in json.dumps(report)


async def test_wallet_mismatch_and_duplicate_currencies_are_rejected():
    novig = Client(raw={"keyId": "different", "balance": "1000"})
    poly = Client("polymarket_us", "production", {"balances": [
        {"currency": "USD", "currentBalance": "1"}, {"currency": "USD", "currentBalance": "9"}]})
    report = await MonitorCashService(lambda: {"novig": novig, "polymarket_us": poly}).refresh()
    assert all(report["accounts"][venue]["reported_balance_usd"] is None for venue in ("novig", "polymarket_us"))


async def test_future_venue_reader_registers_without_dashboard_or_service_branch():
    client = Client("future_exchange")
    service = MonitorCashService(lambda: {client.venue_id: client})
    async def read(client):
        stamp = utcnow().isoformat()
        return {"venue": "future_exchange", "environment": "sandbox", "money_type": "play",
                "reported_balance_usd": "12.3", "observed_at": stamp, "request_started_at": stamp,
                "account_scope": "OFFLINE future exchange fixture"}
    service.register_reader("future_exchange", read)
    row = (await service.refresh("future_exchange"))["accounts"]["future_exchange"]
    assert row["reported_balance_usd"] == "12.3" and not row["available_for_kelly"]
    with pytest.raises(ValueError):
        service.register_reader("future_exchange", read)


async def test_registered_reader_cannot_claim_spendable_cash_or_add_secret_fields():
    client = Client("future_exchange")
    async def bad(client):
        return {"venue": client.venue_id, "api_key": "never display", "available_for_kelly": True}
    service = MonitorCashService(lambda: {client.venue_id: client})
    service.register_reader(client.venue_id, bad)
    report = await service.refresh()
    assert report["accounts"][client.venue_id]["status"] == "refresh_failed"
    assert "never display" not in json.dumps(report)
