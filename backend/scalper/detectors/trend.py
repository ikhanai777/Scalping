"""Trend Catcher (spec §6.3): early detection and tracking of up/down trends per symbol/timeframe."""
from __future__ import annotations

import math
from array import array
from dataclasses import dataclass, field

from ..indicators.bundle import IndicatorBundle
from ..stats import OutcomeStats

STATES = ["NONE", "EARLY", "CONFIRMED", "MATURE", "EXHAUSTING", "ENDED"]
S = {name: i for i, name in enumerate(STATES)}

SENSITIVITY = {
    #               early score, confirm score, confirm bars, min groups
    "early":        (25.0, 45.0, 1, 2),
    "balanced":     (35.0, 55.0, 2, 2),
    "conservative": (45.0, 65.0, 3, 3),
}

WEIGHTS = {"structure": 1.5, "slope": 1.5, "ribbon": 1.0, "strength": 1.0, "changepoint": 1.0,
           "participation": 1.0, "derivatives": 0.5, "market": 1.0}


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass
class TrendRecord:
    symbol: str
    tf: str
    direction: int
    origin_ts: int
    origin_price: float
    detected_ts: int
    detected_price: float
    detected_atr: float
    id: str = ""
    confirmed_ts: int | None = None
    confirmed_price: float | None = None
    confirmed_atr: float | None = None
    extreme: float = 0.0
    end_ts: int | None = None
    end_price: float | None = None
    end_reason: str | None = None
    history: list[tuple[str, int]] = field(default_factory=list)

    def lead_atr(self) -> float:
        return abs(self.detected_price - self.origin_price) / self.detected_atr if self.detected_atr else 0.0

    def max_move_atr(self) -> float:
        return abs(self.extreme - self.origin_price) / self.detected_atr if self.detected_atr else 0.0

    def to_json(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "tf": self.tf,
            "direction": "UP" if self.direction > 0 else "DOWN",
            "origin": {"ts": self.origin_ts, "price": self.origin_price},
            "detected": {"ts": self.detected_ts, "price": self.detected_price, "lead_atr": round(self.lead_atr(), 2)},
            "confirmed": ({"ts": self.confirmed_ts, "price": self.confirmed_price} if self.confirmed_ts else None),
            "max_move_atr": round(self.max_move_atr(), 2),
            "max_move_pct": round(100 * abs(self.extreme - self.origin_price) / self.origin_price, 3) if self.origin_price else 0,
            "end": ({"ts": self.end_ts, "price": self.end_price, "reason": self.end_reason} if self.end_ts else None),
            "history": self.history,
        }


class TrendStats:
    """Outcome of CONFIRMED trends: did price extend ≥ 2 ATR beyond the confirmation price?"""

    def __init__(self):
        self.continued = OutcomeStats()
        self.false_starts = 0
        self.detected = 0
        self.confirmed = 0
        self.leads: list[float] = []

    def p_continue(self, min_n: int = 20) -> float | None:
        return round(self.continued.p_win(), 3) if self.continued.n >= min_n else None

    def summary(self) -> dict:
        s = self.continued.summary()
        leads = sorted(self.leads)
        return {"detected": self.detected, "confirmed": self.confirmed, "false_starts": self.false_starts,
                "confirmed_reached_2atr": s, "median_lead_atr": leads[len(leads) // 2] if leads else None}


class TrendCatcher:
    def __init__(self, symbol: str, tf: str, sensitivity: str = "balanced", chandelier_atr: float = 3.0,
                 mature_atr: float = 3.0, stats: TrendStats | None = None, keep: int = 1000):
        self.symbol, self.tf = symbol, tf
        self.early_thr, self.confirm_thr, self.confirm_bars, self.min_groups = SENSITIVITY[sensitivity]
        self.k_chand, self.mature_atr = chandelier_atr, mature_atr
        self.stats = stats or TrendStats()
        self.state = S["NONE"]
        self.direction = 0
        self.score = 0.0
        self.evidence: dict[str, float] = {}
        self.trend: TrendRecord | None = None
        self.last_ended: TrendRecord | None = None
        self.trail: float | None = None
        self._confirm_count = 0
        self._early_bars = 0
        self._bos_dir = 0
        self._bos_level_hi: float | None = None
        self._bos_level_lo: float | None = None
        self._bos_bar = -999
        self._don_bar = -999
        self._don_dir = 0
        self._cp_dir = 0
        self._cp_bar = -999
        self._div_bear_bar = -999
        self._div_bull_bar = -999
        self._n = 0
        self._seq = 0
        self.exhaustion: list[str] = []
        self.drivers: list[str] = []
        self.keep = keep
        self.h_t = array("q")
        self.h_dir = array("b")
        self.h_state = array("b")
        self.h_score = array("b")
        self.h_trail = array("d")
        self.events: list[dict] = []          # recent state transitions for markers/alerts
        self.records: list[TrendRecord] = []  # recently finished trends (for chart markers)

    # ------------------------------------------------------------------------------------------
    def update(self, ind: IndicatorBundle, btc_dir: int | None = None, rho: float | None = None,
               oi_change_pct: float | None = None, funding: float | None = None,
               htf_dir: int = 0) -> list[dict]:
        """Process one closed bar. Returns state-transition events produced on this bar."""
        self._n += 1
        v = ind.v
        events: list[dict] = []
        if not ind.ready:
            self._record(v["t"])
            return events
        self._update_events(ind)
        self.score, self.evidence, groups = self._score(ind, btc_dir, rho, oi_change_pct)
        sdir = _sign(self.score)
        atr = v["atr"] or 0.0
        c, t = v["close"], v["t"]
        st = self.state
        trigger = (self._n - self._bos_bar <= 3) or (self._n - self._cp_bar <= 3) or (self._n - self._don_bar <= 1)

        if st == S["ENDED"]:
            self.state, st, self.direction, self.trend, self.trail = S["NONE"], S["NONE"], 0, None, None

        if st == S["NONE"]:
            if (trigger and abs(self.score) >= self.early_thr and groups >= self.min_groups) or \
                    abs(self.score) >= self.confirm_thr:
                self._start(ind, sdir)
                events.append(self._event("EARLY"))
        elif st == S["EARLY"]:
            d = self.direction
            self._early_bars += 1
            tr = self.trend
            if (c - tr.origin_price) * d < 0:
                events.append(self._finish("false_start", t, c, false_start=True))
            elif self.score * d < -self.early_thr or self._early_bars > 20:
                events.append(self._finish("faded", t, c, false_start=True))
            else:
                part_ok = self.evidence.get("participation", 0) * d > 0 or v.get("rvol", 1) >= 1.0
                if self.score * d >= self.confirm_thr and htf_dir != -d and part_ok:
                    self._confirm_count += 1
                else:
                    self._confirm_count = 0
                if self._confirm_count >= self.confirm_bars:
                    self.state = S["CONFIRMED"]
                    tr.confirmed_ts, tr.confirmed_price, tr.confirmed_atr = t, c, atr
                    tr.history.append(("CONFIRMED", t))
                    self.stats.confirmed += 1
                    events.append(self._event("CONFIRMED"))
        if self.state in (S["EARLY"], S["CONFIRMED"], S["MATURE"], S["EXHAUSTING"]) and self.trend:
            d, tr = self.direction, self.trend
            tr.extreme = max(tr.extreme, v["high"]) if d > 0 else min(tr.extreme, v["low"])
            chand = tr.extreme - d * self.k_chand * atr
            if self.trail is None:
                self.trail = chand
            self.trail = max(self.trail, chand) if d > 0 else min(self.trail, chand)
            if self.state != S["EARLY"]:
                if (c - self.trail) * d < 0:
                    events.append(self._finish("trailing_stop", t, c))
                elif (c - tr.origin_price) * d < 0:
                    events.append(self._finish("invalidated", t, c))
                else:
                    self.exhaustion = self._exhaustion_signs(ind, d, oi_change_pct, funding)
                    if self.state == S["CONFIRMED"] and abs(tr.extreme - tr.origin_price) >= self.mature_atr * atr \
                            and (v.get("adx") or 0) > 30:
                        self.state = S["MATURE"]
                        tr.history.append(("MATURE", t))
                        events.append(self._event("MATURE"))
                    if self.state in (S["CONFIRMED"], S["MATURE"]) and len(self.exhaustion) >= 2:
                        self.state = S["EXHAUSTING"]
                        tr.history.append(("EXHAUSTING", t))
                        events.append(self._event("EXHAUSTING"))
        self._record(t)
        self.events = (self.events + events)[-50:]
        return events

    # ------------------------------------------------------------------------------------------
    def _update_events(self, ind: IndicatorBundle) -> None:
        v = ind.v
        c = v["close"]
        hi, lo = ind.last_swing_high(), ind.last_swing_low()
        if hi is not None and c > hi and self._bos_level_hi != hi:
            self._bos_dir, self._bos_bar, self._bos_level_hi = 1, self._n, hi
        if lo is not None and c < lo and self._bos_level_lo != lo:
            self._bos_dir, self._bos_bar, self._bos_level_lo = -1, self._n, lo
        if v.get("don20_hi") is not None:
            if c > v["don20_hi"]:
                self._don_dir, self._don_bar = 1, self._n
            elif c < v["don20_lo"]:
                self._don_dir, self._don_bar = -1, self._n
        if v.get("cusum_alarm"):
            self._cp_dir, self._cp_bar = int(v["cusum_alarm"]), self._n
        if v.get("squeeze_released") and v.get("bb_mid"):
            self._cp_dir, self._cp_bar = (1 if c > v["bb_mid"] else -1), self._n
        if v.get("div_bear_rsi") or v.get("div_bear_cvd"):
            self._div_bear_bar = self._n
        if v.get("div_bull_rsi") or v.get("div_bull_cvd"):
            self._div_bull_bar = self._n

    def _score(self, ind: IndicatorBundle, btc_dir, rho, oi_change_pct):
        v = ind.v
        atr = v["atr"] or 1e-12
        e: dict[str, float] = {}
        don_recent = self._don_dir if self._n - self._don_bar <= 3 else 0
        e["structure"] = _clip(0.7 * self._bos_dir + 0.3 * don_recent)
        er = v.get("er") or 0.0
        lr_t = v.get("lr_t") or 0.0
        e["slope"] = _clip(0.5 * min(1.0, er * 2) * _sign(v.get("kama_slope") or 0) + 0.5 * math.tanh(lr_t / 4))
        emas = [v.get(f"ema{p}") for p in (8, 13, 21, 34, 55)]
        if all(x is not None for x in emas):
            order = 0
            for i in range(5):
                for j in range(i + 1, 5):
                    order += _sign(emas[i] - emas[j])
            spread = (emas[0] - emas[4]) / atr
            e["ribbon"] = _clip(order / 10 * min(1.0, abs(spread) / 2))
        else:
            e["ribbon"] = 0.0
        if v.get("adx") is not None and (v["pdi"] + v["mdi"]) > 0:
            di = (v["pdi"] - v["mdi"]) / (v["pdi"] + v["mdi"])
            rising = 1.0 if (v.get("adx_prev") is not None and v["adx"] >= v["adx_prev"]) else 0.7
            e["strength"] = _clip(0.7 * di * min(1.0, v["adx"] / 25) * rising + 0.3 * (v.get("st_dir") or 0))
        else:
            e["strength"] = 0.0
        e["changepoint"] = self._cp_dir * math.exp(-(self._n - self._cp_bar) / 10.0) if self._cp_dir else 0.0
        e["participation"] = _clip(math.tanh(3 * (v.get("cvd_slope") or 0)) * min(1.0, (v.get("rvol") or 1) / 1.5))
        weights = dict(WEIGHTS)
        if oi_change_pct is not None:
            closes = [b.close for b in list(ind.bars)[-6:]]
            r5 = closes[-1] - closes[0] if len(closes) > 1 else 0.0
            e["derivatives"] = _sign(r5) * math.tanh(oi_change_pct) if oi_change_pct > 0 else 0.0
        else:
            weights.pop("derivatives")
        if btc_dir is not None:
            e["market"] = btc_dir * (rho if rho is not None else 0.5)
        else:
            weights.pop("market")
        total_w = sum(weights.values())
        score = 100.0 * sum(weights[k] * e[k] for k in weights) / total_w
        sdir = _sign(score)
        groups = sum(1 for k in weights if e[k] * sdir > 0.25)
        self.drivers = self._drivers(e, sdir)
        return score, e, groups

    def _drivers(self, e: dict[str, float], sdir: int) -> list[str]:
        names = {"structure": "break of structure", "slope": "adaptive slope", "ribbon": "EMA ribbon fanning",
                 "strength": "ADX/DI", "changepoint": "change-point", "participation": "CVD/volume",
                 "derivatives": "OI build-up", "market": "BTC alignment"}
        ranked = sorted(((v * sdir, k) for k, v in e.items()), reverse=True)
        return [f"{names[k]} {v:+.2f}" for v, k in ranked[:4] if v > 0.2]

    def _exhaustion_signs(self, ind: IndicatorBundle, d: int, oi_change_pct, funding) -> list[str]:
        v = ind.v
        signs = []
        if d > 0 and self._n - self._div_bear_bar <= 10:
            signs.append("bearish divergence")
        if d < 0 and self._n - self._div_bull_bar <= 10:
            signs.append("bullish divergence")
        rng = v["high"] - v["low"]
        if rng > 0 and (v.get("rvol") or 0) > 3:
            wick = (v["high"] - max(v["open"], v["close"])) if d > 0 else (min(v["open"], v["close"]) - v["low"])
            if wick / rng > 0.5:
                signs.append("climax volume with wick")
        if v.get("ema21") and v.get("atr") and (v["close"] - v["ema21"]) * d / v["atr"] > 3:
            signs.append("overextended from EMA21")
        if oi_change_pct is not None and oi_change_pct < -1.0:
            signs.append("OI falling")
        if funding is not None and funding * d > 0.0005:
            signs.append("funding extreme")
        return signs

    def _start(self, ind: IndicatorBundle, d: int) -> None:
        v = ind.v
        n = min(20, len(ind.bars))
        bars = list(ind.bars)[-n:]
        ob = min(bars, key=lambda b: b.low) if d > 0 else max(bars, key=lambda b: b.high)
        origin = ob.low if d > 0 else ob.high
        self._seq += 1
        self.trend = TrendRecord(self.symbol, self.tf, d, ob.open_time, origin, v["t"], v["close"], v["atr"],
                                 id=f"{self.symbol}-{self.tf}-{v['t']}", extreme=v["close"])
        self.trend.history.append(("EARLY", v["t"]))
        self.state, self.direction = S["EARLY"], d
        self._confirm_count = 0
        self._early_bars = 0
        self.trail = None
        self.exhaustion = []
        self.stats.detected += 1

    def _finish(self, reason: str, t: int, price: float, false_start: bool = False) -> dict:
        tr = self.trend
        tr.end_ts, tr.end_price, tr.end_reason = t, price, reason
        tr.history.append(("ENDED", t))
        if false_start:
            self.stats.false_starts += 1
        if tr.confirmed_ts and tr.confirmed_atr:
            fav = (tr.extreme - tr.confirmed_price) * tr.direction / tr.confirmed_atr
            self.stats.continued.add(1.0 if fav >= 2.0 else -1.0)
            self.stats.leads.append(round(tr.lead_atr(), 2))
        ev = self._event("ENDED", reason=reason)
        self.last_ended = tr
        self.records = (self.records + [tr])[-40:]
        self.state = S["ENDED"]
        return ev

    def _event(self, state: str, **extra) -> dict:
        tr = self.trend
        return {"type": "trend", "symbol": self.symbol, "tf": self.tf, "state": state,
                "direction": "UP" if self.direction > 0 else "DOWN", "ts": (tr.history[-1][1] if tr else 0),
                "score": round(self.score, 1), "trend_id": tr.id if tr else None, **extra}

    def _record(self, t: int) -> None:
        self.h_t.append(t)
        self.h_dir.append(self.direction)
        self.h_state.append(self.state)
        self.h_score.append(int(round(_clip(self.score / 100) * 100)))
        self.h_trail.append(self.trail if self.trail is not None else float("nan"))
        if len(self.h_t) > self.keep + 200:
            for arr in (self.h_t, self.h_dir, self.h_state, self.h_score, self.h_trail):
                del arr[:200]

    # ------------------------------------------------------------------------------------------
    def channel(self, ind: IndicatorBundle) -> dict | None:
        """Support/resistance line from the trend origin through the latest higher swing low
        (lower swing high for downtrends), plus a parallel line through the extreme."""
        tr = self.trend
        if tr is None or self.state == S["NONE"] or not ind.bars:
            return None
        d = tr.direction
        pivots = ind.swings.lows if d > 0 else ind.swings.highs
        cand = [(t, p) for (t, p, _) in pivots if t > tr.origin_ts and (p - tr.origin_price) * d > 0]
        if not cand:
            return None
        t2, p2 = cand[-1]
        t1, p1 = tr.origin_ts, tr.origin_price
        if t2 == t1:
            return None
        slope = (p2 - p1) / (t2 - t1)
        t_end = ind.bars[-1].open_time
        bars = [b for b in ind.bars if b.open_time >= t1]
        off = max(((b.high if d > 0 else b.low) - (p1 + slope * (b.open_time - t1))) * d for b in bars)
        return {"base": [[t1, p1], [t_end, p1 + slope * (t_end - t1)]],
                "parallel": [[t1, p1 + d * off], [t_end, p1 + slope * (t_end - t1) + d * off]]}

    def snapshot(self, ind: IndicatorBundle | None = None, btc_label: str | None = None) -> dict:
        tr = self.trend
        out = {"symbol": self.symbol, "tf": self.tf, "state": STATES[self.state],
               "direction": ("UP" if self.direction > 0 else "DOWN") if self.direction else None,
               "score": round(self.score, 1), "evidence": {k: round(v, 2) for k, v in self.evidence.items()},
               "drivers": self.drivers, "exhaustion": self.exhaustion,
               "trail_stop": self.trail, "p_continue_2atr": self.stats.p_continue(),
               "btc_coupling": btc_label}
        if tr:
            out.update(origin={"ts": tr.origin_ts, "price": tr.origin_price},
                       detected={"ts": tr.detected_ts, "price": tr.detected_price, "lead_atr": round(tr.lead_atr(), 2)},
                       confirmed=({"ts": tr.confirmed_ts, "price": tr.confirmed_price} if tr.confirmed_ts else None),
                       trend_id=tr.id)
        if ind is not None:
            out["channel"] = self.channel(ind)
        return out

    def history(self) -> list[list]:
        n = min(len(self.h_t), self.keep)
        return [[self.h_t[i], self.h_dir[i], self.h_state[i], self.h_score[i],
                 None if math.isnan(self.h_trail[i]) else self.h_trail[i]] for i in range(len(self.h_t) - n, len(self.h_t))]
