"""Pump & Dump Detector (spec §6.5): early detection of abnormal moves from 1-second bars.

Tier 1 (whole market) feeds ``on_bar_1s`` for every symbol. Tier 2 (escalated symbols) may
add order-book and derivatives evidence via ``set_book_pressure`` / ``set_derivs``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import statistics

from ..models import Bar
from ..stats import OutcomeStats

STAGES = ["NONE", "WATCH", "IGNITION", "CONFIRMED", "EXHAUSTION", "REVERSAL"]


class Ring:
    """Fixed-size float ring buffer with ordered views (pure Python: no numpy, so it runs on Android)."""

    def __init__(self, n: int):
        self.a = [0.0] * n
        self.n = n
        self.i = 0
        self.count = 0

    def push(self, x: float) -> None:
        self.a[self.i] = x
        self.i = (self.i + 1) % self.n
        self.count = min(self.count + 1, self.n)

    def last(self, k: int) -> list[float]:
        """The newest ``k`` values, oldest first."""
        k = min(k, self.count)
        if k == 0:
            return []
        start = (self.i - k) % self.n
        if start + k <= self.n:
            return self.a[start:start + k]
        return self.a[start:] + self.a[: self.i]

    def get(self, back: int) -> float:
        """Value ``back`` steps ago (0 = newest)."""
        return self.a[(self.i - 1 - back) % self.n]


def robust(values: list[float], floor_frac: float = 0.25, floor_abs: float = 1e-12) -> tuple[float, float]:
    """Median and a robust scale (1.4826·MAD) with a floor so sparse series don't blow up z-scores."""
    if not values:
        return 0.0, 1.0
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values]) * 1.4826
    mean_abs = sum(abs(v) for v in values) / len(values)
    return med, max(mad, floor_frac * mean_abs, floor_abs)


@dataclass
class MoveEvent:
    id: str
    symbol: str
    direction: int
    stage: str
    origin_ts: int
    origin_price: float
    ignition_ts: int = 0
    ignition_price: float = 0.0
    ignition_low: float = 0.0           # bar extreme against the move at ignition (stop reference)
    confirmed_ts: int | None = None
    confirmed_price: float | None = None
    extreme: float = 0.0
    last_price: float = 0.0
    vwap_pv: float = 0.0
    vwap_v: float = 0.0
    vol_x: float = 0.0
    chase_limit: float = 0.0
    classification: str = "unclassified"
    catalyst: str | None = None
    features: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    exhaustion: list = field(default_factory=list)
    outcome: str | None = None

    @property
    def vwap(self) -> float:
        return self.vwap_pv / self.vwap_v if self.vwap_v > 0 else self.last_price

    @property
    def move_pct(self) -> float:
        return 100 * (self.extreme - self.origin_price) / self.origin_price if self.origin_price else 0.0

    @property
    def current_pct(self) -> float:
        return 100 * (self.last_price - self.origin_price) / self.origin_price if self.origin_price else 0.0

    def to_json(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "direction": "PUMP" if self.direction > 0 else "DUMP",
            "stage": self.stage, "origin": {"ts": self.origin_ts, "price": self.origin_price},
            "ignition": {"ts": self.ignition_ts, "price": self.ignition_price} if self.ignition_ts else None,
            "confirmed": {"ts": self.confirmed_ts, "price": self.confirmed_price} if self.confirmed_ts else None,
            "extreme": self.extreme, "last": self.last_price, "move_pct": round(self.move_pct, 3),
            "current_pct": round(self.current_pct, 3), "vwap": self.vwap, "vol_x": round(self.vol_x, 1),
            "chase_limit": self.chase_limit, "classification": self.classification, "catalyst": self.catalyst,
            "features": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.features.items()},
            "exhaustion": self.exhaustion, "history": self.history, "outcome": self.outcome,
        }


class _Sym:
    def __init__(self, n: int):
        self.c, self.h, self.l = Ring(n), Ring(n), Ring(n)
        self.v, self.tb, self.trades = Ring(n), Ring(n), Ring(n)
        self.ret = Ring(n)
        self.vol15, self.vol60, self.tr15, self.imb15 = Ring(n), Ring(n), Ring(n), Ring(n)
        self.last_t = 0
        self.base: dict[str, tuple[float, float]] = {}
        self.base_t = 0
        self.stage = "NONE"
        self.event: MoveEvent | None = None
        self.watch_until = 0
        self.fired: dict[str, int] = {}          # group -> last ts it fired
        self.book_z = 0.0
        self.oi_z = 0.0
        self.liq_z = 0.0
        self.funding: float | None = None
        self.oi_change_pct: float | None = None
        self.features: dict = {}


class PumpDetector:
    def __init__(self, cfg: dict | None = None, baseline_seconds: int = 1800, warmup_seconds: int = 300):
        c = cfg or {}
        self.min_move_pct = c.get("min_move_pct", 1.0)
        self.min_vol_x = c.get("min_vol_x", 3.0)
        self.ign_z = c.get("ignition_velocity_z", 4.0)
        self.group_z = c.get("group_z", 3.0)
        self.confirm_s = c.get("confirm_seconds", 60)
        self.fizzle_s = c.get("fizzle_seconds", 180)
        self.chase_atr = c.get("chase_atr", 4.0)
        self.chase_pct = c.get("chase_move_pct", 6.0)
        self.p90_move = c.get("typical_p90_move_pct", 15.0)
        self.organic_qv = c.get("organic_min_quote_volume", 20e6)
        self.suspicious_qv = c.get("suspicious_max_quote_volume", 5e6)
        self.n = baseline_seconds
        self.warmup = warmup_seconds
        self.syms: dict[str, _Sym] = {}
        self.quote_volume: dict[str, float] = {}
        self.catalyst_lookup = None          # callable(symbol, ts) -> str | None
        self.leader = "BTCUSDT"
        self.events: dict[str, MoveEvent] = {}   # active events by symbol
        self.closed: list[MoveEvent] = []
        self.continuation = OutcomeStats()
        self._seq = 0

    # ------------------------------------------------------------------ tier-2 inputs
    def set_book_pressure(self, symbol: str, z: float) -> None:
        """Signed z-score of book pressure (+ = asks depleting / bids stepping up)."""
        if symbol in self.syms:
            self.syms[symbol].book_z = z

    def set_derivs(self, symbol: str, oi_change_pct: float | None = None, funding: float | None = None,
                   liq_z: float | None = None, oi_z: float | None = None) -> None:
        s = self.syms.get(symbol)
        if not s:
            return
        if oi_change_pct is not None:
            s.oi_change_pct = oi_change_pct
        if funding is not None:
            s.funding = funding
        if liq_z is not None:
            s.liq_z = liq_z
        if oi_z is not None:
            s.oi_z = oi_z

    # ------------------------------------------------------------------ main input
    def on_bar_1s(self, symbol: str, bar: Bar) -> list[dict]:
        s = self.syms.get(symbol)
        if s is None:
            s = self.syms[symbol] = _Sym(self.n)
        t = bar.open_time
        if s.last_t and t <= s.last_t:
            return []
        if s.last_t and t - s.last_t > 1000:                  # fill silent seconds (no trades)
            last_c = s.c.get(0)
            for k in range(min((t - s.last_t) // 1000 - 1, 120)):
                self._push(s, last_c, last_c, last_c, 0.0, 0.0, 0)
        self._push(s, bar.close, bar.high, bar.low, bar.volume, bar.taker_buy_volume, bar.trades)
        s.last_t = t
        if s.c.count < self.warmup:
            return []
        if t - s.base_t >= 10_000 or not s.base:
            self._baseline(s)
            s.base_t = t
        return self._evaluate(symbol, s, t, bar)

    def _push(self, s: _Sym, c, h, l, v, tb, n) -> None:
        prev = s.c.get(0) if s.c.count else c
        s.c.push(c)
        s.h.push(h)
        s.l.push(l)
        s.v.push(v)
        s.tb.push(tb)
        s.trades.push(n)
        s.ret.push(math.log(c / prev) if prev > 0 and c > 0 else 0.0)
        v15 = sum(s.v.last(15))
        tb15 = sum(s.tb.last(15))
        s.vol15.push(v15)
        s.vol60.push(sum(s.v.last(60)))
        s.tr15.push(sum(s.trades.last(15)))
        s.imb15.push((2 * tb15 / v15 - 1) if v15 > 0 else 0.0)

    def _baseline(self, s: _Sym) -> None:
        r = s.ret.last(self.n)
        med, scale = robust(r, floor_frac=0.5, floor_abs=2e-5)
        s.base["sigma"] = (0.0, scale)
        for name in ("vol15", "vol60", "tr15", "imb15"):
            ring = getattr(s, name)
            s.base[name] = robust(ring.last(self.n), floor_frac=0.25, floor_abs=1e-9)

    def _z(self, s: _Sym, name: str, x: float) -> float:
        med, sc = s.base[name]
        return (x - med) / sc

    @staticmethod
    def _vol_x(s: _Sym) -> float:
        """Recent volume vs its typical level (fast 15s window or 60s window, whichever is larger)."""
        return max(s.vol15.get(0) / (s.base["vol15"][0] or 1e-9), s.vol60.get(0) / (s.base["vol60"][0] or 1e-9))

    def _atr1m(self, s: _Sym) -> float:
        h, l = s.h.last(900), s.l.last(900)
        k = len(h) // 60
        if k == 0:
            return 0.0
        rng = [max(h[i * 60:(i + 1) * 60]) - min(l[i * 60:(i + 1) * 60]) for i in range(k)]
        return sum(rng) / len(rng)

    # ------------------------------------------------------------------ evaluation
    def _evaluate(self, symbol: str, s: _Sym, t: int, bar: Bar) -> list[dict]:
        sigma = s.base["sigma"][1]
        c = bar.close
        vel = {}
        for h in (5, 15, 60, 300):
            if s.c.count > h:
                past = s.c.get(h)
                vel[h] = (math.log(c / past) / (sigma * math.sqrt(h))) if past > 0 and c > 0 else 0.0
        hbest = max(vel, key=lambda k: abs(vel[k]))
        vz = vel[hbest]
        d = 1 if vz > 0 else -1
        vol_z = max(self._z(s, "vol15", s.vol15.get(0)), self._z(s, "vol60", s.vol60.get(0)),
                    self._z(s, "tr15", s.tr15.get(0)))
        imb = s.imb15.get(0)
        imb_z = self._z(s, "imb15", imb)
        f = {"vel_z": vz, "vel_h": hbest, "vol_z": vol_z, "imb15": imb, "imb_z": imb_z,
             "book_z": s.book_z, "oi_z": s.oi_z, "liq_z": s.liq_z, "sigma_1s": sigma}
        s.features = f
        gz = self.group_z
        fires = {
            "velocity": abs(vz) >= gz,
            "volume": vol_z >= gz,
            "flow": (imb * d > 0.4 and imb_z * d >= gz * 0.66) or imb * d > 0.6,
            "book": s.book_z * d >= gz,
            "derivatives": max(s.oi_z, s.liq_z) >= gz,
        }
        cat = self.catalyst_lookup(symbol, t) if self.catalyst_lookup else None
        if cat:
            fires["catalyst"] = True
        for g, on in fires.items():
            if on:
                s.fired[g] = t
        recent_groups = [g for g, ts in s.fired.items() if t - ts <= 30_000]
        out: list[dict] = []
        ev = s.event

        if ev is None:
            if abs(vz) >= self.ign_z and len(recent_groups) >= 3 and "velocity" in recent_groups:
                origin_idx_prices = s.c.last(300)
                origin = min(origin_idx_prices) if d > 0 else max(origin_idx_prices)
                move = 100 * (c - origin) / origin * d
                min_move = max(self.min_move_pct, 100 * 3 * sigma * math.sqrt(hbest))
                vol_x = self._vol_x(s)
                if move >= min_move and vol_x >= self.min_vol_x:
                    out.append(self._ignite(symbol, s, t, bar, d, origin, recent_groups, cat))
                    return out
            if s.stage == "NONE" and (len(recent_groups) >= 1 or (vol_z >= gz and abs(vz) < 1.5)):
                s.stage, s.watch_until = "WATCH", t + 300_000
                out.append({"type": "pump", "symbol": symbol, "stage": "WATCH", "ts": t,
                            "direction": "PUMP" if d > 0 else "DUMP",
                            "reason": "volume without price" if abs(vz) < 1.5 else f"{', '.join(recent_groups)} z>{gz}"})
            elif s.stage == "WATCH" and t > s.watch_until:
                s.stage = "NONE"
            return out

        # active event
        d = ev.direction
        ev.last_price = c
        ev.vwap_pv += c * bar.volume
        ev.vwap_v += bar.volume
        ev.extreme = max(ev.extreme, bar.high) if d > 0 else min(ev.extreme, bar.low)
        ev.features = {k: f[k] for k in ("vel_z", "vol_z", "imb15", "book_z", "oi_z", "liq_z")}
        age = (t - ev.ignition_ts) / 1000
        mid = ev.origin_price + (ev.ignition_price - ev.origin_price) / 2
        if ev.stage == "IGNITION":
            if (c - mid) * d < 0:
                out.append(self._close(s, t, "fizzle"))
            elif age >= self.confirm_s:
                last60 = s.imb15.last(60)
                imb60 = sum(last60) / len(last60)
                v60z = self._z(s, "vol60", s.vol60.get(0))
                if imb60 * d > 0.1 and v60z > 2:
                    ev.stage = "CONFIRMED"
                    ev.confirmed_ts, ev.confirmed_price = t, c
                    ev.history.append(("CONFIRMED", t, c))
                    out.append(self._event(ev, t))
                elif age >= self.fizzle_s:
                    out.append(self._close(s, t, "fizzle"))
        elif ev.stage in ("CONFIRMED", "EXHAUSTION"):
            signs = []
            hi60, lo60 = max(s.h.last(60)), min(s.l.last(60))
            rng = hi60 - lo60
            wick = (hi60 - c) if d > 0 else (c - lo60)
            if self._z(s, "vol15", s.vol15.get(0)) > 8 and rng > 0 and wick / rng > 0.5:
                signs.append("climax volume with wick")
            new_extreme = (bar.high >= ev.extreme) if d > 0 else (bar.low <= ev.extreme)
            if new_extreme and imb * d < 0:
                signs.append("flow divergence")
            if abs(ev.move_pct) > self.p90_move:
                signs.append("move beyond typical p90")
            if s.oi_change_pct is not None and s.oi_change_pct < -1 and d > 0:
                signs.append("OI dropping (squeeze ending)")
            if s.funding is not None and s.funding * d > 0.001:
                signs.append("funding spike")
            if s.book_z * d < -self.group_z:
                signs.append("opposite wall reloading")
            ev.exhaustion = signs
            if ev.stage == "CONFIRMED" and len(signs) >= 2:
                ev.stage = "EXHAUSTION"
                ev.history.append(("EXHAUSTION", t, c))
                out.append(self._event(ev, t))
            if (c - ev.vwap) * d < 0 and imb * d < -0.1:
                ev.stage = "REVERSAL"
                ev.history.append(("REVERSAL", t, c))
                out.append(self._event(ev, t))
                out.append(self._close(s, t, "reversal", keep_stage=True))
            elif age > 1800:
                out.append(self._close(s, t, "expired"))
        return out

    def _ignite(self, symbol, s: _Sym, t, bar: Bar, d, origin, groups, cat) -> dict:
        self._seq += 1
        atr = self._atr1m(s)
        chase_a = origin + d * self.chase_atr * atr if atr > 0 else None
        chase_p = origin * (1 + d * self.chase_pct / 100)
        if chase_a is None:
            chase = chase_p
        else:
            chase = min(chase_a, chase_p) if d > 0 else max(chase_a, chase_p)
        ev = MoveEvent(id=f"mv_{symbol}_{t}", symbol=symbol, direction=d, stage="IGNITION",
                       origin_ts=t, origin_price=origin, ignition_ts=t, ignition_price=bar.close,
                       ignition_low=bar.low if d > 0 else bar.high, extreme=bar.high if d > 0 else bar.low,
                       last_price=bar.close, vol_x=self._vol_x(s), chase_limit=chase,
                       catalyst=cat, features=dict(s.features))
        ev.vwap_pv, ev.vwap_v = bar.close * bar.volume, bar.volume
        # find origin time: scan back for the origin price
        closes = s.c.last(300)
        idx = closes.index(min(closes) if d > 0 else max(closes))
        ev.origin_ts = t - (len(closes) - 1 - idx) * 1000
        ev.history.append(("IGNITION", t, bar.close))
        ev.classification = self._classify(symbol, d, cat)
        ev.features["groups"] = ",".join(sorted(groups))
        s.event = ev
        s.stage = "IGNITION"
        self.events[symbol] = ev
        return self._event(ev, t)

    def _classify(self, symbol: str, d: int, cat) -> str:
        qv = self.quote_volume.get(symbol, 0.0)
        leader = self.syms.get(self.leader)
        btc_same = bool(leader and leader.features and leader.features.get("vel_z", 0) * d > 2)
        if (btc_same or cat) and qv >= self.organic_qv:
            return "organic"
        if qv and qv < self.suspicious_qv and not cat:
            return "suspicious"
        if not cat and not btc_same and qv < self.organic_qv:
            return "isolated"
        return "unclassified"

    def _close(self, s: _Sym, t: int, outcome: str, keep_stage: bool = False) -> dict:
        ev = s.event
        ev.outcome = outcome
        if ev.confirmed_price:
            fav = (ev.extreme - ev.confirmed_price) * ev.direction / ev.confirmed_price
            self.continuation.add(1.0 if fav >= 0.01 else -1.0)
        if not keep_stage:
            ev.history.append(("ENDED", t, ev.last_price))
        s.event = None
        s.stage = "NONE"
        s.fired.clear()
        self.events.pop(ev.symbol, None)
        self.closed = (self.closed + [ev])[-200:]
        return {"type": "pump", "symbol": ev.symbol, "stage": "ENDED", "ts": t, "outcome": outcome,
                "event": ev.to_json()}

    def _event(self, ev: MoveEvent, t: int) -> dict:
        return {"type": "pump", "symbol": ev.symbol, "stage": ev.stage, "ts": t,
                "direction": "PUMP" if ev.direction > 0 else "DUMP", "event": ev.to_json()}

    # ------------------------------------------------------------------ views
    def radar(self) -> list[dict]:
        order = {"CONFIRMED": 0, "IGNITION": 1, "EXHAUSTION": 2, "REVERSAL": 3}
        active = sorted(self.events.values(), key=lambda e: (order.get(e.stage, 9), -abs(e.move_pct)))
        watch = [{"symbol": sym, "stage": "WATCH", "features": {k: round(v, 2) for k, v in s.features.items()
                                                                if isinstance(v, float)}}
                 for sym, s in self.syms.items() if s.stage == "WATCH"]
        return [e.to_json() for e in active] + watch[:30]

    def recent(self, symbol: str | None = None) -> list[dict]:
        evs = list(self.closed) + list(self.events.values())
        return [e.to_json() for e in evs if symbol is None or e.symbol == symbol]
