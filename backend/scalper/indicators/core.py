"""Incremental indicators.

Every class has ``update(...)`` that consumes one *closed* bar (or value) and returns the
latest output, or ``None`` while warming up. Seeding follows TA-Lib so outputs can be
checked against it (see tests/test_indicators.py).
"""
from __future__ import annotations

import math
from collections import deque


class SMA:
    def __init__(self, n: int):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self.sum = 0.0
        self._count = 0
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if len(self.buf) == self.n:
            self.sum -= self.buf[0]
        self.buf.append(x)
        self.sum += x
        self._count += 1
        if self._count % (self.n * 4) == 0:      # periodic exact resum to stop float drift
            self.sum = math.fsum(self.buf)
        self.value = self.sum / self.n if len(self.buf) == self.n else None
        return self.value


class EMA:
    """EMA seeded with the SMA of the first n values (TA-Lib convention)."""

    def __init__(self, n: int):
        self.n = n
        self.k = 2.0 / (n + 1)
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
                self._seed = []
            return self.value
        self.value += self.k * (x - self.value)
        return self.value


class Wilder:
    """Wilder's smoothing (RMA) seeded with an SMA."""

    def __init__(self, n: int):
        self.n = n
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
                self._seed = []
            return self.value
        self.value = (self.value * (self.n - 1) + x) / self.n
        return self.value


class RSI:
    def __init__(self, n: int = 14):
        self.n = n
        self.prev: float | None = None
        self.gain = Wilder(n)
        self.loss = Wilder(n)
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self.prev is None:
            self.prev = close
            return None
        d = close - self.prev
        self.prev = close
        g = self.gain.update(max(d, 0.0))
        lo = self.loss.update(max(-d, 0.0))
        if g is None or lo is None:
            return None
        total = g + lo
        self.value = 100.0 * g / total if total > 0 else 0.0
        return self.value


def true_range(h: float, l: float, prev_close: float | None) -> float:
    if prev_close is None:
        return h - l
    return max(h - l, abs(h - prev_close), abs(l - prev_close))


class ATR:
    def __init__(self, n: int = 14):
        self.n = n
        self.prev_close: float | None = None
        self.rma = Wilder(n)
        self.value: float | None = None

    def update(self, h: float, l: float, c: float) -> float | None:
        if self.prev_close is None:          # TA-Lib skips the first bar's TR
            self.prev_close = c
            return None
        tr = true_range(h, l, self.prev_close)
        self.prev_close = c
        self.value = self.rma.update(tr)
        return self.value


class StdDev:
    """Rolling population standard deviation."""

    def __init__(self, n: int):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self.value: float | None = None
        self.mean: float | None = None

    def update(self, x: float) -> float | None:
        self.buf.append(x)
        if len(self.buf) < self.n:
            return None
        m = sum(self.buf) / self.n
        self.mean = m
        self.value = math.sqrt(sum((v - m) ** 2 for v in self.buf) / self.n)
        return self.value


class Bollinger:
    def __init__(self, n: int = 20, k: float = 2.0):
        self.k = k
        self.sd = StdDev(n)
        self.mid = self.upper = self.lower = self.width = None

    def update(self, close: float):
        s = self.sd.update(close)
        if s is None:
            return None
        self.mid = self.sd.mean
        self.upper = self.mid + self.k * s
        self.lower = self.mid - self.k * s
        self.width = (self.upper - self.lower) / self.mid if self.mid else 0.0
        return self.mid, self.upper, self.lower


class Keltner:
    def __init__(self, n: int = 20, mult: float = 1.5):
        self.ema = EMA(n)
        self.atr = ATR(n)
        self.mult = mult
        self.mid = self.upper = self.lower = None

    def update(self, h: float, l: float, c: float):
        m = self.ema.update(c)
        a = self.atr.update(h, l, c)
        if m is None or a is None:
            return None
        self.mid, self.upper, self.lower = m, m + self.mult * a, m - self.mult * a
        return self.mid, self.upper, self.lower


class MACD:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.f, self.s, self.sig = EMA(fast), EMA(slow), EMA(signal)
        self.macd = self.signal = self.hist = None

    def update(self, close: float):
        f, s = self.f.update(close), self.s.update(close)
        if f is None or s is None:
            return None
        self.macd = f - s
        self.signal = self.sig.update(self.macd)
        if self.signal is None:
            return None
        self.hist = self.macd - self.signal
        return self.macd, self.signal, self.hist


class StochRSI:
    """TradingView-style Stoch RSI: K = SMA(k) of stoch(RSI), D = SMA(d) of K."""

    def __init__(self, rsi_n: int = 14, stoch_n: int = 14, k: int = 3, d: int = 3):
        self.rsi = RSI(rsi_n)
        self.win: deque[float] = deque(maxlen=stoch_n)
        self.k_sma, self.d_sma = SMA(k), SMA(d)
        self.k = self.d = None

    def update(self, close: float):
        r = self.rsi.update(close)
        if r is None:
            return None
        self.win.append(r)
        if len(self.win) < self.win.maxlen:
            return None
        lo, hi = min(self.win), max(self.win)
        st = 100.0 * (r - lo) / (hi - lo) if hi > lo else 50.0
        self.k = self.k_sma.update(st)
        if self.k is None:
            return None
        self.d = self.d_sma.update(self.k)
        return self.k, self.d


class ADX:
    """ADX / +DI / -DI with TA-Lib seeding."""

    def __init__(self, n: int = 14):
        self.n = n
        self.prev: tuple[float, float, float] | None = None
        self.count = 0
        self.sum_p = self.sum_m = self.sum_tr = 0.0
        self.dx_seed: list[float] = []
        self.plus_di = self.minus_di = self.adx = None
        self.dx: float | None = None

    def update(self, h: float, l: float, c: float):
        if self.prev is None:
            self.prev = (h, l, c)
            return None
        ph, pl, pc = self.prev
        self.prev = (h, l, c)
        up, down = h - ph, pl - l
        pdm = up if (up > down and up > 0) else 0.0
        mdm = down if (down > up and down > 0) else 0.0
        tr = true_range(h, l, pc)
        self.count += 1
        n = self.n
        if self.count < n:                # accumulate the first n-1 values
            self.sum_p += pdm
            self.sum_m += mdm
            self.sum_tr += tr
            return None
        self.sum_p = self.sum_p - self.sum_p / n + pdm
        self.sum_m = self.sum_m - self.sum_m / n + mdm
        self.sum_tr = self.sum_tr - self.sum_tr / n + tr
        if self.sum_tr > 0:
            self.plus_di = 100.0 * self.sum_p / self.sum_tr
            self.minus_di = 100.0 * self.sum_m / self.sum_tr
        else:
            self.plus_di = self.minus_di = 0.0
        s = self.plus_di + self.minus_di
        self.dx = 100.0 * abs(self.plus_di - self.minus_di) / s if s > 0 else 0.0
        if self.adx is None:
            self.dx_seed.append(self.dx)
            if len(self.dx_seed) == n:
                self.adx = sum(self.dx_seed) / n
                self.dx_seed = []
        else:
            self.adx = (self.adx * (n - 1) + self.dx) / n
        return self.plus_di, self.minus_di, self.adx


class Supertrend:
    def __init__(self, n: int = 10, mult: float = 3.0):
        self.atr = ATR(n)
        self.mult = mult
        self.upper = self.lower = None
        self.direction = 0          # +1 up, -1 down
        self.value: float | None = None
        self.prev_close: float | None = None
        self.flipped = False

    def update(self, h: float, l: float, c: float):
        a = self.atr.update(h, l, c)
        pc = self.prev_close
        self.prev_close = c
        self.flipped = False
        if a is None:
            return None
        hl2 = (h + l) / 2
        bu, bl = hl2 + self.mult * a, hl2 - self.mult * a
        if self.upper is None:
            self.upper, self.lower = bu, bl
            self.direction = 1 if c >= hl2 else -1
        else:
            self.upper = bu if (bu < self.upper or (pc is not None and pc > self.upper)) else self.upper
            self.lower = bl if (bl > self.lower or (pc is not None and pc < self.lower)) else self.lower
            if self.direction == 1 and c < self.lower:
                self.direction, self.flipped = -1, True
            elif self.direction == -1 and c > self.upper:
                self.direction, self.flipped = 1, True
        self.value = self.lower if self.direction == 1 else self.upper
        return self.value, self.direction


class Donchian:
    """Highest high / lowest low of the previous n bars (current bar excluded)."""

    def __init__(self, n: int):
        self.highs: deque[float] = deque(maxlen=n)
        self.lows: deque[float] = deque(maxlen=n)
        self.upper = self.lower = None

    def update(self, h: float, l: float):
        if len(self.highs) == self.highs.maxlen:
            self.upper, self.lower = max(self.highs), min(self.lows)
        self.highs.append(h)
        self.lows.append(l)
        return (self.upper, self.lower) if self.upper is not None else None


class KAMA:
    """Kaufman adaptive moving average + efficiency ratio (TA-Lib seeding: first value = prior close)."""

    def __init__(self, n: int = 10, fast: int = 2, slow: int = 30):
        self.n = n
        self.fast_sc = 2.0 / (fast + 1)
        self.slow_sc = 2.0 / (slow + 1)
        self.buf: deque[float] = deque(maxlen=n + 1)
        self.value: float | None = None
        self.er: float | None = None

    def update(self, close: float):
        self.buf.append(close)
        if len(self.buf) < self.n + 1:
            return None
        change = abs(self.buf[-1] - self.buf[0])
        vol = sum(abs(self.buf[i] - self.buf[i - 1]) for i in range(1, len(self.buf)))
        self.er = change / vol if vol > 0 else 0.0
        sc = (self.er * (self.fast_sc - self.slow_sc) + self.slow_sc) ** 2
        if self.value is None:
            self.value = self.buf[-2]
        self.value += sc * (close - self.value)
        return self.value


class LinReg:
    """Rolling OLS of y on t: slope, t-statistic of the slope, and R²."""

    def __init__(self, n: int = 20):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        t = list(range(n))
        self.t_mean = sum(t) / n
        self.sxx = sum((x - self.t_mean) ** 2 for x in t)
        self.slope = self.tstat = self.r2 = None

    def update(self, y: float):
        self.buf.append(y)
        if len(self.buf) < self.n:
            return None
        n = self.n
        ym = sum(self.buf) / n
        sxy = sum((i - self.t_mean) * (v - ym) for i, v in enumerate(self.buf))
        slope = sxy / self.sxx
        intercept = ym - slope * self.t_mean
        sse = sum((v - (intercept + slope * i)) ** 2 for i, v in enumerate(self.buf))
        sst = sum((v - ym) ** 2 for v in self.buf)
        self.slope = slope
        self.r2 = 1 - sse / sst if sst > 0 else 0.0
        se = math.sqrt(sse / (n - 2) / self.sxx) if sse > 0 else 0.0
        self.tstat = slope / se if se > 0 else (math.copysign(50.0, slope) if slope else 0.0)
        return self.slope, self.tstat


class CUSUM:
    """Two-sided CUSUM change detector on volatility-standardised log returns."""

    def __init__(self, k: float = 0.5, h: float = 5.0, vol_n: int = 50):
        self.k, self.h = k, h
        self.var: float | None = None
        self.alpha = 2.0 / (vol_n + 1)
        self.prev: float | None = None
        self.pos = self.neg = 0.0
        self.alarm = 0              # +1 up change, -1 down change, 0 none (this bar)

    def update(self, close: float) -> float:
        self.alarm = 0
        if self.prev is None or self.prev <= 0 or close <= 0:
            self.prev = close
            return 0.0
        r = math.log(close / self.prev)
        self.prev = close
        if self.var is None:
            self.var = r * r if r != 0 else 1e-10
            return 0.0
        sd = math.sqrt(self.var) or 1e-10
        z = max(-6.0, min(6.0, r / sd))
        self.var = (1 - self.alpha) * self.var + self.alpha * r * r
        self.pos = max(0.0, self.pos + z - self.k)
        self.neg = min(0.0, self.neg + z + self.k)
        if self.pos > self.h:
            self.alarm, self.pos, self.neg = 1, 0.0, 0.0
        elif self.neg < -self.h:
            self.alarm, self.pos, self.neg = -1, 0.0, 0.0
        return self.pos + self.neg


class SessionVWAP:
    """VWAP with σ bands, resetting at 00:00 UTC."""

    DAY = 86_400_000

    def __init__(self):
        self.day = None
        self.pv = self.v = self.pv2 = 0.0
        self.value = self.sigma = None

    def update(self, open_time: int, h: float, l: float, c: float, vol: float):
        day = open_time // self.DAY
        if day != self.day:
            self.day = day
            self.pv = self.v = self.pv2 = 0.0
        tp = (h + l + c) / 3
        self.pv += tp * vol
        self.pv2 += tp * tp * vol
        self.v += vol
        if self.v <= 0:
            self.value, self.sigma = tp, 0.0
        else:
            self.value = self.pv / self.v
            self.sigma = math.sqrt(max(self.pv2 / self.v - self.value ** 2, 0.0))
        return self.value, self.sigma


class SessionSum:
    """Cumulative sum resetting at 00:00 UTC (used for session CVD)."""

    DAY = 86_400_000

    def __init__(self):
        self.day = None
        self.value = 0.0

    def update(self, open_time: int, x: float) -> float:
        day = open_time // self.DAY
        if day != self.day:
            self.day, self.value = day, 0.0
        self.value += x
        return self.value


class OBV:
    def __init__(self):
        self.prev: float | None = None
        self.value = 0.0

    def update(self, close: float, vol: float) -> float:
        if self.prev is not None:
            if close > self.prev:
                self.value += vol
            elif close < self.prev:
                self.value -= vol
        self.prev = close
        return self.value


class Percentile:
    """Rank of the newest value inside a rolling window, in [0, 1]."""

    def __init__(self, n: int = 200):
        self.buf: deque[float] = deque(maxlen=n)

    def update(self, x: float) -> float:
        self.buf.append(x)
        if len(self.buf) < 2:
            return 0.5
        below = sum(1 for v in self.buf if v < x)
        return below / (len(self.buf) - 1)


class Swings:
    """Fractal swing points (left = right = k). A swing is confirmed k bars after it forms,
    so nothing here looks ahead."""

    def __init__(self, k: int = 3, keep: int = 20):
        self.k = k
        self.bars: deque[tuple[int, float, float, int]] = deque(maxlen=2 * k + 1)
        self.highs: deque[tuple[int, float, int]] = deque(maxlen=keep)   # (time, price, idx)
        self.lows: deque[tuple[int, float, int]] = deque(maxlen=keep)
        self.idx = -1
        self.new_high = self.new_low = False

    def update(self, t: int, h: float, l: float):
        self.idx += 1
        self.bars.append((t, h, l, self.idx))
        self.new_high = self.new_low = False
        if len(self.bars) < self.bars.maxlen:
            return
        mid = self.bars[self.k]
        others = [b for i, b in enumerate(self.bars) if i != self.k]
        if all(mid[1] > b[1] for b in others):
            self.highs.append((mid[0], mid[1], mid[3]))
            self.new_high = True
        if all(mid[2] < b[2] for b in others):
            self.lows.append((mid[0], mid[2], mid[3]))
            self.new_low = True
