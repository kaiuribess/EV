"""Synchronous, placement-only checks at the actual transport boundary.

These retain the already-consumed permit; they never issue or consume another
authorization. All captured credentials remain backend-only and are never
included in errors or logs.
"""
from __future__ import annotations

import time
from typing import Callable

from .common import PreflightRequired


def placement_guard(client, client_order_id: str, before_send: Callable[[], None] | None = None):
    settings, transport = client.settings, client.http
    gate = settings.gate
    approval = gate.risk_approval_id
    configuration = settings.model_dump(exclude={"gate"})

    def check():
        if (client.settings is not settings or client.http is not transport
                or settings.model_dump(exclude={"gate"}) != configuration
                or settings.gate is not gate or gate.risk_approval_id != approval
                or client._closed):
            raise PreflightRequired("Native placement binding changed before dispatch")
        gate.require(client_order_id, settings.environment)
        if before_send is not None:
            before_send()

    check()
    return check


def authenticated_request_guard(client, before_send: Callable[[], None] | None):
    """Reject a request signed by a key/session replaced while it was queued."""
    if before_send is None:
        return None
    key = getattr(client, "_key", None)
    token = getattr(client, "_access_token", None)

    def check():
        if (getattr(client, "_key", None) is not key
                or getattr(client, "_access_token", None) != token
                or (token is not None and (not token or client._access_expires <= time.time()))):
            raise PreflightRequired("Native signing key or authenticated session changed before dispatch")
        before_send()

    return check
