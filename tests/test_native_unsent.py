"""Only a synchronous pre-send rejection proves a new order never went out."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from evplatform.core.models import OrderStatus
from evplatform.core.risk import RiskRejected
from evplatform.venues.common import DispatchRejected, NativeSettings, NativeTransport, UnknownOrderOutcome
from test_native_oms import order, system


async def test_before_send_rejection_is_redacted_and_marks_new_intent_rejected_without_retry(system,market):
    manager,risk,storage,adapter=system
    writes=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:writes.append(request) or httpx.Response(200,json={}))) as http:
        transport=NativeTransport(NativeSettings(),http)
        def guard(): raise ValueError("OFFLINE credential-like text must not appear")
        async def submit(intent,authorization):
            assert risk.authority.consume(authorization,intent.id)
            return await transport._request(http.build_request("POST","https://offline.invalid/orders"),
                mutation_id=intent.client_order_id,before_send=guard)
        adapter.submit_intent=submit
        original=order(market)
        result=await manager.execute(original)
        assert result.status==OrderStatus.REJECTED and result.reconciliation_complete
        assert not writes and not transport.request_count and risk.exposures()==0
        assert not risk.kill_switch and "credential-like" not in result.reason
        with pytest.raises(RiskRejected): await manager.execute(original)
        await manager.start()
        assert risk.native_orders[result.id].status==OrderStatus.REJECTED and risk.exposures()==0
        assert (await manager.reconcile(result.id)).status==OrderStatus.REJECTED
        assert not risk.kill_switch


async def test_real_transport_timeout_remains_unknown_with_reservation(system,market):
    manager,risk,storage,adapter=system
    writes=[]
    def timeout(request):
        writes.append(request)
        raise httpx.ReadTimeout("OFFLINE transport timeout",request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        transport=NativeTransport(NativeSettings(),http)
        async def submit(intent,authorization):
            assert risk.authority.consume(authorization,intent.id)
            return await transport._request(http.build_request("POST","https://offline.invalid/orders"),
                mutation_id=intent.client_order_id,before_send=lambda:None)
        adapter.submit_intent=submit
        original=order(market)
        result=await manager.execute(original)
        assert result.status==OrderStatus.UNKNOWN and not result.reconciliation_complete
        assert len(writes)==1 and risk.kill_switch and risk.exposures()>0
        with pytest.raises(RiskRejected): await manager.execute(original)


async def test_guard_rejection_cannot_release_an_intent_already_bound_to_native_evidence(system,market):
    manager,risk,storage,adapter=system
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={}))) as http:
        transport=NativeTransport(NativeSettings(),http)
        def guard(): raise ValueError("OFFLINE reject")
        async def submit(intent,authorization):
            assert risk.authority.consume(authorization,intent.id)
            adapter._native_intents={"known-exchange-id":intent}
            return await transport._request(http.build_request("POST","https://offline.invalid/orders"),
                mutation_id=intent.client_order_id,before_send=guard)
        adapter.submit_intent=submit
        result=await manager.execute(order(market))
        assert result.status==OrderStatus.UNKNOWN and risk.exposures()>0 and risk.kill_switch


async def test_ws_guard_exception_has_explicit_unsent_type_but_cancelled_or_send_errors_do_not():
    transport=NativeTransport(NativeSettings())
    transport._ws=SimpleNamespace(send=AsyncMock())
    def guard(): raise ValueError("OFFLINE secret-shaped detail")
    with pytest.raises(DispatchRejected) as caught:
        await transport._send_ws({"type":"order"},before_send=guard)
    assert str(caught.value)=="Native dispatch rejected before transmission"
    transport._ws.send.assert_not_called()
    def cancelled(): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await transport._send_ws({"type":"order"},before_send=cancelled)
    transport._ws.send.assert_not_called()
    transport._ws.send=AsyncMock(side_effect=TimeoutError("OFFLINE send ambiguity"))
    with pytest.raises(TimeoutError):
        await transport._send_ws({"type":"order"},before_send=lambda:None)
    transport._ws.send.assert_awaited_once()
    await transport.http.aclose()
