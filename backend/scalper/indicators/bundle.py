"""IndicatorBundle: all indicators for one symbol/timeframe, updated once per closed bar.

The same bundle class is used live, in backtests and when re-computing chart overlays,
so there is exactly one implementation of every indicator.
"""
from __future__ import annotations

import math
from collections import deque

from ..models import Bar
from .core import (ADX, ATR, CUSUM, EMA, KAMA, MACD, OBV, RSI, SMA, Bollinger, Donchian, Keltner,
                   LinReg, Percentile, SessionSum, SessionVWAP, StdDev, StochRSI, Supertrend, Swings)

WARMUP_BARS = 60


class IndicatorBundle:
    def __init__(self, tf: str, keep_bars: int = 1000):
        self.tf = tf
        self.bars: deque[Bar] = deque(maxlen=keep_bars)
        self.n = 0
        self.emas = {p: EMA(p) for p in (8, 9, 13, 21, 34, 50, 55, 200)}
        self.rsi, self.rsi2 = RSI(14), RSI(2)
        self.stoch = StochRSI(14, 14, 3, 3)
        self.macd = MACD(12, 26, 9)
        self.atr = ATR(14)
        self.atr_pct = Percentile(200)
        self.adx = ADX(14)
        self.bb = Bollinger(20, 2.0)
        self.kc = Keltner(20, 1.5)
        self.st = Supertrend(10, 3.0)
        self.don20, self.don55 = Donchian(20), Donchian(55)
        self.kama = KAMA(10, 2, 30)
        self.lr = LinReg(20)
        self.cusum = CUSUM(0.5, 5.0, 50)
        self.vwap = SessionVWAP()
        self.cvd = SessionSum()
        self.cvd_lr = LinReg(10)
        self.obv = OBV()
        self.vol_sma = SMA(20)
        self.ret_sd = StdDev(30)
        self.swings = Swings(3, 20)
        self.squeeze_bars = 0
        self._recent: deque[tuple[int, float | None, float]] = deque(maxlen=60)  # (idx, rsi, cvd)
        self.v: dict[str, float] = {}

    @property
    def ready(self) -> bool:
        return self.n >= WARMUP_BARS and self.v.get("atr") is not None

    @property
    def last(self) -> Bar | None:
        return self.bars[-1] if self.bars else None

    def update(self, bar: Bar) -> dict[str, float]:
        prev_close = self.bars[-1].close if self.bars else None
        self.bars.append(bar)
        self.n += 1
        o, h, l, c, vol = bar.open, bar.high, bar.low, bar.close, bar.volume
        v = self.v
        prev = dict(v)
        v.clear()
        v.update(open=o, high=h, low=l, close=c, volume=vol, delta=bar.delta, t=bar.open_time)
        for p, e in self.emas.items():
            v[f"ema{p}"] = e.update(c)
        v["ema21_slope"] = (v["ema21"] - prev["ema21"]) if (v["ema21"] and prev.get("ema21")) else 0.0
        v["rsi"] = self.rsi.update(c)
        v["rsi2"] = self.rsi2.update(c)
        v["rsi2_prev"] = prev.get("rsi2")
        sk = self.stoch.update(c)
        v["stoch_k"], v["stoch_d"] = (sk if sk else (None, None))
        v["stoch_k_prev"] = prev.get("stoch_k")
        m = self.macd.update(c)
        v["macd"], v["macd_signal"], v["macd_hist"] = m if m else (None, None, None)
        v["macd_hist_prev"] = prev.get("macd_hist")
        a = self.atr.update(h, l, c)
        v["atr"] = a
        v["atr_pct"] = self.atr_pct.update(a) if a else 0.5
        d = self.adx.update(h, l, c)
        v["pdi"], v["mdi"], v["adx"] = d if d else (None, None, None)
        v["adx_prev"] = prev.get("adx")
        b = self.bb.update(c)
        v["bb_mid"], v["bb_up"], v["bb_lo"] = b if b else (None, None, None)
        v["bb_width"] = self.bb.width
        k = self.kc.update(h, l, c)
        v["kc_mid"], v["kc_up"], v["kc_lo"] = k if k else (None, None, None)
        squeeze = bool(b and k and self.bb.upper < self.kc.upper and self.bb.lower > self.kc.lower)
        v["squeeze_released"] = 1.0 if (not squeeze and self.squeeze_bars >= 6) else 0.0
        v["squeeze_len_before"] = float(self.squeeze_bars)
        self.squeeze_bars = self.squeeze_bars + 1 if squeeze else 0
        v["squeeze"] = 1.0 if squeeze else 0.0
        v["squeeze_bars"] = float(self.squeeze_bars)
        s = self.st.update(h, l, c)
        v["st"], v["st_dir"] = s if s else (None, 0)
        v["st_flip"] = 1.0 if self.st.flipped else 0.0
        dc = self.don20.update(h, l)
        v["don20_hi"], v["don20_lo"] = dc if dc else (None, None)
        dc = self.don55.update(h, l)
        v["don55_hi"], v["don55_lo"] = dc if dc else (None, None)
        kv = self.kama.update(c)
        v["kama"] = kv
        v["er"] = self.kama.er
        v["kama_slope"] = (kv - prev["kama"]) if (kv is not None and prev.get("kama") is not None) else 0.0
        lr = self.lr.update(math.log(c) if c > 0 else 0.0)
        v["lr_slope"], v["lr_t"] = lr if lr else (None, None)
        v["lr_r2"] = self.lr.r2
        v["cusum"] = self.cusum.update(c)
        v["cusum_alarm"] = float(self.cusum.alarm)
        vw, sig = self.vwap.update(bar.open_time, h, l, c, vol)
        v["vwap"], v["vwap_sigma"] = vw, sig
        cvd = self.cvd.update(bar.open_time, bar.delta)
        v["cvd"] = cvd
        cl = self.cvd_lr.update(cvd)
        vs_prev = self.vol_sma.value
        v["cvd_slope"] = (cl[0] / vs_prev) if (cl and vs_prev) else 0.0   # delta per bar vs avg volume
        v["obv"] = self.obv.update(c, vol)
        v["rvol"] = vol / vs_prev if vs_prev else 1.0
        v["vol_sma"] = self.vol_sma.update(vol)
        r = math.log(c / prev_close) if (prev_close and c > 0) else 0.0
        v["ret"] = r
        v["ret_sd"] = self.ret_sd.update(r)
        self.swings.update(bar.open_time, h, l)
        v["swing_new_high"] = 1.0 if self.swings.new_high else 0.0
        v["swing_new_low"] = 1.0 if self.swings.new_low else 0.0
        self._recent.append((self.swings.idx, v["rsi"], cvd))
        self._divergences(v)
        return v

    # --- helpers -------------------------------------------------------------------------
    def _at(self, idx: int) -> tuple[float | None, float] | None:
        for i, r, cv in self._recent:
            if i == idx:
                return r, cv
        return None

    def _divergences(self, v: dict) -> None:
        v["div_bull_rsi"] = v["div_bear_rsi"] = v["div_bull_cvd"] = v["div_bear_cvd"] = 0.0
        sw = self.swings
        if sw.new_low and len(sw.lows) >= 2:
            (_, p1, i1), (_, p2, i2) = sw.lows[-2], sw.lows[-1]
            a, b = self._at(i1), self._at(i2)
            if a and b and p2 < p1:
                if a[0] is not None and b[0] is not None and b[0] > a[0]:
                    v["div_bull_rsi"] = 1.0
                if b[1] > a[1]:
                    v["div_bull_cvd"] = 1.0
        if sw.new_high and len(sw.highs) >= 2:
            (_, p1, i1), (_, p2, i2) = sw.highs[-2], sw.highs[-1]
            a, b = self._at(i1), self._at(i2)
            if a and b and p2 > p1:
                if a[0] is not None and b[0] is not None and b[0] < a[0]:
                    v["div_bear_rsi"] = 1.0
                if b[1] < a[1]:
                    v["div_bear_cvd"] = 1.0

    def last_swing_high(self) -> float | None:
        return self.swings.highs[-1][1] if self.swings.highs else None

    def last_swing_low(self) -> float | None:
        return self.swings.lows[-1][1] if self.swings.lows else None

    def recent_high(self, n: int, skip: int = 0) -> float:
        bars = list(self.bars)[-(n + skip):len(self.bars) - skip if skip else None]
        return max(b.high for b in bars)

    def recent_low(self, n: int, skip: int = 0) -> float:
        bars = list(self.bars)[-(n + skip):len(self.bars) - skip if skip else None]
        return min(b.low for b in bars)

    def trend_state(self) -> int:
        """Simple EMA-based trend direction used as a higher-timeframe filter."""
        v = self.v
        if not self.ready or v.get("ema21") is None or v.get("ema50") is None:
            return 0
        if v["ema21"] > v["ema50"] and v["close"] > v["ema21"]:
            return 1
        if v["ema21"] < v["ema50"] and v["close"] < v["ema21"]:
            return -1
        return 0

    def regime(self) -> str:
        v = self.v
        if not self.ready:
            return "warming_up"
        adx, atrp = v.get("adx") or 0.0, v.get("atr_pct", 0.5)
        if atrp > 0.95 and adx < 25:
            return "high_vol_chaotic"
        if adx >= 25:
            return "trend_up" if (v.get("pdi") or 0) > (v.get("mdi") or 0) else "trend_down"
        if adx < 20:
            return "ranging"
        return "transition"
