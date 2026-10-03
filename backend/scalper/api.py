"""FastAPI gateway: REST endpoints + WebSocket push for the web UI."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import REPO_ROOT, Cfg
from .engine import Engine

log = logging.getLogger("scalper.api")


class OrderIn(BaseModel):
    symbol: str
    side: str                       # LONG | SHORT
    stop: float | None = None
    targets: list[float] | None = None
    risk_scale: float = 1.0
    signal_id: str | None = None
    trailing_atr: float | None = None


def create_app(cfg: Cfg, engine: Engine | None = None, start_engine: bool = True) -> FastAPI:
    eng = engine or Engine(cfg)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(eng.start()) if start_engine else None
        yield
        if task:
            task.cancel()
        await eng.stop()

    app = FastAPI(title="Scalping Terminal", version="0.1.0", lifespan=lifespan)
    app.state.engine = eng
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/status")
    def status():
        return eng.status()

    @app.get("/api/watchlist")
    def watchlist():
        return eng.watchlist()

    @app.get("/api/chart/{symbol}")
    async def chart(symbol: str, tf: str = "1m", limit: int = 600):
        symbol = symbol.upper()
        if symbol not in eng.states:
            ok = await eng.add_symbol(symbol)
            if not ok:
                raise HTTPException(404, f"unknown symbol {symbol}")
        data = eng.chart(symbol, tf, min(limit, 1000))
        if data is None:
            raise HTTPException(404, f"no data for {symbol} {tf}")
        return JSONResponse(data)

    @app.get("/api/signals")
    def signals(limit: int = 200, symbol: str | None = None):
        recent = [r for r in eng.store.recent("signals", limit, symbol) if "strategy" in r]
        return {"active": eng.active_signals(), "recent": recent,
                "rejected": eng.signal_engine.rejected[-100:]}

    @app.get("/api/trends")
    def trends():
        return eng.trend_board()

    @app.get("/api/trend-records")
    def trend_records(symbol: str | None = None, limit: int = 200):
        return eng.store.recent("trends", limit, symbol)

    @app.get("/api/radar")
    def radar():
        return {"active": eng.pump.radar(), "recent": eng.store.recent("move_events", 100),
                "continuation": eng.pump.continuation.summary()}

    @app.get("/api/coupling")
    def coupling():
        return eng.coupling_table()

    @app.get("/api/news")
    def news(symbol: str | None = None, limit: int = 100):
        base = symbol.upper().removesuffix("USDT") if symbol else None
        return {"items": eng.news.recent(base, limit), "events": eng.news.upcoming_events(),
                "fear_greed": eng.news.fear_greed, "health": eng.news.health,
                "macro_block_now": eng.news.macro_block(int(time.time() * 1000))}

    @app.get("/api/strategies")
    def strategies():
        out = []
        summ = eng.stats.summary()
        models = eng.model.summary() if eng.model else {}
        for sid, s in eng.strategies.items():
            out.append({"id": sid, "version": s.version, "description": s.description,
                        "timeframes": list(getattr(s, "timeframes", ())), "regimes": list(s.regimes),
                        "needs": list(s.needs), "stats": summ.get(sid), "model": models.get(sid)})
        return {"strategies": out, "bootstrap": eng.backtest_report}

    @app.get("/api/book/{symbol}")
    def book(symbol: str):
        symbol = symbol.upper()
        eng.set_focus(symbol)
        ob = eng.books.get(symbol)
        if not ob or not ob.synced:
            return {"synced": False}
        bids, asks = ob.top(25)
        return {"synced": True, "bids": bids, "asks": asks, "metrics": eng.book_cache.get(symbol)}

    @app.post("/api/focus/{symbol}")
    def focus(symbol: str):
        eng.set_focus(symbol.upper())
        return {"ok": True}

    # ---- paper trading ------------------------------------------------------------------
    @app.get("/api/account")
    def account():
        return eng.broker.account()

    @app.post("/api/orders")
    def place(o: OrderIn):
        sym = o.symbol.upper()
        st = eng.states.get(sym)
        atr = st.bundles["5m"].v.get("atr") if st and "5m" in st.bundles else None
        info = eng.symbol_info.get(sym, {})
        step = min_notional = None
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step = float(f["stepSize"])
            if f.get("filterType") in ("NOTIONAL", "MIN_NOTIONAL"):
                min_notional = float(f.get("minNotional") or 0)
        pos, err = eng.broker.open(sym, o.side.upper(), o.stop, o.targets, risk_scale=o.risk_scale,
                                   signal_id=o.signal_id, trailing_atr=o.trailing_atr, atr=atr,
                                   step=step, min_notional=min_notional or 0.0)
        if err:
            raise HTTPException(400, err)
        eng.hub.publish({"type": "position_opened", "data": pos.to_json()})
        return pos.to_json()

    @app.post("/api/orders/from-signal/{signal_id}")
    def from_signal(signal_id: str, risk_scale: float = 1.0):
        sig = eng.signals.get(signal_id)
        if not sig:
            raise HTTPException(404, "signal not active")
        c = sig.candidate
        scale = risk_scale * c.features.get("risk_scale", 1.0)
        o = OrderIn(symbol=c.symbol, side=c.side, stop=c.stop, targets=[t.price for t in c.targets],
                    risk_scale=scale, signal_id=signal_id,
                    trailing_atr=c.features.get("trail_atr") if c.trailing else None)
        return place(o)

    @app.post("/api/positions/{pid}/close")
    def close(pid: str):
        pos = eng.broker.close(pid)
        if not pos:
            raise HTTPException(404, "no such position")
        eng.store.put("paper_trades", pos.id, pos.closed_at, pos.sim.c.symbol, pos.to_json())
        eng.hub.publish({"type": "position_closed", "data": pos.to_json()})
        return pos.to_json()

    @app.post("/api/positions/{pid}/breakeven")
    def breakeven(pid: str):
        return {"ok": eng.broker.move_stop_to_breakeven(pid)}

    @app.post("/api/kill")
    def kill():
        closed = eng.broker.kill()
        for p in closed:
            eng.store.put("paper_trades", p.id, p.closed_at, p.sim.c.symbol, p.to_json())
        eng.hub.publish({"type": "kill", "closed": len(closed)})
        return {"closed": len(closed), "risk": eng.risk.status(eng.broker.equity, 0)}

    @app.post("/api/kill/reset")
    def kill_reset():
        eng.risk.killed = False
        return {"ok": True}

    # ---- websocket ----------------------------------------------------------------------
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q = eng.hub.subscribe()
        try:
            await sock.send_json({"type": "status", "data": eng.status()})

            async def reader():
                while True:
                    msg = await sock.receive_json()
                    if msg.get("type") == "focus" and msg.get("symbol"):
                        eng.set_focus(msg["symbol"].upper())

            rt = asyncio.create_task(reader())
            try:
                while True:
                    msg = await q.get()
                    await sock.send_json(msg)
            finally:
                rt.cancel()
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug("ws closed: %s", e)
        finally:
            eng.hub.unsubscribe(q)

    dist = REPO_ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            f = dist / path
            if path and f.is_file() and Path(f).resolve().is_relative_to(dist.resolve()):
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app
