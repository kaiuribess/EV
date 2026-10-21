import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .core.config import PricingConfig, RiskConfig, Settings
from .core.models import Envelope, SettlementResult
from .core.market_review import ContractReview, compare_contracts, pending_market_reviews
from .core.signals import SignalResultImport
from .engine import Engine


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_trade: bool | None = None
    kill_switch: bool | None = None


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


def local_origin(value, port=8000):
    if value is None:
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port in {port, 5173}


def create_app(settings=None):
    engine = Engine(settings or Settings())

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()

    app = FastAPI(title="EV Platform", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine

    @app.middleware("http")
    async def loopback_guard(request: Request, call_next):
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return JSONResponse({"detail": "Local access only"}, status_code=403)
        if not local_origin(request.headers.get("origin"), engine.settings.port):
            return JSONResponse({"detail": "Untrusted origin"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("X-EV-Control") != "local":
            return JSONResponse({"detail": "Local control header required"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/health")
    async def health():
        return {"status": "running", "mode": engine.settings.mode, "kill_switch": engine.risk.kill_switch}

    @app.get("/api/state")
    async def state():
        return engine.state()

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

    @app.get("/api/market-review")
    async def market_review(provider: str | None = None, offset: int = Query(0, ge=0),
                            limit: int = Query(100, ge=1, le=1000)):
        return pending_market_reviews(engine, provider=provider, offset=offset, limit=limit)

    @app.post("/api/market-review/compare")
    async def compare_review(body: ContractComparisonBody):
        return compare_contracts(body.source, body.target)

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
        allowed_risk = set(RiskConfig.model_fields) - {"live_max_order_stake"}
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
        return engine.state()

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        if not local_origin(socket.headers.get("origin"), engine.settings.port):
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

