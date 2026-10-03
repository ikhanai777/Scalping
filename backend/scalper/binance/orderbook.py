"""Local order book from REST snapshot + diff stream with gap detection (Binance documented procedure),
plus order-book and tape metrics used by strategies and detectors (spec §4.1, §5.4)."""
from __future__ import annotations

import math
import statistics
import time
from collections import deque

from ..models import Trade


class LocalOrderBook:
    def __init__(self, symbol: str, futures: bool = False):
        self.symbol = symbol
        self.futures = futures
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update_id = 0
        self.synced = False
        self.buffer: list[dict] = []
        self.prev_u: int | None = None
        self.resyncs = 0
        self.last_event_ms = 0

    def on_diff(self, ev: dict) -> bool:
        """Apply a depth diff. Returns False if a resync (new snapshot) is required."""
        self.last_event_ms = ev.get("E", 0)
        if not self.synced:
            self.buffer.append(ev)
            if len(self.buffer) > 2000:
                self.buffer = self.buffer[-1000:]
            return True
        return self._apply(ev)

    def _apply(self, ev: dict) -> bool:
        U, u = ev["U"], ev["u"]
        if u <= self.last_update_id:
            return True
        if self.prev_u is None:
            if not (U <= self.last_update_id + 1 <= u):
                return self._gap()
        else:
            if self.futures:
                if ev.get("pu") != self.prev_u:
                    return self._gap()
            elif U != self.prev_u + 1:
                return self._gap()
        for p, q in ev["b"]:
            self._set(self.bids, float(p), float(q))
        for p, q in ev["a"]:
            self._set(self.asks, float(p), float(q))
        self.prev_u = u
        self.last_update_id = u
        return True

    def _gap(self) -> bool:
        self.synced = False
        self.prev_u = None
        self.buffer = []
        self.resyncs += 1
        return False

    @staticmethod
    def _set(side: dict, p: float, q: float) -> None:
        if q == 0:
            side.pop(p, None)
        else:
            side[p] = q

    def on_snapshot(self, snap: dict) -> None:
        self.bids = {float(p): float(q) for p, q in snap["bids"]}
        self.asks = {float(p): float(q) for p, q in snap["asks"]}
        self.last_update_id = snap["lastUpdateId"]
        self.prev_u = None
        self.synced = True
        buf, self.buffer = self.buffer, []
        for ev in buf:
            if not self._apply(ev):
                return

    def top(self, n: int = 20) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        bids = sorted(self.bids.items(), key=lambda x: -x[0])[:n]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:n]
        return bids, asks

    def best(self) -> tuple[float, float, float, float] | None:
        if not self.bids or not self.asks:
            return None
        b = max(self.bids)
        a = min(self.asks)
        return b, self.bids[b], a, self.asks[a]


class Tape:
    """Recent aggTrades: intensity, aggressor share, large-trade detection."""

    def __init__(self, keep: int = 2000):
        self.trades: deque[Trade] = deque(maxlen=keep)
        self.sizes: deque[float] = deque(maxlen=5000)
        self.rate_ewma = None
        self.rate_var = None
        self._sec = 0
        self._sec_count = 0
        self.large: deque[Trade] = deque(maxlen=200)
        self.p99 = None
        self._n = 0

    def add(self, t: Trade) -> bool:
        """Returns True if the trade is a large trade (> rolling p99 notional)."""
        self.trades.append(t)
        notional = t.price * t.qty
        self.sizes.append(notional)
        self._n += 1
        if self._n % 200 == 0 and len(self.sizes) > 200:
            self.p99 = statistics.quantiles(self.sizes, n=100)[98]
        sec = t.ts // 1000
        if sec != self._sec:
            if self._sec:
                self._update_rate(self._sec_count)
            self._sec, self._sec_count = sec, 0
        self._sec_count += 1
        if self.p99 and notional > self.p99:
            self.large.append(t)
            return True
        return False

    def _update_rate(self, x: float) -> None:
        a = 0.02
        if self.rate_ewma is None:
            self.rate_ewma, self.rate_var = x, max(x, 1.0)
            return
        d = x - self.rate_ewma
        self.rate_ewma += a * d
        self.rate_var = (1 - a) * (self.rate_var + a * d * d)

    def intensity_z(self) -> float:
        if self.rate_ewma is None:
            return 0.0
        sd = math.sqrt(self.rate_var) or 1.0
        return (self._sec_count - self.rate_ewma) / sd

    def aggr_buy_share(self, seconds: int = 10) -> float:
        if not self.trades:
            return 0.5
        cutoff = self.trades[-1].ts - seconds * 1000
        buy = sell = 0.0
        for t in reversed(self.trades):
            if t.ts < cutoff:
                break
            if t.is_buyer_maker:
                sell += t.qty
            else:
                buy += t.qty
        tot = buy + sell
        return buy / tot if tot else 0.5


class BookMetrics:
    """Derives OBI, microprice, spread, walls and sustained-imbalance timers from a LocalOrderBook."""

    def __init__(self):
        self.obi_sign = 0
        self.obi_since = 0.0
        self.obi_hist: deque[float] = deque(maxlen=600)
        self.walls_seen: dict[float, float] = {}

    def compute(self, book: LocalOrderBook, tape: Tape | None = None, levels: int = 10) -> dict | None:
        best = book.best()
        if not best:
            return None
        bid, bq, ask, aq = best
        mid = (bid + ask) / 2
        bids, asks = book.top(200)
        tb = sum(q for _, q in bids[:levels])
        ta = sum(q for _, q in asks[:levels])
        obi = (tb - ta) / (tb + ta) if tb + ta else 0.0
        band = mid * 0.001                                   # ±10 bps
        nb = sum(p * q for p, q in bids if p >= mid - band)
        na = sum(p * q for p, q in asks if p <= mid + band)
        obi_10bps = (nb - na) / (nb + na) if nb + na else 0.0
        band1 = mid * 0.01
        b1 = sum(p * q for p, q in bids if p >= mid - band1)
        a1 = sum(p * q for p, q in asks if p <= mid + band1)
        pressure = (b1 - a1) / (b1 + a1) if b1 + a1 else 0.0
        self.obi_hist.append(pressure)
        pz = 0.0
        if len(self.obi_hist) > 60:
            m = statistics.fmean(self.obi_hist)
            sd = statistics.pstdev(self.obi_hist) or 1e-9
            pz = (pressure - m) / sd
        now = time.time()
        sign = 1 if obi > 0.3 else (-1 if obi < -0.3 else 0)
        if sign != self.obi_sign:
            self.obi_sign, self.obi_since = sign, now
        sizes = [q for _, q in bids[:50] + asks[:50]]
        med = statistics.median(sizes) if sizes else 0
        walls = []
        for side, lv in (("bid", bids[:100]), ("ask", asks[:100])):
            for p, q in lv:
                if med and q > 8 * med and abs(p - mid) / mid < 0.01:
                    first = self.walls_seen.setdefault(p, now)
                    walls.append({"side": side, "price": p, "qty": q, "age_s": round(now - first, 1)})
        live_prices = {w["price"] for w in walls}
        for p in [p for p in self.walls_seen if p not in live_prices]:
            del self.walls_seen[p]
        out = {"bid": bid, "ask": ask, "bid_qty": bq, "ask_qty": aq, "mid": mid,
               "spread_bps": (ask - bid) / mid * 10_000, "microprice": (ask * bq + bid * aq) / (bq + aq) if bq + aq else mid,
               "obi": obi, "obi_10bps": obi_10bps, "pressure_1pct": pressure, "pressure_z": pz,
               "obi_sustained_s": (now - self.obi_since) if self.obi_sign else 0.0,
               "depth_10bps_notional": nb + na, "walls": walls[:10], "synced": book.synced}
        if tape:
            out["intensity_z"] = tape.intensity_z()
            out["aggr_buy_share"] = tape.aggr_buy_share()
        return out
