import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from hlbot.config import ConfigError, resolve_limits


def _page() -> str:
    return (Path(__file__).with_name("console.html")).read_text(encoding="utf-8")


def create_app(session, token: str, reload_fn=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if path in ("/", "/favicon.ico"):
            return await call_next(request)
        if path.startswith("/api/"):
            got = request.headers.get("authorization", "")
            if not secrets.compare_digest(got, "Bearer " + token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @app.get("/")
    def index():
        return HTMLResponse(_page())

    @app.get("/api/state")
    def state():
        return session.public_state()

    @app.get("/api/coins")
    def coins():
        return session.public_coins()

    @app.post("/api/coins/enable")
    async def enable(request: Request):
        body = await request.json()
        name = body.get("name")
        if not isinstance(name, str):
            return JSONResponse({"error": "name"}, status_code=400)
        err = session.set_coin(name, bool(body.get("on")), bool(body.get("force")))
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}

    @app.post("/api/coins/enable-liquid")
    def enable_liquid():
        return {"ok": True, "added": session.enable_liquid()}

    @app.post("/api/strategies")
    async def save_strategy(request: Request):
        body = await request.json()
        try:
            strat = session.upsert_strategy(body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "strategy": strat.as_dict()}

    @app.post("/api/strategies/select")
    async def select_strategy(request: Request):
        body = await request.json()
        name = body.get("name")
        if not isinstance(name, str) or not session.select_strategy(name):
            return JSONResponse({"error": "unknown"}, status_code=404)
        return {"ok": True}

    @app.delete("/api/strategies/{name}")
    def delete_strategy(name: str):
        if not session.drop_strategy(name):
            return JSONResponse({"error": "unknown"}, status_code=404)
        return {"ok": True}

    @app.post("/api/risk")
    async def save_risk(request: Request):
        body = await request.json()
        try:
            limits = resolve_limits(body)
        except ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        with session.lock:
            session.limits = limits
            session._lev_applied.clear()
            session._lev_failed.clear()
        return {"ok": True, "risk": session.public_state()["risk"]}

    @app.post("/api/margin")
    async def save_margin(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "margin"}, status_code=400)
        err = session.set_margin(str(body.get("margin") or ""))
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True, "margin": session.margin}

    @app.post("/api/leverage")
    async def save_leverage(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "leverage"}, status_code=400)
        name = str(body.get("coin") or "")
        err = session.set_leverage(name, body.get("leverage"))
        if err:
            return JSONResponse({"error": err}, status_code=400)
        coin = session.coins.get(name)
        n = int(session.leverage.get(name, 1))
        top = int(coin.max_leverage) if coin is not None else n
        return {"ok": True, "coin": name, "leverage": n, "maxed": top > 1 and n == top}

    @app.post("/api/halt")
    def halt():
        session.halt()
        return {"ok": True}

    @app.post("/api/resume")
    def resume():
        session.resume()
        return {"ok": True}

    @app.post("/api/arm")
    def arm():
        err = session.arm()
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}

    @app.post("/api/disarm")
    def disarm():
        session.disarm()
        return {"ok": True}

    @app.post("/api/account")
    async def account(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "account"}, status_code=400)
        try:
            out = session.connect_account(
                str(body.get("mode") or "paper"),
                str(body.get("account") or ""),
                str(body.get("vault") or ""),
                str(body.get("agent_key") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "mode": out["mode"], "agent": out.get("agent", "")}

    @app.post("/api/interval")
    async def save_interval(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "interval"}, status_code=400)
        err = session.set_interval(str(body.get("interval") or ""))
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True, "interval": session.interval}

    @app.post("/api/flatten")
    def flatten():
        from hlbot.models import Reject

        rows = session.flatten()
        sent = sum(1 for row in rows if not isinstance(row, Reject))
        rejected = [row.reason for row in rows if isinstance(row, Reject)]
        return {"ok": True, "n": sent, "rejected": rejected}

    @app.post("/api/step")
    def step():
        return {"ok": True, "n": len(session.step())}

    @app.post("/api/catalog/reload")
    def reload_catalog():
        fn = reload_fn
        if fn is None:
            from hlbot.catalog import fetch_catalog, post_info

            def fn(payload):
                return post_info(session.info_url, payload)

            try:
                coins = fetch_catalog(fn)
            except Exception as exc:
                return JSONResponse({"error": exc.__class__.__name__}, status_code=502)
        else:
            try:
                coins = fn()
            except Exception as exc:
                return JSONResponse({"error": exc.__class__.__name__}, status_code=502)
        session.replace_catalog(coins)
        return {"ok": True, "coins": len(session.coins)}

    return app
