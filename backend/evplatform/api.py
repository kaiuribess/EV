import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .core.config import PricingConfig, RiskConfig, Settings
from .core.models import Envelope, Market, SettlementResult
from .core.market_review import ContractReview, compare_contracts, pending_market_reviews
from .core.native_accounting import ExactDecimal
from .core.settlement_bridge import ExceptionalSettlementBound, estimate_bridge
from .core.launch_setup import launch_setup, operator_limits_draft
from .core.manual_native import ManualNativeService
from .core.paths import runtime_data_dir
from .core.research_review import read_reviews, review_payload, save_review
from .core.risk import RiskRejected
from .core.signals import SignalResultImport
from .engine import Engine


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_trade: bool | None = None
    kill_switch: bool | None = None
    expected_launch_digest: str | None = None
    authorization_expires_at: datetime | None = None


class ToggleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict


class ContractComparisonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: ContractReview
    target: ContractReview


class SettlementBridgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Market
    target: Market
    outcome: str
    normal_probability: ExactDecimal
    exceptional_bound: ExceptionalSettlementBound


class CashRefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    venue: str | None = None


class LaunchProbeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    venue: str = Field(min_length=1)


class ProphetXDiscoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tournament_id: int | None = Field(default=None, ge=1, le=1_000_000_000)
    event_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    event_ids: list[str] | None = Field(default=None, min_length=1, max_length=10)
    limit: int = Field(default=20, ge=1, le=50)
    market_sub_type: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")


class ManualPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market_id: str
    venue: str
    outcome: str
    max_cost_usd: Decimal = Field(gt=0, allow_inf_nan=False)


class ManualSubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_token: str
    terms_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_max_cost_usd: Decimal = Field(gt=0, allow_inf_nan=False)


class NativeResearchReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_market_id: str = Field(min_length=1)
    venue: str = Field(pattern="^(kalshi|novig)$")
    native_market_id: str = Field(min_length=1)
    native_outcome_id: str = Field(min_length=1)
    strategy: Literal["multi_book_consensus", "sharp_books"] | None = None


def local_origin(value, port=8000):
    if value is None:
        return True
    if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlparse(value)
        return (parsed.scheme in {"http", "https"}
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.port in {port, 5173}
                and parsed.username is None and parsed.password is None
                and not (parsed.path or parsed.params or parsed.query or parsed.fragment))
    except ValueError:
        return False


def local_host(value):
    if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlparse("//" + value)
        return (parsed.hostname in {"localhost", "127.0.0.1", "::1", "testserver"}
                and parsed.username is None and parsed.password is None
                and parsed.port != 0
                and not (parsed.path or parsed.params or parsed.query or parsed.fragment))
    except ValueError:
        return False


def create_app(settings=None):
    engine = Engine(settings or Settings())
    from .core.monitor_cash import MonitorCashService
    from .plugins.external import NativeMonitor
    def monitored_clients():
        return {name: record.plugin.client for name, record in engine.registry.records.items()
                if record.enabled and isinstance(record.plugin, NativeMonitor) and record.plugin.client is not None}
    monitor_cash = MonitorCashService(monitored_clients)

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()

    app = FastAPI(title="EV Platform", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.monitor_cash = monitor_cash
    manual_native = ManualNativeService(engine)
    app.state.manual_native = manual_native

    @app.middleware("http")
    async def loopback_guard(request: Request, call_next):
        if not local_host(request.headers.get("host")):
            response = JSONResponse({"detail": "Local access only"}, status_code=403)
        elif not local_origin(request.headers.get("origin"), engine.settings.port):
            response = JSONResponse({"detail": "Untrusted origin"}, status_code=403)
        elif request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("X-EV-Control") != "local":
            response = JSONResponse({"detail": "Local control header required"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    async def health():
        stopped = engine.stopped_workers()
        return {"status": "degraded" if stopped else "running",
                "mode": engine.settings.mode, "kill_switch": engine.risk.kill_switch,
                "critical_workers_stopped": stopped,
                "plugin_workers_stopped": engine.stopped_plugin_workers()}

    @app.get("/api/state")
    async def state():
        return engine.state()

    @app.get("/api/native/market-data/prophetx")
    async def prophetx_market_data(
        limit: int = Query(20, ge=1, le=100),
        event_id: str | None = Query(None, pattern=r"^[0-9]{1,20}$"),
        market_id: str | None = Query(None, pattern=r"^[0-9]{1,20}$"),
        strike_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
    ):
        return engine.native.prophetx_research_books(
            limit=limit, event_id=event_id, market_id=market_id, strike_id=strike_id)

    @app.get("/api/native/market-data/polymarket-us")
    async def polymarket_us_market_data(limit: int = Query(20, ge=1, le=50)):
        return engine.native.polymarket_us_research_books(limit=limit)

    @app.get("/api/signals")
    async def signal_history(limit: int = Query(25, ge=1, le=200),
                             offset: int = Query(0, ge=0), synthetic: bool = True):
        return await engine.signal_ledger.query(limit=limit, offset=offset, synthetic=synthetic)

    @app.get("/api/signals/summary")
    async def signal_summary(synthetic: bool = True):
        return await engine.signal_ledger.summary(synthetic=synthetic)

    @app.get("/api/signals/transitions")
    async def signal_transitions(limit: int = Query(50, ge=1, le=200),
                                 offset: int = Query(0, ge=0), synthetic: bool = True):
        return await engine.signal_ledger.transitions(limit=limit, offset=offset, synthetic=synthetic)

    @app.post("/api/signals/result")
    async def signal_result(body: SignalResultImport):
        try:
            await engine.record_signal_result(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"status": "recorded", "hypothetical_only": True,
                "synthetic": body.result.synthetic,
                "note": "Operator-supplied canonical result; this does not settle native positions or prove execution."}

    @app.get("/api/readiness")
    async def readiness():
        summary = await engine.signal_ledger.summary(synthetic=False)
        report = engine.native.report()
        return {**report.model_dump(mode="json"),
            "paper_position_conflicts_enforced": True,
            "signal_ledger": engine.signal_ledger.started,
            "real_signal_evidence": summary,
            "blocking_items": report.blocking_items,
            "configuration_error": engine.native.profile_error,
            "accounts": {venue: item.model_dump(mode="json") for venue, item in engine.native.reports().items()},
        }

    @app.get("/api/readiness/runtime")
    async def runtime_readiness(venue: str | None = None):
        if venue is not None and engine.native.profile_for(venue) is None:
            raise HTTPException(404, "No reviewed launch profile for this venue")
        return {**engine.native.runtime_facts(venue).model_dump(mode="json"),
            "accounts": {key: facts.model_dump(mode="json") for key, facts in engine.native.runtime_facts_by_venue().items()}}

    @app.get("/api/launch/setup")
    async def launch_preparation():
        limits, error = await asyncio.to_thread(operator_limits_draft)
        return launch_setup(engine, limits_draft=limits, limits_error=error)

    @app.post("/api/launch/probe")
    async def launch_account_probe(body: LaunchProbeBody):
        try:
            account = await engine.native.probe_monitor_account(body.venue)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"account": account, "read_only": True, "usable_for_execution": False,
                "note": "Pre-profile account diagnostic; it does not arm trading or certify an order lifecycle."}

    @app.post("/api/native/discovery/prophetx")
    async def prophetx_catalog(body: ProphetXDiscoveryBody):
        """Inspect an exact native event using the monitor's existing session."""
        from .integrations.prophetx_discovery import (
            summarize_batched_markets, summarize_events, summarize_markets)
        from .venues.common import VenueHTTPError

        if sum(value is not None for value in (body.tournament_id, body.event_id, body.event_ids)) != 1:
            raise HTTPException(422, "Provide exactly one tournament_id, event_id or event_ids")
        if body.event_ids is not None and (len(set(body.event_ids)) != len(body.event_ids)
                or any(not value.isascii() or not value.isdigit() or len(value) > 20
                       for value in body.event_ids)):
            raise HTTPException(422, "Unique numeric event_ids are required")
        record = engine.registry.records.get("prophetx")
        plugin = record.plugin if record else None
        client = getattr(plugin, "client", None)
        if (not record or not record.enabled or not isinstance(plugin, NativeMonitor)
                or client is None or plugin.task is None or plugin.task.done()
                or record.health.get("authenticated") is not True):
            raise HTTPException(409, "A connected ProphetX native monitor is required")
        try:
            if body.tournament_id is not None:
                payload = await asyncio.wait_for(client.discover_events(tournament_id=body.tournament_id), 15)
                return summarize_events(payload, limit=body.limit)
            if body.event_ids is not None:
                payload = await asyncio.wait_for(client.discover_markets(body.event_ids), 15)
                return summarize_batched_markets(payload, event_ids=body.event_ids,
                    limit=body.limit, sub_type=body.market_sub_type)
            payload = await asyncio.wait_for(client.fetch_snapshot(body.event_id), 15)
            return summarize_markets(payload, limit=body.limit, sub_type=body.market_sub_type)
        except VenueHTTPError as exc:
            # VenueHTTPError contains only status and a provider error code.
            raise HTTPException(502, {"provider_status": exc.status_code, "provider_code": exc.code}) from None
        except (ValueError, RuntimeError, asyncio.TimeoutError):
            raise HTTPException(502, "ProphetX catalog read or schema validation failed") from None

    @app.get("/api/native/discovery/prophetx/tournaments")
    async def prophetx_active_tournaments():
        """Bounded public identity summary using the monitor's existing session."""
        from .integrations.prophetx_discovery import summarize_tournaments
        from .plugins.external import NativeMonitor
        from .venues.common import VenueHTTPError

        record = engine.registry.records.get("prophetx")
        plugin = record.plugin if record else None
        client = getattr(plugin, "client", None)
        if (not record or not record.enabled or not isinstance(plugin, NativeMonitor)
                or client is None or plugin.task is None or plugin.task.done()
                or record.health.get("authenticated") is not True):
            raise HTTPException(409, "A connected ProphetX native monitor is required")
        try:
            payload = await asyncio.wait_for(client.discover_sports(active_events_only=True), 15)
            return summarize_tournaments(payload)
        except VenueHTTPError as exc:
            raise HTTPException(502, {"provider_status": exc.status_code,
                                      "provider_code": exc.code}) from None
        except (ValueError, RuntimeError, asyncio.TimeoutError):
            raise HTTPException(502, "ProphetX tournament read or schema validation failed") from None

    @app.get("/api/accounts/cash")
    async def reported_cash():
        return monitor_cash.state()

    @app.post("/api/accounts/cash/refresh")
    async def refresh_reported_cash(body: CashRefreshBody):
        try:
            return await monitor_cash.refresh(body.venue)
        except ValueError:
            raise HTTPException(422, "Choose an enabled account monitor")

    @app.get("/api/market-review")
    async def market_review(provider: str | None = None, offset: int = Query(0, ge=0),
                            limit: int = Query(100, ge=1, le=1000)):
        return pending_market_reviews(engine, provider=provider, offset=offset, limit=limit)

    @app.post("/api/market-review/compare")
    async def compare_review(body: ContractComparisonBody):
        return compare_contracts(body.source, body.target)

    @app.post("/api/market-review/bridge-estimate")
    async def bridge_estimate(body: SettlementBridgeBody):
        try:
            return estimate_bridge(body.source, body.target, body.outcome,
                                   body.normal_probability, body.exceptional_bound)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/native/orders")
    async def native_orders():
        return {"orders": [o.model_dump(mode="json") for o in engine.risk.native_orders.values()],
                "native_execution_enabled": engine.settings.live_enabled}

    @app.get("/api/native/signals")
    async def native_signals(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        import json
        rows = await (await engine.storage.connection.execute(
            "SELECT data FROM events WHERE schema_name='native.signal' ORDER BY seq DESC LIMIT ? OFFSET ?",
            (limit, offset))).fetchall()
        return {"items": [json.loads(await engine.storage.decode_event_data(row[0])) for row in rows], "limit": limit, "offset": offset,
                "note": "Native-unit observations; execution and settlement are tracked separately"}

    @app.post("/api/native/manual/preview")
    async def native_manual_preview(body: ManualPreviewBody):
        try:
            return await manual_native.preview(body.market_id, body.venue, body.outcome, body.max_cost_usd)
        except (RiskRejected, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/native/research-review/drafts")
    async def native_research_drafts():
        destination = runtime_data_dir(engine.settings.data_dir) / "native_review_drafts"
        return {"items": await asyncio.to_thread(read_reviews, destination),
                "execution_authorized": False}

    @app.post("/api/native/research-review/drafts")
    async def native_research_draft(body: NativeResearchReviewBody):
        try:
            payload = review_payload(engine.state(), **body.model_dump())
            destination = runtime_data_dir(engine.settings.data_dir) / "native_review_drafts"
            target = await asyncio.to_thread(save_review, payload, destination)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"draft_id": target.stem, "status": payload["status"],
                "execution_authorized": False, "model_approved": False}

    @app.post("/api/native/manual/submit")
    async def native_manual_submit(body: ManualSubmitBody):
        try:
            return await manual_native.submit(body.review_token, body.terms_digest,
                                               body.approved_max_cost_usd, body.proposal_digest)
        except (RiskRejected, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/native/orders/{order_id}/cancel")
    async def native_cancel(order_id: str):
        if order_id not in engine.risk.native_orders:
            raise HTTPException(404, "Unknown native order")
        return (await engine.native.manager.cancel(order_id)).model_dump(mode="json")

    @app.post("/api/native/orders/{order_id}/reconcile")
    async def native_reconcile(order_id: str):
        if order_id not in engine.risk.native_orders:
            raise HTTPException(404, "Unknown native order")
        try:
            return (await engine.native.manager.reconcile(order_id)).model_dump(mode="json")
        except (ValueError, RuntimeError):
            raise HTTPException(409, "Native order requires complete authenticated reconciliation")

    @app.post("/api/shutdown")
    async def shutdown():
        if not hasattr(app.state, "request_shutdown"):
            raise HTTPException(409, "Shutdown is available through the local launcher")
        await engine.control(auto_trade=False)
        app.state.request_shutdown()
        return {"status": "stopping"}

    @app.get("/api/markets/{market_id:path}")
    async def market(market_id: str):
        if market_id not in engine.markets:
            raise HTTPException(404, "Unknown market")
        return engine.market_detail(market_id)

    @app.post("/api/control")
    async def control(body: ControlBody):
        try:
            await engine.control(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return engine.state()

    @app.post("/api/orders/{order_id}/cancel")
    async def cancel(order_id: str):
        if order_id not in engine.risk.orders:
            raise HTTPException(404, "Unknown order")
        await engine.oms.cancel(order_id)
        return engine.state()

    @app.post("/api/paper/settle")
    async def paper_settle(body: SettlementResult):
        if not body.synthetic or engine.settings.mode not in {"demo", "paper"}:
            raise HTTPException(409, "Only explicitly simulated settlements are accepted here")
        try:
            await engine.apply_settlement(body)
            await engine.bus.publish(Envelope(schema_name="trade.settlement", source="paper_operator", payload=body))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return engine.state()

    @app.post("/api/plugins/{name}/toggle")
    async def toggle(name: str, body: ToggleBody):
        if name not in engine.registry.records:
            raise HTTPException(404, "Unknown plugin")
        try:
            await engine.toggle_plugin(name, body.enabled)
        except RuntimeError:
            raise HTTPException(503, "Plugin startup preference could not be saved")
        return engine.state()

    @app.post("/api/plugins/{name}/config")
    async def configure(name: str, body: ConfigBody):
        if name not in engine.registry.records:
            raise HTTPException(404, "Unknown plugin")
        try:
            enabled = engine.registry.records[name].enabled
            engine.registry.configure(name, body.config)
            if name == "positive_ev":
                engine.settings.pricing = PricingConfig.model_validate(body.config)
                await engine.storage.save_config("pricing", engine.settings.pricing.model_dump(mode="json"))
            if enabled:
                await engine.registry.stop(name)
                await engine.registry.start(name, engine.context(name))
            await engine.storage.save_config("plugin_configs", {k: r.config for k, r in engine.registry.records.items()})
        except (ValueError, TypeError) as exc:
            # Pydantic errors may echo secrets; only non-secret fields are allowed by config schemas.
            raise HTTPException(422, "Plugin configuration did not pass schema validation")
        return engine.state()

    @app.post("/api/settings")
    async def settings_update(body: dict):
        allowed_pricing = set(PricingConfig.model_fields)
        allowed_risk = set(RiskConfig.model_fields) - {"live_max_order_stake", "live_stake_authorized"}
        if set(body) - allowed_pricing - allowed_risk:
            raise HTTPException(422, "Unknown or restricted setting")
        try:
            pricing = PricingConfig.model_validate({**engine.settings.pricing.model_dump(),
                                                    **{k: v for k, v in body.items() if k in allowed_pricing}})
            risk = RiskConfig.model_validate({**engine.risk.config.model_dump(),
                                              **{k: v for k, v in body.items() if k in allowed_risk}})
        except ValueError:
            raise HTTPException(422, "Settings failed validation")
        engine.settings.pricing = pricing
        engine.risk.config = engine.settings.risk = risk
        engine.registry.configure("positive_ev", pricing.model_dump(mode="json"))
        await engine.storage.save_config("pricing", pricing.model_dump(mode="json"))
        await engine.storage.save_config("risk", risk.model_dump(mode="json"))
        if set(body) & allowed_pricing:
            # A new research display window must refresh existing source-only
            # markets immediately. This pure path never evaluates a native
            # strategy, publishes a candidate, or arms an execution venue.
            for index, (market_id, market) in enumerate(list(engine.markets.items())):
                if market.extensions.get("research_only"):
                    engine.refresh_reference_estimate(market_id)
                    engine.schedule_expiration(market_id)
                    if index % 64 == 63:
                        await asyncio.sleep(0)
        return engine.state()

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        if not local_host(socket.headers.get("host")) or not local_origin(socket.headers.get("origin"), engine.settings.port):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            while True:
                await socket.send_json({"type": "state", "data": engine.state()})
                await asyncio.sleep(.5)
        except (WebSocketDisconnect, RuntimeError):
            pass

    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/")
        async def dashboard():
            return FileResponse(dist / "index.html")
    else:
        @app.get("/")
        async def dashboard_missing():
            return {"message": "Build dashboard with setup_windows.ps1 or run npm run dev in frontend", "api": "/docs"}
    return app

