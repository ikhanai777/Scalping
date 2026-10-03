"""Event-driven portfolio backtester (spec §8) running the live code path over historical 1m bars."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from ..detectors.coupling import CouplingTracker
from ..detectors.pump import PumpDetector
from ..detectors.trend import TrendStats
from ..models import Bar
from ..signals.engine import SignalEngine
from ..signals.store import StatsStore, strategy_id
from ..sim import Costs, TradeSim
from ..stats import OutcomeStats
from ..strategies.base import Context
from ..strategies.builtin import BAR_STRATEGIES, BtcCatchup, PumpMomentum, build_strategies
from ..symbol_state import SymbolState

LEADER = "BTCUSDT"


@dataclass
class BacktestResult:
    trades: list[dict] = field(default_factory=list)
    features: list[dict] = field(default_factory=list)      # ML rows aligned with trades
    trend_stats: dict[str, TrendStats] = field(default_factory=dict)
    trend_records: list[dict] = field(default_factory=list)
    start: int = 0
    end: int = 0

    def by_strategy(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for t in self.trades:
            out.setdefault(strategy_id(t["strategy"]), []).append(t["r"])
        return out

    def metrics(self, windows: int = 6) -> dict:
        res = {}
        for sid, rs in self.by_strategy().items():
            trades = [t for t in self.trades if strategy_id(t["strategy"]) == sid]
            res[sid] = strategy_metrics(trades, self.start, self.end, windows)
        res["_all"] = strategy_metrics(self.trades, self.start, self.end, windows)
        return res


def max_drawdown(rs: list[float]) -> float:
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def bootstrap_ci(rs: list[float], n: int = 1000, seed: int = 0) -> tuple[float, float]:
    if len(rs) < 5:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(rs) for _ in rs) / len(rs) for _ in range(n))
    return means[int(0.05 * n)], means[int(0.95 * n)]


def strategy_metrics(trades: list[dict], start: int, end: int, windows: int = 6) -> dict:
    trades = sorted((t for t in trades if t["exit_reason"] != "expired"), key=lambda t: t["signal_ts"])
    rs = [t["r"] for t in trades]
    st = OutcomeStats()
    for r in rs:
        st.add(r)
    out = st.summary()
    out["total_r"] = round(sum(rs), 3)
    out["max_dd_r"] = round(max_drawdown(rs), 3)
    sd = (sum((r - st.expectancy()) ** 2 for r in rs) / (len(rs) - 1)) ** 0.5 if len(rs) > 1 else 0
    out["sharpe_per_trade"] = round(st.expectancy() / sd, 3) if sd else None
    lo, hi = bootstrap_ci(rs)
    out["expectancy_ci90"] = [round(lo, 4), round(hi, 4)] if not math.isnan(lo) else None
    holds = [(t["exit_time"] - t["entry_time"]) / 60_000 for t in trades if t.get("exit_time") and t.get("entry_time")]
    out["avg_hold_min"] = round(sum(holds) / len(holds), 1) if holds else None
    if end > start and trades:
        span = (end - start) / windows
        wins = []
        for i in range(windows):
            w = [t["r"] for t in trades if start + i * span <= t["signal_ts"] < start + (i + 1) * span]
            if w:
                wins.append(sum(w) > 0)
        out["walk_forward_positive"] = round(sum(wins) / len(wins), 3) if wins else None
        out["walk_forward_windows"] = len(wins)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    out["exit_reasons"] = reasons
    out["gates"] = promotion_gates(out)
    return out


def promotion_gates(m: dict) -> dict:
    """Spec §8.4 backtest gate (defaults)."""
    checks = {
        "trades>=300": (m.get("n") or 0) >= 300,
        "profit_factor>=1.3": (m.get("profit_factor") or 0) >= 1.3,
        "expectancy>0.1R": (m.get("expectancy_r") or 0) > 0.1,
        "max_dd<15R": (m.get("max_dd_r") or 0) < 15,
        "walk_forward>=70%": (m.get("walk_forward_positive") or 0) >= 0.7,
    }
    return {"checks": checks, "passed": all(checks.values())}


class Backtester:
    def __init__(self, cfg: dict, market: str = "spot", strategies: dict | None = None,
                 sensitivity: str | None = None, apply_filters: bool = True, collect_features: bool = False):
        self.cfg = cfg
        self.market = market
        self.costs = Costs.from_cfg(cfg.get("fees", {}), market)
        all_strats = strategies or build_strategies(cfg.get("signals", {}).get("strategies"))
        self.bar_strats = {k: s for k, s in all_strats.items()
                           if isinstance(s, BAR_STRATEGIES) and not s.needs}
        self.catchup = next((s for s in all_strats.values() if isinstance(s, BtcCatchup)), None)
        self.sensitivity = sensitivity or cfg.get("trend", {}).get("sensitivity", "balanced")
        self.tfs = cfg.get("timeframes", {}).get("trend", ["1m", "5m", "15m", "1h", "4h"])
        self.apply_filters = apply_filters
        self.collect = collect_features
        sig_cfg = dict(cfg.get("signals", {}))
        self.engine = SignalEngine(sig_cfg, StatsStore(), self.costs)
        self.engine.regimes_from(all_strats)
        self.min_conf = sig_cfg.get("min_confluence", 55)

    def run(self, data: dict[str, list[Bar]], progress=None) -> BacktestResult:
        res = BacktestResult()
        stats: dict[str, TrendStats] = {}
        states = {s: SymbolState(s, self.tfs, self.sensitivity, stats) for s in data}
        coupling = CouplingTracker()
        open_sims: dict[tuple[str, str], tuple[TradeSim, dict | None]] = {}
        timeline = sorted(((b.open_time, 0 if s == LEADER else 1, s, b) for s, bars in data.items() for b in bars),
                          key=lambda x: (x[0], x[1]))
        if not timeline:
            return res
        res.start, res.end = timeline[0][0], timeline[-1][0]
        counter: dict[str, int] = {}
        leader_state = states.get(LEADER)
        total = len(timeline)
        for i, (t, _, sym, bar) in enumerate(timeline):
            if progress and i % 20000 == 0:
                progress(i, total)
            st = states[sym]
            # 1) advance open trades of this symbol
            for key in [k for k in open_sims if k[0] == sym]:
                sim, feat = open_sims[key]
                if sim.on_bar(bar):
                    self._record(res, sim, feat)
                    del open_sims[key]
            # 2) update state with BTC context
            cp = coupling.get(sym)

            def market(tf: str, _sym=sym, _cp=cp):
                if _sym == LEADER or leader_state is None:
                    return {}
                return {"btc_dir": leader_state.trend_dir(tf), "rho": _cp["rho"] if _cp else None}

            closed, events = st.on_1m_close(bar, market)
            coupling.add_bar(sym, t, bar.close)
            counter[sym] = counter.get(sym, 0) + 1
            if sym != LEADER and counter[sym] % 15 == 0:
                coupling.compute(sym)
            for ev in events:
                if ev["state"] == "ENDED":
                    tr = st.trends[ev["tf"]].last_ended
                    if tr:
                        res.trend_records.append(tr.to_json())
            btc_trends = leader_state.trends if leader_state and sym != LEADER else {}
            cands = []
            for tf in closed:
                ctx = Context(sym, tf, t, st.bundles[tf], st.bundles, st.trends, btc_trends,
                              coupling.get(sym), {}, None, events)
                for sid, strat in self.bar_strats.items():
                    if tf not in strat.timeframes:
                        continue
                    try:
                        cands += [(c, ctx) for c in strat.on_bar(ctx)]
                    except Exception:          # a strategy bug must not kill the backtest
                        continue
            if sym == LEADER and self.catchup:
                for ev in events:
                    if ev["state"] == "CONFIRMED" and ev["tf"] in ("1m", "5m"):
                        tc = st.trends[ev["tf"]]
                        ev2 = dict(ev, origin={"ts": tc.trend.origin_ts, "price": tc.trend.origin_price})
                        alts = {s: (states[s].bundles[ev["tf"]], coupling.get(s)) for s in states if s != LEADER}
                        for c in self.catchup.candidates(ev2, st.bundles[ev["tf"]], alts):
                            actx = Context(c.symbol, ev["tf"], t, states[c.symbol].bundles[ev["tf"]], states[c.symbol].bundles,
                                           states[c.symbol].trends, st.trends, coupling.get(c.symbol))
                            self._consider(c, actx, open_sims)
            for c, cctx in cands:
                self._consider(c, cctx, open_sims)
        for sim, feat in open_sims.values():
            self._record(res, sim, feat)
        res.trend_stats = stats
        return res

    def _consider(self, c, ctx: Context, open_sims) -> None:
        key = (c.symbol, strategy_id(c.strategy), c.tf)
        if key in open_sims:
            return
        conf, parts = self.engine.confluence(c, ctx)
        if self.apply_filters:
            if self.engine.vetoes(c, ctx, {}) or conf < self.min_conf:
                return
        feat = None
        if self.collect:
            from ..ml.features import feature_row
            feat = feature_row(c, ctx, conf, parts)
        open_sims[key] = (TradeSim(c, self.costs), feat)

    def _record(self, res: BacktestResult, sim: TradeSim, feat: dict | None) -> None:
        if sim.status != "done" and sim.status != "open":
            return
        if sim.status == "open":
            return                          # unfinished at end of data: excluded
        row = sim.to_json()
        res.trades.append(row)
        if self.collect and feat is not None:
            res.features.append({**feat, "_r": row["r"], "_ts": row["signal_ts"], "_strategy": strategy_id(row["strategy"]),
                                 "_exit": row["exit_reason"]})


def run_pump_backtest(cfg: dict, data_1s: dict[str, list[Bar]], market: str = "spot") -> dict:
    """Replay 1s bars through the Pump & Dump Detector and simulate S11 entries (spec §6.5.5)."""
    det = PumpDetector(cfg.get("pump", {}))
    strat = PumpMomentum()
    costs = Costs.from_cfg(cfg.get("fees", {}), market)
    sims: dict[str, TradeSim] = {}
    trades, events = [], []
    timeline = sorted(((b.open_time, s, b) for s, bars in data_1s.items() for b in bars), key=lambda x: x[0])
    for t, sym, bar in timeline:
        if sym in sims and sims[sym].on_bar(bar, 1000):
            trades.append(sims.pop(sym).to_json())
        for ev in det.on_bar_1s(sym, bar):
            if ev["stage"] in ("IGNITION", "CONFIRMED", "EXHAUSTION", "REVERSAL", "ENDED"):
                events.append({k: ev[k] for k in ("symbol", "stage", "ts")})
            c = strat.from_event(ev, None, market)
            if c and sym not in sims:
                c.time_stop_bars = 30 * 60            # 30 min in 1s bars
                sims[sym] = TradeSim(c, costs)
    rs = [t["r"] for t in trades]
    st = OutcomeStats()
    for r in rs:
        st.add(r)
    return {"events": events, "trades": trades, "metrics": st.summary(),
            "detector_continuation": det.continuation.summary(),
            "closed_events": [e.to_json() for e in det.closed]}
