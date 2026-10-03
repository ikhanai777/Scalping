"""Signal engine (spec §7): candidates -> vetoes -> confluence -> probability -> EV check -> Signal."""
from __future__ import annotations

import math
from typing import Any, Callable

from ..models import Candidate, Signal
from ..sim import Costs
from ..strategies.base import Context
from .store import StatsStore, strategy_id

FAMILY_WEIGHTS = {"trend": 20, "momentum": 10, "flow": 25, "book": 15, "location": 15,
                  "derivatives": 10, "sentiment": 5}


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


class SignalEngine:
    def __init__(self, cfg: dict, stats: StatsStore, costs: Costs, model=None,
                 macro_block: Callable[[int], dict | None] | None = None,
                 coin_veto: Callable[[str, str, int], str | None] | None = None,
                 coin_sentiment: Callable[[str, int], float | None] | None = None,
                 risk_gate: Callable[[], str | None] | None = None,
                 regimes: dict[str, tuple] | None = None):
        self.cfg = cfg
        self.stats = stats
        self.costs = costs
        self.model = model
        self.macro_block = macro_block
        self.coin_veto = coin_veto
        self.coin_sentiment = coin_sentiment
        self.risk_gate = risk_gate
        self.regimes = regimes or {}
        self.rejected: list[dict] = []

    # ------------------------------------------------------------------------------------
    def vetoes(self, c: Candidate, ctx: Context | None, extra: dict) -> list[str]:
        cfg = self.cfg
        out = []
        book = ctx.book if ctx else None
        if book and book.get("spread_bps", 0) > cfg.get("max_spread_bps", 8.0):
            out.append(f"spread {book['spread_bps']:.1f}bps")
        age = extra.get("data_age_s")
        if age is not None and age > cfg.get("stale_data_seconds", 5):
            out.append(f"stale data {age:.0f}s")
        if self.macro_block:
            ev = self.macro_block(c.ts)
            if ev:
                out.append(f"macro event: {ev.get('title')}")
        if self.coin_veto:
            reason = self.coin_veto(c.symbol, c.side, c.ts)
            if reason:
                out.append(reason)
        if self.risk_gate:
            r = self.risk_gate()
            if r:
                out.append(r)
        first = c.targets[0].price if c.targets else c.entry
        cost_px = self.costs.round_trip() * c.entry
        if abs(first - c.entry) < cfg.get("min_target_cost_multiple", 3.0) * cost_px:
            out.append("target < 3x round-trip cost")
        sid = strategy_id(c.strategy)
        if sid in self.stats.shadow:
            out.append(f"strategy degraded: {self.stats.shadow[sid]}")
        allowed = self.regimes.get(sid)
        if allowed and ctx is not None:
            reg = ctx.ind.regime()
            if reg not in allowed and reg != "warming_up":
                out.append(f"regime {reg} not in {allowed}")
        if ctx is not None and cfg.get("btc_follower_mode", "penalty") == "veto" and self._btc_opposes(c, ctx):
            out.append("BTC follower against BTC trend")
        return out

    def _btc_opposes(self, c: Candidate, ctx: Context) -> bool:
        cp = ctx.coupling
        if not cp or not str(cp.get("label", "")).startswith("FOLLOWER") or not ctx.btc_trend:
            return False
        d = 1 if c.side == "LONG" else -1
        tc = ctx.btc_trend.get("5m") or ctx.btc_trend.get("1m")
        return bool(tc and tc.state in (2, 3) and tc.direction == -d)

    # ------------------------------------------------------------------------------------
    def confluence(self, c: Candidate, ctx: Context) -> tuple[float, dict[str, float]]:
        d = 1 if c.side == "LONG" else -1
        v = ctx.ind.v
        parts: dict[str, float] = {}
        # trend: trend-catcher alignment across timeframes + EMA states, BTC alignment for followers
        if ctx.trend:
            tdirs = [t.direction if t.state in (1, 2, 3, 4) else 0 for t in ctx.trend.values()]
            align = sum(1 for x in tdirs if x == d) - sum(1 for x in tdirs if x == -d)
            emas = sum(ctx.htf_dir(tf) * d for tf in ctx.htf)
            s = 0.5 + 0.35 * align / max(len(tdirs), 1) + 0.15 * emas / max(len(ctx.htf), 1)
            if self._btc_opposes(c, ctx):
                s -= 0.3
            parts["trend"] = _clip01(s)
        # momentum
        rsi, hist, hist_p = v.get("rsi"), v.get("macd_hist"), v.get("macd_hist_prev")
        if rsi is not None:
            s = 0.5
            s += 0.15 if (hist is not None and hist * d > 0) else -0.1
            s += 0.1 if (hist is not None and hist_p is not None and (hist - hist_p) * d > 0) else 0
            s += 0.15 if (v.get("div_bull_rsi") if d > 0 else v.get("div_bear_rsi")) else 0
            if (d > 0 and rsi > 80) or (d < 0 and rsi < 20):
                s -= 0.2
            parts["momentum"] = _clip01(s)
        # volume & flow
        s = 0.5 + 0.2 * (1 if v.get("delta", 0) * d > 0 else -1) + 0.2 * math.tanh(4 * v.get("cvd_slope", 0) * d)
        s += 0.1 if v.get("rvol", 1) > 1.2 else 0
        s += 0.1 if (v.get("div_bull_cvd") if d > 0 else v.get("div_bear_cvd")) else 0
        parts["flow"] = _clip01(s)
        # order book
        if ctx.book and "obi" in ctx.book:
            parts["book"] = _clip01(0.5 + 0.5 * ctx.book["obi"] * d / 0.5)
        # location
        if v.get("vwap") and v.get("vwap_sigma"):
            dev = (v["close"] - v["vwap"]) / v["vwap_sigma"] * d
            s = 0.7 if -1.5 <= dev <= 1.0 else (0.45 if dev <= 2.0 else 0.2)
            if "VWAP" in " ".join(c.reasons) and dev < -1.5:
                s = 0.8                               # mean-reversion entries at extremes are the point
            parts["location"] = s
        # derivatives
        der = ctx.derivs or {}
        if der.get("funding") is not None or der.get("oi_change_pct") is not None:
            s = 0.5
            f = der.get("funding")
            if f is not None:
                s -= 0.25 * _clip01(f * d / 0.0005)        # crowded in our direction
            oi = der.get("oi_change_pct")
            if oi is not None and oi > 0:
                s += 0.2 * (1 if v.get("ret", 0) * d > 0 else -1)
            parts["derivatives"] = _clip01(s)
        # sentiment
        if self.coin_sentiment:
            snt = self.coin_sentiment(c.symbol, c.ts)
            if snt is not None:
                parts["sentiment"] = _clip01(0.5 + 0.5 * snt * d)
        w = {k: FAMILY_WEIGHTS[k] for k in parts}
        total = sum(w.values()) or 1
        score = 100 * sum(parts[k] * w[k] for k in parts) / total
        return score, {k: 100 * p for k, p in parts.items()}

    # ------------------------------------------------------------------------------------
    def probability(self, c: Candidate, ctx: Context | None, conf: float, parts: dict | None = None) -> tuple[float | None, tuple | None, str, list[str]]:
        sid = strategy_id(c.strategy)
        if self.model is not None and self.model.has(sid) and ctx is not None:
            p, drivers = self.model.predict(sid, c, ctx, conf, parts)
            if p is not None:
                return p, None, "model", drivers
        st = self.stats.combined(c.strategy)
        if st.n >= self.cfg.get("min_samples_for_probability", 30):
            return st.p_win(), st.ci(), "stats", []
        return None, None, "none", []

    def evaluate(self, c: Candidate, ctx: Context | None, extra: dict | None = None) -> Signal | None:
        extra = extra or {}
        reasons = self.vetoes(c, ctx, extra)
        if reasons:
            self._reject(c, reasons)
            return None
        if ctx is not None:
            conf, parts = self.confluence(c, ctx)
        else:
            conf, parts = 60.0, {}
        p, ci, src, drivers = self.probability(c, ctx, conf, parts)
        st = self.stats.combined(c.strategy)
        cost_r = self.costs.round_trip() * c.entry / c.risk if c.risk else 0.0
        if st.n >= 10:
            avg_win, avg_loss = st.avg_win(), st.avg_loss()   # stats R are already net of costs
            cost_adj = 0.0
        else:
            avg_win = sum(c.r_multiple(t.price) * t.size_pct / 100 for t in c.targets) or 1.0
            avg_loss, cost_adj = 1.0, cost_r
        ev = p * avg_win - (1 - p) * avg_loss - cost_adj if p is not None else None
        risks = self._risks(c, ctx)
        validated = src != "none"
        min_conf = self.cfg.get("min_confluence", 55)
        if conf < min_conf:
            self._reject(c, [f"confluence {conf:.0f} < {min_conf}"])
            return None
        if validated:
            if p < self.cfg.get("min_p_win", 0.5) or ev <= 0:
                self._reject(c, [f"p_win {p:.2f}, EV {ev:+.2f}R after costs"])
                return None
        elif not self.cfg.get("show_unvalidated", True):
            self._reject(c, ["strategy has no validated statistics yet"])
            return None
        regime = ctx.ind.regime() if ctx is not None else "n/a"
        return Signal(c, p, ci, ev, conf, parts, regime, risks, validated, src, drivers=drivers)

    def _risks(self, c: Candidate, ctx: Context | None) -> list[str]:
        out = []
        if ctx is None:
            return out
        d = 1 if c.side == "LONG" else -1
        der = ctx.derivs or {}
        if der.get("funding") is not None and der["funding"] * d > 0.0003:
            out.append(f"funding elevated {der['funding'] * 100:+.3f}%")
        if ctx.btc_trend and ctx.symbol != "BTCUSDT":
            tc = ctx.btc_trend.get("15m")
            if tc and tc.state in (2, 3) and tc.direction == -d:
                out.append("BTC 15m trend opposes")
        v = ctx.ind.v
        if v.get("atr_pct", 0.5) > 0.9:
            out.append("volatility in top decile")
        if self._btc_opposes(c, ctx):
            out.append("BTC follower against BTC trend")
        return out

    def _reject(self, c: Candidate, reasons: list[str]) -> None:
        self.rejected = (self.rejected + [{"ts": c.ts, "symbol": c.symbol, "strategy": c.strategy,
                                           "side": c.side, "reasons": reasons}])[-300:]

    def regimes_from(self, strategies: dict[str, Any]) -> None:
        self.regimes = {sid: s.regimes for sid, s in strategies.items() if getattr(s, "regimes", ())}
