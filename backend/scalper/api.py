"""HTTP + WebSocket gateway for the web UI.

Built on Starlette only (pure Python, no pydantic) so the same server runs on desktop and inside the
Android app. Handlers are async and run on the engine's event loop, so engine state is never touched
from another thread.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import REPO_ROOT, Cfg, save_local_settings
from .engine import Engine

log = logging.getLogger("scalper.api")

# Settings the UI may change (written to local.yaml; applied on restart).
EDITABLE_SETTINGS = {
    "universe.trend_universe_size": int, "universe.radar_max_symbols": int,
    "universe.radar_min_quote_volume": float, "trend.sensitivity": str, "execution.market": str,
    "risk.equity": float, "risk.risk_per_trade": float, "risk.max_daily_loss": float,
    "risk.max_open_positions": int, "signals.min_confluence": float, "signals.show_unvalidated": bool,
    "alerts.telegram_token": str, "alerts.telegram_chat_id": str, "alerts.discord_webhook": str,
    "alerts.min_p_win": float, "alerts.pump_alerts": bool, "alerts.local_notifications": bool,
    "news.enabled": bool, "news.cryptopanic_token": str, "bootstrap.enabled": bool,
}
SECRET_SETTINGS = {"alerts.telegram_token", "alerts.discord_webhook", "news.cryptopanic_token"}


def _clean(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


class SafeJSON(JSONResponse):
    """JSON response that turns NaN/inf into null instead of failing."""

    def render(self, content) -> bytes:
        try:
            return json.dumps(content, allow_nan=False, separators=(",", ":"), default=str).encode()
        except ValueError:
            return json.dumps(_clean(content), allow_nan=False, separators=(",", ":"), default=str).encode()


def _q(req: Request, name: str, cast=str, default=None):
    v = req.query_params.get(name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except ValueError as e:
        raise HTTPException(400, f"invalid {name}") from e


def _num(v, name: str, required: bool = False):
    if v is None:
        if required:
            raise HTTPException(400, f"{name} is required")
        return None
    try:
        return float(v)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"{name} must be a number") from e


def create_app(cfg: Cfg, engine: Engine | None = None, start_engine: bool = True,
               ui_dir: Path | None = None) -> Starlette:
    eng = engine or Engine(cfg)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(eng.start()) if start_engine else None
        yield
        if task:
            task.cancel()
        await eng.stop()

    # ---------------------------------------------------------------- read endpoints
    async def status(req):
        return SafeJSON(eng.status())

    async def watchlist(req):
        return SafeJSON(eng.watchlist())

    async def chart(req):
        symbol = req.path_params["symbol"].upper()
        tf = _q(req, "tf", str, "1m")
        limit = _q(req, "limit", int, 600)
        if symbol not in eng.states:
            if not await eng.add_symbol(symbol):
                raise HTTPException(404, f"unknown symbol {symbol}")
        data = eng.chart(symbol, tf, min(limit, 1000))
        if data is None:
            raise HTTPException(404, f"no data for {symbol} {tf}")
        return SafeJSON(data)

    async def signals(req):
        limit, symbol = _q(req, "limit", int, 200), _q(req, "symbol")
        recent = [r for r in eng.store.recent("signals", limit, symbol) if "strategy" in r]
        return SafeJSON({"active": eng.active_signals(), "recent": recent,
                         "rejected": eng.signal_engine.rejected[-100:]})

    async def trends(req):
        return SafeJSON(eng.trend_board())

    async def trend_records(req):
        return SafeJSON(eng.store.recent("trends", _q(req, "limit", int, 200), _q(req, "symbol")))

    async def radar(req):
        return SafeJSON({"active": eng.pump.radar(), "recent": eng.store.recent("move_events", 100),
                         "continuation": eng.pump.continuation.summary()})

    async def coupling(req):
        return SafeJSON(eng.coupling_table())

    async def news(req):
        symbol = _q(req, "symbol")
        base = symbol.upper().removesuffix("USDT") if symbol else None
        return SafeJSON({"items": eng.news.recent(base, _q(req, "limit", int, 100)),
                         "events": eng.news.upcoming_events(), "fear_greed": eng.news.fear_greed,
                         "health": eng.news.health, "macro_block_now": eng.news.macro_block(int(time.time() * 1000))})

    async def strategies(req):
        out = []
        summ = eng.stats.summary()
        models = eng.model.summary() if eng.model else {}
        for sid, s in eng.strategies.items():
            out.append({"id": sid, "version": s.version, "description": s.description,
                        "timeframes": list(getattr(s, "timeframes", ())), "regimes": list(s.regimes),
                        "needs": list(s.needs), "stats": summ.get(sid), "model": models.get(sid)})
        return SafeJSON({"strategies": out, "bootstrap": eng.backtest_report})

    async def book(req):
        symbol = req.path_params["symbol"].upper()
        eng.set_focus(symbol)
        ob = eng.books.get(symbol)
        if not ob or not ob.synced:
            return SafeJSON({"synced": False})
        bids, asks = ob.top(25)
        return SafeJSON({"synced": True, "bids": bids, "asks": asks, "metrics": eng.book_cache.get(symbol)})

    async def focus(req):
        eng.set_focus(req.path_params["symbol"].upper())
        return SafeJSON({"ok": True})

    # ---------------------------------------------------------------- paper trading
    async def account(req):
        return SafeJSON(eng.broker.account())

    def _place(symbol: str, side: str, stop, targets, risk_scale=1.0, signal_id=None, trailing_atr=None):
        sym = symbol.upper()
        side = str(side).upper()
        if side not in ("LONG", "SHORT"):
            raise HTTPException(400, "side must be LONG or SHORT")
        st = eng.states.get(sym)
        atr = st.bundles["5m"].v.get("atr") if st and "5m" in st.bundles else None
        info = eng.symbol_info.get(sym, {})
        step = min_notional = None
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step = float(f["stepSize"])
            if f.get("filterType") in ("NOTIONAL", "MIN_NOTIONAL"):
                min_notional = float(f.get("minNotional") or 0)
        pos, err = eng.broker.open(sym, side, stop, targets, risk_scale=risk_scale, signal_id=signal_id,
                                   trailing_atr=trailing_atr, atr=atr, step=step, min_notional=min_notional or 0.0)
        if err:
            raise HTTPException(400, err)
        eng.hub.publish({"type": "position_opened", "data": pos.to_json()})
        return SafeJSON(pos.to_json())

    async def place(req):
        try:
            b = await req.json()
        except ValueError as e:
            raise HTTPException(400, "invalid JSON body") from e
        if not isinstance(b, dict) or not b.get("symbol") or not b.get("side"):
            raise HTTPException(400, "symbol and side are required")
        targets = b.get("targets")
        if targets is not None:
            if not isinstance(targets, list):
                raise HTTPException(400, "targets must be a list")
            targets = [_num(t, "target", True) for t in targets]
        return _place(b["symbol"], b["side"], _num(b.get("stop"), "stop"), targets,
                      _num(b.get("risk_scale"), "risk_scale") or 1.0, b.get("signal_id"),
                      _num(b.get("trailing_atr"), "trailing_atr"))

    async def from_signal(req):
        sig = eng.signals.get(req.path_params["signal_id"])
        if not sig:
            raise HTTPException(404, "signal not active")
        c = sig.candidate
        scale = (_q(req, "risk_scale", float, 1.0)) * c.features.get("risk_scale", 1.0)
        return _place(c.symbol, c.side, c.stop, [t.price for t in c.targets], scale, sig.id,
                      c.features.get("trail_atr") if c.trailing else None)

    async def close(req):
        pos = eng.broker.close(req.path_params["pid"])
        if not pos:
            raise HTTPException(404, "no such position")
        eng.store.put("paper_trades", pos.id, pos.closed_at, pos.sim.c.symbol, pos.to_json())
        eng.hub.publish({"type": "position_closed", "data": pos.to_json()})
        return SafeJSON(pos.to_json())

    async def breakeven(req):
        return SafeJSON({"ok": eng.broker.move_stop_to_breakeven(req.path_params["pid"])})

    async def kill(req):
        closed = eng.broker.kill()
        for p in closed:
            eng.store.put("paper_trades", p.id, p.closed_at, p.sim.c.symbol, p.to_json())
        eng.hub.publish({"type": "kill", "closed": len(closed)})
        return SafeJSON({"closed": len(closed), "risk": eng.risk.status(eng.broker.equity, 0)})

    async def kill_reset(req):
        eng.risk.killed = False
        return SafeJSON({"ok": True})

    # ---------------------------------------------------------------- settings
    async def get_settings(req):
        out = {}
        for key in EDITABLE_SETTINGS:
            v = cfg.get_path(key)
            out[key] = ("set" if v else "") if key in SECRET_SETTINGS else v
        return SafeJSON({"settings": out, "restart_required_after_save": True})

    async def put_settings(req):
        try:
            b = await req.json()
        except ValueError as e:
            raise HTTPException(400, "invalid JSON body") from e
        if not isinstance(b, dict):
            raise HTTPException(400, "expected an object of settings")
        clean = {}
        for key, val in b.items():
            cast = EDITABLE_SETTINGS.get(key)
            if cast is None:
                raise HTTPException(400, f"setting {key} cannot be changed here")
            if key in SECRET_SETTINGS and val == "set":
                continue                          # unchanged secret placeholder
            try:
                clean[key] = (val in (True, "true", "1", 1)) if cast is bool else cast(val)
            except (TypeError, ValueError) as e:
                raise HTTPException(400, f"invalid value for {key}") from e
        if clean.get("execution.market", "spot") not in ("spot", "futures"):
            raise HTTPException(400, "execution.market must be spot or futures")
        if clean.get("trend.sensitivity", "balanced") not in ("early", "balanced", "conservative"):
            raise HTTPException(400, "trend.sensitivity must be early, balanced or conservative")
        path = save_local_settings(clean)
        return SafeJSON({"ok": True, "saved": sorted(clean), "path": str(path), "restart_required": True})

    # ---------------------------------------------------------------- websocket
    async def ws(sock: WebSocket):
        await sock.accept()
        q = eng.hub.subscribe()
        try:
            await sock.send_text(SafeJSON({"type": "status", "data": eng.status()}).body.decode())

            async def reader():
                while True:
                    msg = await sock.receive_json()
                    if msg.get("type") == "focus" and msg.get("symbol"):
                        eng.set_focus(msg["symbol"].upper())

            rt = asyncio.create_task(reader())
            try:
                while True:
                    msg = await q.get()
                    await sock.send_text(SafeJSON(msg).body.decode())
            finally:
                rt.cancel()
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug("ws closed: %s", e)
        finally:
            eng.hub.unsubscribe(q)

    async def logs(req):
        """Tail of the engine log file (set by the Android launcher via SCALPER_LOG_FILE)."""
        import os
        f = os.environ.get("SCALPER_LOG_FILE")
        if not f or not Path(f).exists():
            return SafeJSON({"lines": []})
        lines = Path(f).read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        return SafeJSON({"lines": lines})

    async def http_error(req, exc: HTTPException):
        return SafeJSON({"detail": exc.detail}, status_code=exc.status_code)

    routes = [
        Route("/api/status", status), Route("/api/watchlist", watchlist), Route("/api/chart/{symbol}", chart),
        Route("/api/signals", signals), Route("/api/trends", trends), Route("/api/trend-records", trend_records),
        Route("/api/radar", radar), Route("/api/coupling", coupling), Route("/api/news", news),
        Route("/api/strategies", strategies), Route("/api/book/{symbol}", book),
        Route("/api/focus/{symbol}", focus, methods=["POST"]), Route("/api/account", account),
        Route("/api/orders", place, methods=["POST"]),
        Route("/api/orders/from-signal/{signal_id}", from_signal, methods=["POST"]),
        Route("/api/positions/{pid}/close", close, methods=["POST"]),
        Route("/api/positions/{pid}/breakeven", breakeven, methods=["POST"]),
        Route("/api/kill", kill, methods=["POST"]), Route("/api/kill/reset", kill_reset, methods=["POST"]),
        Route("/api/settings", get_settings, methods=["GET"]), Route("/api/settings", put_settings, methods=["PUT", "POST"]),
        Route("/api/logs", logs), WebSocketRoute("/ws", ws),
    ]
    dist = ui_dir or (REPO_ROOT / "frontend" / "dist")
    if dist.exists():
        index = dist / "index.html"
        routes.append(Mount("/assets", app=StaticFiles(directory=dist / "assets"), name="assets"))

        async def spa(req):
            path = req.path_params.get("path", "")
            f = (dist / path).resolve()
            if path and f.is_file() and f.is_relative_to(dist.resolve()):
                return FileResponse(f)
            if path.startswith("api/"):
                return Response(status_code=404)
            return FileResponse(index)

        routes.append(Route("/{path:path}", spa))

    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: http_error},
                    middleware=[Middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                                           allow_methods=["*"], allow_headers=["*"])])
    app.state.engine = eng
    return app
