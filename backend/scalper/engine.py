"""Live engine: market data -> bars/indicators -> detectors -> strategies -> signals -> UI/alerts/paper."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .alerts import Alerts, fmt_pump, fmt_signal, fmt_trend
from .binance import BinanceREST, BookMetrics, LocalOrderBook, StreamManager, Tape
from .config import Cfg, data_dir
from .detectors.coupling import CouplingTracker
from .detectors.pump import PumpDetector
from .detectors.trend import STATES
from .execution import PaperBroker, RiskConfig, RiskEngine
from .indicators.bundle import IndicatorBundle
from .models import TF_MS, Bar, Signal, Trade, now_ms
from .news import NewsService
from .signals import SignalEngine, StatsStore
from .signals.store import strategy_id
from .sim import Costs, TradeSim
from .storage import ParquetRecorder, Store
from .strategies import BAR_STRATEGIES, Context, build_strategies
from .strategies.builtin import BtcCatchup, OrderflowMomentum, PumpMomentum
from .symbol_state import SymbolState

log = logging.getLogger("scalper.engine")
LEADER = "BTCUSDT"
STABLE_LIKE = ("UP", "DOWN", "BULL", "BEAR")


class Hub:
    """Fan-out of JSON messages to connected UI websockets."""

    def __init__(self):
        self.clients: set[asyncio.Queue] = set()

    def publish(self, msg: dict) -> None:
        for q in list(self.clients):
            if q.qsize() < 2000:
                q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)


def _bootstrap_job(cfg: dict, symbols: list[str], days: int, ddir: str) -> dict:
    """Runs in a worker process: backtest recent history to seed per-strategy statistics."""
    from .backtest.data import load_history
    from .backtest.engine import Backtester
    data = {}
    for s in symbols:
        try:
            data[s] = load_history(s, "1m", days, Path(ddir), "spot", cfg["binance"]["spot_rest"])
        except Exception:  # noqa: BLE001
            continue
    res = Backtester(cfg, cfg.get("execution", {}).get("market", "spot")).run(data)
    return {"by_strategy": res.by_strategy(), "metrics": res.metrics(),
            "trend": {tf: s.summary() for tf, s in res.trend_stats.items()},
            "symbols": list(data), "days": days}


class Engine:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.ddir = data_dir(cfg)
        self.market = cfg.get_path("execution.market", "spot")
        self.tfs = list(cfg.get_path("timeframes.trend", ["1m", "5m", "15m", "1h", "4h"]))
        self.store = Store(self.ddir / "scalper.db")
        self.stats = StatsStore(self.ddir / "stats.json")
        self.model = self._load_model()
        self.spot = BinanceREST(cfg.binance.spot_rest, weight_limit=cfg.get_path("binance.weight_limit_per_min", 6000),
                                safety=cfg.get_path("binance.weight_safety", 0.7))
        self.fut = BinanceREST(cfg.binance.futures_rest, futures=True) if cfg.get_path("binance.enable_futures", True) else None
        self.futures_ok = False
        self.trend_stats: dict = {}
        self.states: dict[str, SymbolState] = {}
        cp = cfg.get_path("coupling", {})
        self.coupling = CouplingTracker(LEADER, cp.get("window_short", 240), cp.get("window_long", 1440),
                                        cp.get("follower_rho", 0.7), cp.get("follower_r2", 0.5),
                                        cp.get("partial_rho", 0.4), cp.get("leadlag_max_lag", 10))
        self.pump = PumpDetector(dict(cfg.get_path("pump", {})))
        news_cfg = {**dict(cfg.get_path("news", {})),
                    "macro_block_before_min": cfg.get_path("signals.macro_block_before_min", 15),
                    "macro_block_after_min": cfg.get_path("signals.macro_block_after_min", 30)}
        self.news = NewsService(news_cfg, on_item=self._on_news)
        self.pump.catalyst_lookup = self.news.catalyst
        self.costs = Costs.from_cfg(dict(cfg.get_path("fees", {})), self.market)
        self.risk = RiskEngine(RiskConfig.from_cfg(dict(cfg.get_path("risk", {}))))
        self.broker = PaperBroker(self.risk, self.costs, self.market)
        self.strategies = build_strategies(cfg.get_path("signals.strategies", {}))
        self.bar_strats = {k: s for k, s in self.strategies.items() if isinstance(s, BAR_STRATEGIES)}
        self.signal_engine = SignalEngine(dict(cfg.signals), self.stats, self.costs, self.model,
                                          macro_block=self.news.macro_block, coin_veto=self.news.coin_veto,
                                          coin_sentiment=self.news.coin_sentiment,
                                          risk_gate=lambda: self.risk.lock_reason(self.broker.equity))
        self.signal_engine.regimes_from(self.strategies)
        self.signals: dict[str, Signal] = {}
        self.signal_sims: dict[str, TradeSim] = {}
        self.recent_signals: deque = deque(maxlen=300)
        self.alerts = Alerts(dict(cfg.get_path("alerts", {})))
        self.hub = Hub()
        self.books: dict[str, LocalOrderBook] = {}
        self.tapes: dict[str, Tape] = {}
        self.book_metrics: dict[str, BookMetrics] = {}
        self.book_cache: dict[str, dict] = {}
        self.focus: dict[str, float] = {}           # symbol -> last time the UI looked at it
        self.escalated: dict[str, float] = {}       # pump tier-2 symbols -> until
        self.derivs: dict[str, dict] = {}
        self.liq: dict[str, deque] = {}
        self.liq_base: dict[str, tuple[float, float]] = {}
        self.oi_hist: dict[str, deque] = {}
        self.universe: list[dict] = []
        self.trend_syms: list[str] = []
        self.radar_syms: list[str] = []
        self.symbol_info: dict[str, dict] = {}
        self.futures_syms: set[str] = set()
        self.phase = "init"
        self.errors: deque = deque(maxlen=50)
        self.clock_offset = 0
        self.started_at = time.time()
        self.backtest_report: dict | None = None
        self._tasks: list[asyncio.Task] = []
        self._last_tick_eval: dict[str, float] = {}
        self._last_book_push: dict[str, float] = {}
        self._trade_buf: dict[str, list] = {}
        self._last_msg_ms: dict[str, int] = {}
        self.recorder = ParquetRecorder(self.ddir / "recordings") if cfg.get_path("storage.record_trades") else None
        self.ws = StreamManager(cfg.binance.spot_ws, self._on_spot, cfg.get_path("binance.max_streams_per_connection", 200))
        self.ws_fut = StreamManager(cfg.binance.futures_ws, self._on_fut, 200) if self.fut else None

    def _load_model(self):
        p = self.ddir / "models.joblib"
        if not p.exists():
            return None
        try:
            from .ml.meta import MetaModel
            return MetaModel(p)
        except Exception as e:  # noqa: BLE001
            log.warning("could not load ML models: %s", e)
            return None

    # ======================================================================== lifecycle
    async def start(self) -> None:
        try:
            self.phase = "universe"
            await self._sync_clock()
            await self._load_universe()
            self.phase = "backfill"
            await self._backfill_all()
            self.ws.subscribe([f"{s.lower()}@kline_1m" for s in self.trend_syms])
            self._tasks += [asyncio.create_task(self._radar_start()),
                            asyncio.create_task(self._periodic()),
                            asyncio.create_task(self._futures_start())]
            if self.cfg.get_path("news.enabled", True):
                self._tasks.append(asyncio.create_task(self.news.poll_forever()))
            if self.cfg.get_path("bootstrap.enabled", True) and not self.stats.backtest:
                self._tasks.append(asyncio.create_task(self._bootstrap()))
            self.phase = "live"
            log.info("engine live: %d trend symbols, %d radar symbols", len(self.trend_syms), len(self.radar_syms))
        except Exception as e:  # noqa: BLE001
            self.phase = "error"
            self.errors.append({"ts": now_ms(), "error": f"startup: {e}"})
            log.exception("startup failed")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.ws.close()
        if self.ws_fut:
            await self.ws_fut.close()
        self.stats.save()
        if self.recorder:
            self.recorder.flush()

    async def _sync_clock(self) -> None:
        try:
            t0 = time.time()
            st = await self.spot.server_time()
            self.clock_offset = int(st - (t0 + time.time()) / 2 * 1000)
        except Exception as e:  # noqa: BLE001
            self.errors.append({"ts": now_ms(), "error": f"clock sync: {e}"})

    async def _load_universe(self) -> None:
        u = self.cfg.universe
        info = await self.spot.exchange_info()
        tick = await self.spot.tickers_24h()
        quote = u.get("quote", "USDT")
        excl = set(u.get("exclude_bases", []))
        syms = {}
        for s in info["symbols"]:
            if s.get("status") != "TRADING" or s.get("quoteAsset") != quote or s["baseAsset"] in excl:
                continue
            if any(s["baseAsset"].endswith(x) for x in STABLE_LIKE) and len(s["baseAsset"]) > 4:
                continue
            syms[s["symbol"]] = s
        self.symbol_info = syms
        rows = []
        for t in tick:
            if t["symbol"] in syms:
                rows.append({"symbol": t["symbol"], "base": syms[t["symbol"]]["baseAsset"],
                             "price": float(t["lastPrice"]), "change_pct": float(t["priceChangePercent"]),
                             "quote_volume": float(t["quoteVolume"]), "high": float(t["highPrice"]), "low": float(t["lowPrice"])})
        rows.sort(key=lambda r: -r["quote_volume"])
        self.universe = rows
        n = u.get("trend_universe_size", 60)
        trend = [r["symbol"] for r in rows[:n]]
        for s in u.get("always_include", []):
            if s in syms and s not in trend:
                trend.append(s)
        if LEADER in trend:
            trend.remove(LEADER)
        self.trend_syms = [LEADER] + trend
        radar = [r["symbol"] for r in rows if r["quote_volume"] >= u.get("radar_min_quote_volume", 2e6)]
        self.radar_syms = radar[: u.get("radar_max_symbols", 400)]
        for s in self.trend_syms:
            if s not in self.radar_syms:
                self.radar_syms.append(s)
        for r in rows:
            self.pump.quote_volume[r["symbol"]] = r["quote_volume"]
        self.news.set_universe({r["base"] for r in rows})

    async def _backfill_symbol(self, sym: str) -> None:
        st = SymbolState(sym, self.tfs, self.cfg.get_path("trend.sensitivity", "balanced"), self.trend_stats,
                         self.cfg.get_path("trend.chandelier_atr", 3.0), self.cfg.get_path("trend.mature_atr", 3.0))
        n = self.cfg.get_path("timeframes.history_bars", 600)
        now = now_ms() + self.clock_offset
        bars_1m = []
        for tf in self.tfs:
            raw = await self.spot.klines(sym, tf, limit=min(n, 1000))
            bars = [Bar.from_kline(k) for k in raw]
            bars = [b for b in bars if b.open_time + TF_MS[tf] <= now]          # closed only
            if tf == "1m":
                bars_1m = bars
            st.seed(tf, bars, self._market_fn(sym))
        st.seed_forming(bars_1m)
        for b in bars_1m:
            self.coupling.add_bar(sym, b.open_time, b.close)
        st.last_update_ms = now
        r = next((x for x in self.universe if x["symbol"] == sym), None)
        if r:
            st.quote_volume_24h, st.price_change_24h = r["quote_volume"], r["change_pct"]
        self.states[sym] = st

    async def _backfill_all(self) -> None:
        await self._backfill_symbol(LEADER)
        sem = asyncio.Semaphore(4)

        async def one(s):
            async with sem:
                try:
                    await self._backfill_symbol(s)
                except Exception as e:  # noqa: BLE001
                    self.errors.append({"ts": now_ms(), "error": f"backfill {s}: {e}"})
        await asyncio.gather(*(one(s) for s in self.trend_syms if s != LEADER))
        for s in self.trend_syms:
            if s in self.states and s != LEADER:
                self.coupling.compute(s)

    async def add_symbol(self, sym: str) -> bool:
        """Start tracking a symbol on demand (e.g. the user opened its chart)."""
        if sym in self.states:
            return True
        if sym not in self.symbol_info:
            return False
        await self._backfill_symbol(sym)
        self.trend_syms.append(sym)
        self.ws.subscribe([f"{sym.lower()}@kline_1m", f"{sym.lower()}@kline_1s"])
        return True

    async def _radar_start(self) -> None:
        """Warm the pump detector with recent 1s klines, then subscribe whole-market 1s streams."""
        sem = asyncio.Semaphore(4)

        async def warm(s):
            async with sem:
                try:
                    raw = await self.spot.klines(s, "1s", limit=600)
                    for k in raw[:-1]:
                        b = Bar.from_kline(k)
                        self.pump.on_bar_1s(s, b)
                        self.coupling.add_fast(s, b.open_time, b.close)
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.gather(*(warm(s) for s in self.radar_syms))
        self.ws.subscribe([f"{s.lower()}@kline_1s" for s in self.radar_syms])

    async def _futures_start(self) -> None:
        if not self.fut:
            return
        try:
            info = await self.fut.exchange_info()
            self.futures_syms = {s["symbol"] for s in info["symbols"] if s.get("status") == "TRADING"}
            await self._poll_premium()
            self.futures_ok = True
        except Exception as e:  # noqa: BLE001
            self.futures_ok = False
            self.errors.append({"ts": now_ms(), "error": f"futures data unavailable ({e}); derivatives features off"})
            log.warning("futures data unavailable: %s", e)
            return
        self.ws_fut.subscribe(["!forceOrder@arr"])
        while True:
            try:
                await self._poll_premium()
                await self._poll_oi()
            except Exception as e:  # noqa: BLE001
                self.errors.append({"ts": now_ms(), "error": f"futures poll: {e}"})
            await asyncio.sleep(60)

    async def _poll_premium(self) -> None:
        for p in await self.fut.premium_index():
            d = self.derivs.setdefault(p["symbol"], {})
            d["funding"] = float(p.get("lastFundingRate") or 0)
            d["mark"] = float(p.get("markPrice") or 0)
            d["next_funding"] = p.get("nextFundingTime")

    async def _poll_oi(self) -> None:
        for s in self.trend_syms:
            if s not in self.futures_syms:
                continue
            try:
                oi = float((await self.fut.open_interest(s))["openInterest"])
            except Exception:  # noqa: BLE001
                continue
            h = self.oi_hist.setdefault(s, deque(maxlen=30))
            h.append((now_ms(), oi))
            d = self.derivs.setdefault(s, {})
            d["oi"] = oi
            old = [x for x in h if now_ms() - x[0] >= 5 * 60_000]
            if old:
                d["oi_change_pct"] = 100 * (oi - old[-1][1]) / old[-1][1] if old[-1][1] else 0.0
                self.pump.set_derivs(s, oi_change_pct=d["oi_change_pct"], funding=d.get("funding"),
                                     oi_z=abs(d["oi_change_pct"]) / 1.0)
            # liquidation baseline (per-minute notional) for liq_z
            q = self.liq.get(s)
            last60 = sum(n for t, n, _ in q if now_ms() - t < 60_000) if q else 0.0
            m, v = self.liq_base.get(s, (last60, max(last60, 1.0) ** 2))
            a = 0.05
            self.liq_base[s] = (m + a * (last60 - m), (1 - a) * (v + a * (last60 - m) ** 2))

    async def _periodic(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(15)
            n += 1
            try:
                if n % 4 == 0:
                    for s in list(self.states):
                        if s != LEADER:
                            self.coupling.compute(s)
                    self.hub.publish({"type": "coupling", "data": self.coupling_table()})
                self.hub.publish({"type": "radar", "data": self.pump.radar()})
                self.hub.publish({"type": "status", "data": self.status()})
                now = time.time()
                for s, until in list(self.escalated.items()):
                    if now > until:
                        del self.escalated[s]
                        self._maybe_unsubscribe_detail(s)
                for s, ts in list(self.focus.items()):
                    if now - ts > 600:
                        del self.focus[s]
                        self._maybe_unsubscribe_detail(s)
                if n % 20 == 0:
                    self.stats.save()
                    await self._sync_clock()
                if n % 240 == 0:
                    await self._refresh_universe_stats()
            except Exception as e:  # noqa: BLE001
                self.errors.append({"ts": now_ms(), "error": f"periodic: {e}"})

    async def _refresh_universe_stats(self) -> None:
        tick = await self.spot.tickers_24h()
        by = {t["symbol"]: t for t in tick}
        for r in self.universe:
            t = by.get(r["symbol"])
            if t:
                r.update(price=float(t["lastPrice"]), change_pct=float(t["priceChangePercent"]),
                         quote_volume=float(t["quoteVolume"]))
                self.pump.quote_volume[r["symbol"]] = r["quote_volume"]

    async def _bootstrap(self) -> None:
        """Seed strategy statistics from a backtest over recent history (worker process)."""
        n_sym = self.cfg.get_path("bootstrap.symbols", 8)
        days = self.cfg.get_path("bootstrap.days", 7)
        syms = self.trend_syms[:n_sym]
        self.backtest_report = {"status": "running", "symbols": syms, "days": days}
        loop = asyncio.get_running_loop()
        try:
            with ProcessPoolExecutor(max_workers=1) as pool:
                res = await loop.run_in_executor(pool, _bootstrap_job, json.loads(json.dumps(self.cfg)), syms, days,
                                                 str(self.ddir))
            for sid, rs in res["by_strategy"].items():
                self.stats.set_backtest(sid, rs)
            self.stats.save()
            self.backtest_report = {"status": "done", **{k: res[k] for k in ("metrics", "trend", "symbols", "days")}}
        except Exception as e:  # noqa: BLE001
            self.backtest_report = {"status": "error", "error": str(e)}
            self.errors.append({"ts": now_ms(), "error": f"bootstrap backtest: {e}"})

    # ======================================================================== stream handlers
    async def _on_spot(self, stream: str, d: dict) -> None:
        kind = stream.split("@", 1)[1] if "@" in stream else stream
        sym = d.get("s")
        if kind == "kline_1m":
            k = d["k"]
            bar = Bar.from_kline(k)
            self._last_msg_ms[sym] = d.get("E", now_ms())
            st = self.states.get(sym)
            if not st:
                return
            if k["x"]:
                await self._on_1m_close(sym, st, bar)
            else:
                st.partial_1m = bar
                if sym in self.focus:
                    self.hub.publish({"type": "bar", "symbol": sym, "bar": bar.to_json(),
                                      "forming": {tf: (f.to_json() if (f := st.forming(tf)) else None) for tf in self.tfs}})
        elif kind == "kline_1s":
            k = d["k"]
            if k["x"]:
                self._on_1s(sym, Bar.from_kline(k))
        elif kind == "aggTrade":
            t = Trade(d["T"], float(d["p"]), float(d["q"]), d["m"], d["a"])
            tape = self.tapes.setdefault(sym, Tape())
            large = tape.add(t)
            self.broker.on_price(sym, t.price)
            buf = self._trade_buf.setdefault(sym, [])
            buf.append([t.ts, t.price, t.qty, t.side, large])
            if self.recorder:
                self.recorder.add("trades", sym, {"ts": t.ts, "price": t.price, "qty": t.qty, "m": t.is_buyer_maker, "a": t.agg_id})
            if len(buf) >= 40 or (buf and t.ts - buf[0][0] > 250):
                self.hub.publish({"type": "trades", "symbol": sym, "data": buf})
                self._trade_buf[sym] = []
        elif kind.startswith("depth"):
            ob = self.books.get(sym)
            if ob is None:
                return
            if not ob.on_diff(d):
                asyncio.create_task(self._snapshot(sym))
            await self._book_update(sym)
        elif kind == "bookTicker":
            self.broker.on_price(sym, (float(d["b"]) + float(d["a"])) / 2)

    async def _on_fut(self, stream: str, d: dict) -> None:
        if stream == "!forceOrder@arr":
            o = d.get("o", {})
            s = o.get("s")
            notional = float(o.get("q", 0)) * float(o.get("ap") or o.get("p") or 0)
            q = self.liq.setdefault(s, deque(maxlen=500))
            q.append((o.get("T", now_ms()), notional, o.get("S")))
            last60 = [x for x in q if now_ms() - x[0] < 60_000]
            total = sum(x[1] for x in last60)
            m, v = self.liq_base.get(s, (0.0, 1.0))
            z = (total - m) / (math.sqrt(v) or 1.0)
            side = max(("BUY", "SELL"), key=lambda sd: sum(x[1] for x in last60 if x[2] == sd))
            dd = self.derivs.setdefault(s, {})
            dd.update(liq_60s=total, liq_z=z, liq_side=side)
            self.pump.set_derivs(s, liq_z=z)
            self.hub.publish({"type": "liquidation", "symbol": s, "side": o.get("S"), "notional": notional,
                              "price": float(o.get("ap") or 0), "ts": o.get("T")})

    def _market_fn(self, sym: str):
        def market(tf: str) -> dict:
            out = {}
            lead = self.states.get(LEADER)
            if sym != LEADER and lead is not None:
                out["btc_dir"] = lead.trend_dir(tf)
                cp = self.coupling.get(sym)
                out["rho"] = cp["rho"] if cp else None
            d = self.derivs.get(sym)
            if d:
                out["oi_change_pct"] = d.get("oi_change_pct")
                out["funding"] = d.get("funding")
            return out
        return market

    def _context(self, sym: str, st: SymbolState, tf: str, events: list[dict]) -> Context:
        lead = self.states.get(LEADER)
        return Context(sym, tf, now_ms(), st.bundles[tf], st.bundles, st.trends,
                       lead.trends if lead and sym != LEADER else {}, self.coupling.get(sym),
                       self.derivs.get(sym, {}), self.book_cache.get(sym), events)

    async def _on_1m_close(self, sym: str, st: SymbolState, bar: Bar) -> None:
        closed, events = st.on_1m_close(bar, self._market_fn(sym))
        st.last_update_ms = now_ms()
        self.coupling.add_bar(sym, bar.open_time, bar.close)
        if sym not in self.radar_syms:
            self._advance_sims(sym, bar, 60_000)
        for ev in events:
            self._on_trend_event(sym, st, ev)
        cands = []
        for tf in closed:
            ctx = self._context(sym, st, tf, events)
            for strat in self.bar_strats.values():
                if tf not in strat.timeframes:
                    continue
                try:
                    cands += [(c, ctx) for c in strat.on_bar(ctx)]
                except Exception as e:  # noqa: BLE001
                    self.errors.append({"ts": now_ms(), "error": f"{strat.id} {sym}: {e}"})
        if sym == LEADER and "S12_btc_catchup" in self.strategies:
            cu: BtcCatchup = self.strategies["S12_btc_catchup"]
            for ev in events:
                if ev["state"] == "CONFIRMED" and ev["tf"] in ("1m", "5m") and st.trends[ev["tf"]].trend:
                    tr = st.trends[ev["tf"]].trend
                    ev2 = dict(ev, origin={"ts": tr.origin_ts, "price": tr.origin_price})
                    alts = {s: (o.bundles[ev["tf"]], self.coupling.get(s)) for s, o in self.states.items() if s != LEADER}
                    for c in cu.candidates(ev2, st.bundles[ev["tf"]], alts):
                        cands.append((c, self._context(c.symbol, self.states[c.symbol], ev["tf"], [])))
        for c, ctx in cands:
            self._submit(c, ctx)
        if sym in self.focus:
            self.hub.publish({"type": "bar_closed", "symbol": sym, "tfs": closed, "bar": bar.to_json(),
                              "trend": {tf: st.trends[tf].snapshot() for tf in self.tfs}})
        self.hub.publish({"type": "tick", "symbol": sym, "price": bar.close,
                          "trend": {tf: [st.trends[tf].direction, st.trends[tf].state] for tf in self.tfs}})

    def _on_1s(self, sym: str, bar: Bar) -> None:
        self.coupling.add_fast(sym, bar.open_time, bar.close)
        self._advance_sims(sym, bar, 1000)
        for pos in self.broker.on_bar_1s(sym, bar):
            self.store.put("paper_trades", pos.id, pos.closed_at or now_ms(), sym, pos.to_json())
            self.hub.publish({"type": "position_closed", "data": pos.to_json()})
        for ev in self.pump.on_bar_1s(sym, bar):
            self._on_pump_event(sym, ev)

    def _advance_sims(self, sym: str, bar: Bar, bar_ms: int) -> None:
        for sid, sim in list(self.signal_sims.items()):
            if sim.c.symbol != sym:
                continue
            sig = self.signals.get(sid)
            done = sim.on_bar(bar, bar_ms)
            if sig is None:
                continue
            prev = sig.status
            if sim.status == "open" and sim.targets_hit and not done:
                sig.status = "tp1_hit"
            elif sim.status == "open":
                sig.status = "triggered"
            if done:
                sig.status = {"stop": "stopped", "trailing_stop": "trail_exit", "breakeven": "breakeven",
                              "time_stop": "time_stopped", "expired": "expired"}.get(sim.exit_reason, "tp")
                sig.outcome_r = round(sim.r, 3)
                if sim.exit_reason != "expired":
                    self.stats.add(sig.candidate.strategy, sim.r, "live")
                del self.signal_sims[sid]
                self.signals.pop(sid, None)
            if sig.status != prev or done:
                body = sig.to_json()
                self.store.put("signals", sig.id, sig.candidate.ts, sym, body)
                self.hub.publish({"type": "signal_update", "data": body})

    # ======================================================================== events -> signals
    def _submit(self, c, ctx: Context | None) -> Signal | None:
        for s in self.signals.values():
            if s.candidate.symbol == c.symbol and strategy_id(s.candidate.strategy) == strategy_id(c.strategy) \
                    and s.candidate.tf == c.tf:
                return None                     # one active signal per symbol/strategy/timeframe
        age = (now_ms() - self._last_msg_ms.get(c.symbol, now_ms())) / 1000
        sig = self.signal_engine.evaluate(c, ctx, {"data_age_s": age})
        if sig is None:
            return None
        self.signals[sig.id] = sig
        self.signal_sims[sig.id] = TradeSim(c, self.costs, fill_price=c.entry, entry_time=c.ts)
        sig.status = "triggered"
        body = sig.to_json()
        self.recent_signals.append(sig.id)
        self.store.put("signals", sig.id, c.ts, c.symbol, body)
        self.hub.publish({"type": "signal", "data": body})
        if sig.validated and (sig.p_win or 0) >= self.cfg.get_path("alerts.min_p_win", 0.55):
            self.alerts.send(fmt_signal(body), key=f"sig:{c.symbol}:{c.strategy}")
        return sig

    def _on_trend_event(self, sym: str, st: SymbolState, ev: dict) -> None:
        tc = st.trends[ev["tf"]]
        align = sum(1 for t in st.trends.values() if t.direction == tc.direction and t.state in (1, 2, 3, 4))
        ev = {**ev, "alignment": f"{align}/{len(st.trends)}", "btc_coupling": self.coupling.badge(sym)}
        if tc.trend:
            ev["trend"] = tc.trend.to_json()
            self.store.put("trends", tc.trend.id, ev["ts"], sym, ev["trend"])
        self.hub.publish({"type": "trend", "data": ev})
        if ev["state"] == "CONFIRMED" and align >= self.cfg.get_path("alerts.trend_min_alignment", 3) and ev["tf"] != "1m":
            self.alerts.send(fmt_trend(ev, ev["alignment"]), key=f"trend:{sym}:{ev['tf']}", min_interval=900)

    def _on_pump_event(self, sym: str, ev: dict) -> None:
        if ev["stage"] in ("IGNITION", "CONFIRMED"):
            self.escalated[sym] = time.time() + 900
            self._subscribe_detail(sym)
        if ev.get("event"):
            self.store.put("move_events", ev["event"]["id"], ev["event"]["ignition"]["ts"] if ev["event"].get("ignition") else ev["ts"],
                           sym, ev["event"])
        self.hub.publish({"type": "pump", "data": ev})
        if ev["stage"] in ("IGNITION", "CONFIRMED", "EXHAUSTION") and self.cfg.get_path("alerts.pump_alerts", True):
            qv = self.pump.quote_volume.get(sym, 0)
            if qv >= self.cfg.get_path("universe.radar_min_quote_volume", 2e6):
                self.alerts.send(fmt_pump(ev), key=f"pump:{sym}:{ev['stage']}", min_interval=300)
        if "S11_pump_momentum" in self.strategies and ev.get("event"):
            strat: PumpMomentum = self.strategies["S11_pump_momentum"]
            c = strat.from_event(ev, None, self.market)
            if c:
                st = self.states.get(sym)
                ctx = self._context(sym, st, "1m", []) if st else None
                self._submit(c, ctx)

    def _on_news(self, item) -> None:
        self.store.put("news", item.id, item.ts, ",".join(item.symbols), item.to_json())
        if self.phase == "live":
            self.hub.publish({"type": "news", "data": item.to_json()})
            if item.impact >= 60:
                self.alerts.send(f"📰 [{item.event_type}] {item.title} ({', '.join(item.symbols) or 'market'})",
                                 key=f"news:{item.id}")

    # ======================================================================== order book (tier 2 / focus)
    def _subscribe_detail(self, sym: str) -> None:
        if sym in self.books:
            return
        self.books[sym] = LocalOrderBook(sym)
        self.book_metrics[sym] = BookMetrics()
        s = sym.lower()
        self.ws.subscribe([f"{s}@aggTrade", f"{s}@depth@100ms", f"{s}@bookTicker"])
        asyncio.create_task(self._snapshot(sym, delay=1.5))

    def _maybe_unsubscribe_detail(self, sym: str) -> None:
        if sym in self.focus or sym in self.escalated or sym not in self.books:
            return
        s = sym.lower()
        self.ws.unsubscribe([f"{s}@aggTrade", f"{s}@depth@100ms", f"{s}@bookTicker"])
        for d in (self.books, self.book_metrics, self.book_cache, self.tapes):
            d.pop(sym, None)

    async def _snapshot(self, sym: str, delay: float = 0.0) -> None:
        if delay:
            await asyncio.sleep(delay)
        ob = self.books.get(sym)
        if ob is None or ob.synced:
            return
        try:
            ob.on_snapshot(await self.spot.depth(sym, 1000))
        except Exception as e:  # noqa: BLE001
            self.errors.append({"ts": now_ms(), "error": f"snapshot {sym}: {e}"})

    async def _book_update(self, sym: str) -> None:
        now = time.time()
        if now - self._last_book_push.get(sym, 0) < 0.25:
            return
        self._last_book_push[sym] = now
        ob = self.books[sym]
        if not ob.synced:
            return
        m = self.book_metrics[sym].compute(ob, self.tapes.get(sym))
        if not m:
            return
        self.book_cache[sym] = m
        self.pump.set_book_pressure(sym, m["pressure_z"])
        bids, asks = ob.top(25)
        if self.recorder:
            self.recorder.add("book", sym, {"ts": now_ms(), "bids": bids, "asks": asks})
        self.hub.publish({"type": "book", "symbol": sym, "bids": bids, "asks": asks,
                          "metrics": {k: v for k, v in m.items() if k != "walls"}, "walls": m["walls"]})
        if "S5_orderflow_momentum" in self.strategies and now - self._last_tick_eval.get(sym, 0) > 1.0:
            self._last_tick_eval[sym] = now
            st = self.states.get(sym)
            if st and st.bundles["1m"].ready:
                ctx = self._context(sym, st, "1m", [])
                strat: OrderflowMomentum = self.strategies["S5_orderflow_momentum"]
                for c in strat.on_tick(ctx):
                    self._submit(c, ctx)

    # ======================================================================== API helpers
    def set_focus(self, sym: str) -> None:
        self.focus[sym] = time.time()
        self._subscribe_detail(sym)

    def status(self) -> dict:
        return {"phase": self.phase, "uptime_s": int(time.time() - self.started_at), "market": self.market,
                "execution_mode": self.cfg.get_path("execution.mode", "paper"),
                "trend_symbols": len(self.trend_syms), "radar_symbols": len(self.radar_syms),
                "ws": self.ws.status(), "ws_futures": self.ws_fut.status() if self.ws_fut else None,
                "futures_data": self.futures_ok, "clock_offset_ms": self.clock_offset,
                "weight_used": self.spot.gov.used, "news_health": self.news.health,
                "alerts_enabled": self.alerts.enabled, "model_loaded": self.model is not None,
                "errors": list(self.errors)[-10:], "fear_greed": self.news.fear_greed,
                "bootstrap": {k: v for k, v in (self.backtest_report or {}).items() if k in ("status", "symbols", "days", "error")}}

    def watchlist(self) -> list[dict]:
        out = []
        for r in self.universe:
            s = r["symbol"]
            st = self.states.get(s)
            row = {**r}
            if st:
                b = st.bundles["1m"].last
                if b:
                    row["price"] = b.close if not st.partial_1m else st.partial_1m.close
                row["trend"] = {tf: {"dir": st.trends[tf].direction, "state": STATES[st.trends[tf].state],
                                     "score": round(st.trends[tf].score)} for tf in self.tfs}
                row["coupling"] = self.coupling.badge(s)
                v = st.bundles["5m"].v if "5m" in st.bundles else {}
                row["rvol_5m"] = round(v.get("rvol", 0) or 0, 2)
                row["atr_pct_5m"] = round(100 * (v.get("atr") or 0) / (v.get("close") or 1), 3)
                row["tracked"] = True
            ev = self.pump.events.get(s)
            if ev:
                row["pump"] = {"stage": ev.stage, "direction": "PUMP" if ev.direction > 0 else "DUMP",
                               "move_pct": round(ev.move_pct, 2)}
            row["signals"] = sum(1 for x in self.signals.values() if x.candidate.symbol == s)
            row["opportunity"] = self._opportunity(row)
            out.append(row)
        return out

    def _opportunity(self, row: dict) -> float:
        score = 0.0
        tr = row.get("trend") or {}
        dirs = [v["dir"] for v in tr.values() if v["state"] in ("EARLY", "CONFIRMED", "MATURE")]
        if dirs:
            score += 10 * abs(sum(dirs))
        new_conf = sum(1 for v in tr.values() if v["state"] == "CONFIRMED")
        score += 8 * new_conf
        score += 15 * row.get("signals", 0)
        if row.get("pump"):
            score += {"IGNITION": 40, "CONFIRMED": 35, "EXHAUSTION": 10}.get(row["pump"]["stage"], 0)
        score += 5 * min(row.get("rvol_5m", 0), 4)
        return round(score, 1)

    def trend_board(self) -> dict:
        rows = []
        for s, st in self.states.items():
            cells = {}
            for tf in self.tfs:
                tc = st.trends[tf]
                cells[tf] = {"dir": tc.direction, "state": STATES[tc.state], "score": round(tc.score),
                             "since": tc.trend.history[-1][1] if tc.trend else None}
            align_up = sum(1 for c in cells.values() if c["dir"] > 0 and c["state"] not in ("NONE", "ENDED"))
            align_dn = sum(1 for c in cells.values() if c["dir"] < 0 and c["state"] not in ("NONE", "ENDED"))
            newest = max((c["since"] or 0) for c in cells.values())
            rows.append({"symbol": s, "cells": cells, "alignment": max(align_up, align_dn),
                         "direction": "UP" if align_up > align_dn else ("DOWN" if align_dn > align_up else None),
                         "newest": newest, "coupling": self.coupling.badge(s)})
        stats = {tf: s.summary() for tf, s in self.trend_stats.items()}
        return {"tfs": self.tfs, "rows": rows, "stats": stats}

    def coupling_table(self) -> list[dict]:
        return [r for r in self.coupling.results.values()]

    def chart(self, sym: str, tf: str, limit: int = 600) -> dict | None:
        st = self.states.get(sym)
        if st is None or tf not in st.bundles:
            return None
        self.set_focus(sym)
        src = list(st.bundles[tf].bars)
        fresh = IndicatorBundle(tf)
        series: dict[str, list] = {k: [] for k in ("ema9", "ema21", "ema50", "ema200", "vwap", "vwap_u1", "vwap_l1",
                                                    "vwap_u2", "vwap_l2", "bb_up", "bb_lo", "st", "st_dir", "rsi",
                                                    "cvd", "delta", "macd_hist", "kama")}
        for b in src:
            v = fresh.update(b)
            for k in ("ema9", "ema21", "ema50", "ema200", "vwap", "bb_up", "bb_lo", "st", "st_dir", "rsi", "cvd",
                      "delta", "macd_hist", "kama"):
                series[k].append(v.get(k))
            sg = v.get("vwap_sigma") or 0
            vw = v.get("vwap")
            for name, mult in (("vwap_u1", 1), ("vwap_l1", -1), ("vwap_u2", 2), ("vwap_l2", -2)):
                series[name].append(vw + mult * sg if vw is not None else None)
        cut = max(0, len(src) - limit)
        bars = [b.to_json() for b in src[cut:]]
        series = {k: v[cut:] for k, v in series.items()}
        forming = st.forming(tf)
        tc = st.trends[tf]
        lead = self.states.get(LEADER)
        base = sym[:-4] if sym.endswith("USDT") else sym
        btc_overlay = None
        if lead and sym != LEADER and tf in lead.bundles:
            lb = {b.open_time: b.close for b in lead.bundles[tf].bars}
            btc_overlay = [[b["t"], lb.get(b["t"])] for b in bars]
        return {
            "symbol": sym, "tf": tf, "bars": bars, "forming": forming.to_json() if forming else None,
            "series": series, "trend_history": tc.history()[-len(bars):],
            "trend": tc.snapshot(st.bundles[tf], self.coupling.badge(sym)),
            "trend_records": [r.to_json() for r in tc.records if r.origin_ts >= (bars[0]["t"] if bars else 0)],
            "mtf": {t: {"dir": st.trends[t].direction, "state": STATES[st.trends[t].state], "score": round(st.trends[t].score)}
                    for t in self.tfs},
            "btc_mtf": ({t: {"dir": lead.trends[t].direction, "state": STATES[lead.trends[t].state]} for t in self.tfs}
                        if lead else None),
            "coupling": self.coupling.get(sym), "btc_overlay": btc_overlay,
            "signals": self.store.recent("signals", 100, sym),
            "pump_events": self.pump.recent(sym),
            "news": self.news.recent(base, 30),
            "derivs": self.derivs.get(sym), "book": self.book_cache.get(sym),
            "regime": st.bundles[tf].regime(),
            "indicators": {k: (round(v, 8) if isinstance(v, float) else v) for k, v in st.bundles[tf].v.items()},
        }

    def active_signals(self) -> list[dict]:
        return [s.to_json() for s in self.signals.values()]
