"""Built-in strategies S1–S12 (spec §6.1). Long logic is described; short is mirrored."""
from __future__ import annotations

from ..models import Candidate
from .base import Context, Strategy


def _ok(*vals) -> bool:
    return all(v is not None for v in vals)


class TrendPullback(Strategy):
    id = "S1_trend_pullback"
    description = "HTF trend + pullback into EMA9–21/VWAP zone, momentum reset, close with positive delta"
    regimes = ("trend_up", "trend_down", "transition")

    def on_bar(self, ctx: Context) -> list[Candidate]:
        v, ind = ctx.ind.v, ctx.ind
        if not ind.ready or not _ok(v["ema9"], v["ema21"], v["atr"], v["rsi2"], v["vwap"]):
            return []
        h1, h2 = ctx.higher_dir(1), ctx.higher_dir(2)
        out = []
        for d in (1, -1):
            if h1 != d or h2 == -d:
                continue
            zone_hi, zone_lo = max(v["ema9"], v["ema21"]), min(v["ema9"], v["ema21"])
            touched = (v["low"] <= zone_hi + 0.1 * v["atr"]) if d > 0 else (v["high"] >= zone_lo - 0.1 * v["atr"])
            near_vwap = abs(v["close"] - v["vwap"]) < 0.5 * v["atr"]
            if not (touched or near_vwap):
                continue
            on_side_of_vwap = (v["close"] - v["vwap"]) * d > -0.25 * v["atr"]
            r2p = v.get("rsi2_prev")
            reset = r2p is not None and ((r2p < 10) if d > 0 else (r2p > 90))   # pullback exhausted last bar
            stoch_cross = _ok(v["stoch_k"], v["stoch_k_prev"]) and (
                (v["stoch_k_prev"] < 20 <= v["stoch_k"]) if d > 0 else (v["stoch_k_prev"] > 80 >= v["stoch_k"]))
            body_dir = (v["close"] - v["open"]) * d > 0
            if not (on_side_of_vwap and body_dir and v["delta"] * d > 0 and (reset or stoch_cross)):
                continue
            n = 5
            swing = ind.recent_low(n) if d > 0 else ind.recent_high(n)
            stop = swing - d * 0.2 * v["atr"]
            side = "LONG" if d > 0 else "SHORT"
            c = self.make(ctx, side, v["close"], stop, (1.0, 2.0),
                          [f"{ctx.higher(1)}/{ctx.higher(2)} trend {'up' if d > 0 else 'down'}", "pullback to EMA9-21/VWAP",
                           "momentum reset" if reset else "StochRSI cross", "delta confirms"],
                          time_stop=20)
            if c:
                out.append(c)
        return out


class VwapReversion(Strategy):
    id = "S2_vwap_reversion"
    description = "Fade VWAP ±2σ extremes in ranging markets with divergence/absorption"
    regimes = ("ranging", "transition")

    def on_bar(self, ctx: Context) -> list[Candidate]:
        v, ind = ctx.ind.v, ctx.ind
        if not ind.ready or not _ok(v["vwap"], v["vwap_sigma"], v["adx"], v["rsi"], v["atr"]) or v["vwap_sigma"] <= 0:
            return []
        if v["adx"] >= 20:
            return []
        out = []
        dev = (v["close"] - v["vwap"]) / v["vwap_sigma"]
        rng = v["high"] - v["low"]
        absorption = rng > 0 and v["rvol"] > 1.5 and abs(v["close"] - v["open"]) / rng < 0.35
        if dev <= -2 and v["rsi"] < 30 and (v["div_bull_rsi"] or v["div_bull_cvd"] or absorption) and v["close"] > v["open"]:
            stop = min(v["low"], v["vwap"] - 3 * v["vwap_sigma"]) - 0.1 * v["atr"]
            c = self._mk(ctx, "LONG", v, stop, dev, absorption)
            if c:
                out.append(c)
        if dev >= 2 and v["rsi"] > 70 and (v["div_bear_rsi"] or v["div_bear_cvd"] or absorption) and v["close"] < v["open"]:
            stop = max(v["high"], v["vwap"] + 3 * v["vwap_sigma"]) + 0.1 * v["atr"]
            c = self._mk(ctx, "SHORT", v, stop, dev, absorption)
            if c:
                out.append(c)
        return out

    def _mk(self, ctx, side, v, stop, dev, absorption):
        entry = v["close"]
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        rr = abs(v["vwap"] - entry) / risk
        if rr < 1.0:
            return None
        return self.make(ctx, side, entry, stop, (min(1.0, rr / 2), rr),
                         [f"VWAP {dev:+.1f}σ", "ADX < 20 (range)",
                          "absorption" if absorption else "divergence", "target VWAP"], time_stop=30)


class SqueezeBreakout(Strategy):
    id = "S3_squeeze_breakout"
    description = "Bollinger-inside-Keltner squeeze release with volume, delta and direction"

    def on_bar(self, ctx: Context) -> list[Candidate]:
        v, ind = ctx.ind.v, ctx.ind
        if not ind.ready or not v["squeeze_released"] or not _ok(v["atr"], v["bb_mid"]):
            return []
        if v["rvol"] < 1.8:
            return []
        n = int(max(6, v["squeeze_len_before"]))
        hi, lo = ind.recent_high(n, skip=1), ind.recent_low(n, skip=1)
        out = []
        if v["close"] > hi and v["delta"] > 0:
            stop = max(lo, v["close"] - 2 * v["atr"])
            c = self.make(ctx, "LONG", v["close"], stop, (1.0, 2.5),
                          [f"squeeze {n} bars released up", f"RVOL {v['rvol']:.1f}", "positive delta"], time_stop=25)
            out += [c] if c else []
        elif v["close"] < lo and v["delta"] < 0:
            stop = min(hi, v["close"] + 2 * v["atr"])
            c = self.make(ctx, "SHORT", v["close"], stop, (1.0, 2.5),
                          [f"squeeze {n} bars released down", f"RVOL {v['rvol']:.1f}", "negative delta"], time_stop=25)
            out += [c] if c else []
        return out


class LiquiditySweep(Strategy):
    id = "S4_liquidity_sweep"
    description = "Sweep of prior swing/session low (high) then reclaim with CVD divergence or absorption"

    def on_bar(self, ctx: Context) -> list[Candidate]:
        v, ind = ctx.ind.v, ctx.ind
        if not ind.ready or v["atr"] is None or len(ind.swings.lows) < 1:
            return []
        out = []
        lows = [p for (_, p, _) in list(ind.swings.lows)[-4:]]
        highs = [p for (_, p, _) in list(ind.swings.highs)[-4:]]
        rng = v["high"] - v["low"]
        if rng <= 0:
            return []
        lower_wick = (min(v["open"], v["close"]) - v["low"]) / rng
        upper_wick = (v["high"] - max(v["open"], v["close"])) / rng
        flow_bull = v["delta"] > 0 or v["div_bull_cvd"]
        flow_bear = v["delta"] < 0 or v["div_bear_cvd"]
        for lvl in lows:
            swept = v["low"] < lvl - 0.05 * v["atr"] and v["close"] > lvl
            if swept and lower_wick > 0.5 and flow_bull and v["low"] > lvl - 1.5 * v["atr"]:
                stop = v["low"] - 0.15 * v["atr"]
                c = self.make(ctx, "LONG", v["close"], stop, (1.0, 2.0),
                              [f"swept swing low {lvl:.6g}", "reclaimed", "long lower wick",
                               "CVD divergence" if v["div_bull_cvd"] else "buyers absorbed"], time_stop=20)
                out += [c] if c else []
                break
        for lvl in highs:
            swept = v["high"] > lvl + 0.05 * v["atr"] and v["close"] < lvl
            if swept and upper_wick > 0.5 and flow_bear and v["high"] < lvl + 1.5 * v["atr"]:
                stop = v["high"] + 0.15 * v["atr"]
                c = self.make(ctx, "SHORT", v["close"], stop, (1.0, 2.0),
                              [f"swept swing high {lvl:.6g}", "rejected", "long upper wick",
                               "CVD divergence" if v["div_bear_cvd"] else "sellers absorbed"], time_stop=20)
                out += [c] if c else []
                break
        return out


class OrderflowMomentum(Strategy):
    id = "S5_orderflow_momentum"
    description = "Tick-level: sustained book imbalance + microprice skew + aggressive burst (live only)"
    needs = ("book",)

    @classmethod
    def default_params(cls):
        return {"obi": 0.3, "intensity_z": 2.0, "aggr_share": 0.65, "max_spread_bps": 3.0}

    def on_tick(self, ctx: Context) -> list[Candidate]:
        b, v = ctx.book, ctx.ind.v
        if not b or not ctx.ind.ready or v.get("atr") is None:
            return []
        p = self.params
        if b.get("spread_bps", 99) > p["max_spread_bps"] or b.get("obi_sustained_s", 0) < 3:
            return []
        out = []
        for d, side in ((1, "LONG"), (-1, "SHORT")):
            share = b.get("aggr_buy_share", 0.5) if d > 0 else 1 - b.get("aggr_buy_share", 0.5)
            if b.get("obi", 0) * d >= p["obi"] and (b["microprice"] - b["mid"]) * d > 0 \
                    and b.get("intensity_z", 0) >= p["intensity_z"] and share >= p["aggr_share"]:
                entry = b["ask"] if d > 0 else b["bid"]
                stop = entry - d * 0.6 * v["atr"]
                c = self.make(ctx, side, entry, stop, (1.0, 1.5),
                              [f"OBI {b['obi']:+.2f} sustained", "microprice skew", f"intensity z {b['intensity_z']:.1f}",
                               f"aggressor share {share:.0%}"], time_stop=5)
                out += [c] if c else []
        return out


class SessionBreakout(Strategy):
    id = "S6_session_breakout"
    timeframes = ("1m",)
    description = "Opening-range breakout of London (07:00 UTC) and New York (13:30 UTC) sessions"

    SESSIONS = {"London": 7 * 60, "New York": 13 * 60 + 30}

    @classmethod
    def default_params(cls):
        return {"range_minutes": 30, "window_minutes": 120, "min_rvol": 1.5}

    def on_bar(self, ctx: Context) -> list[Candidate]:
        v, ind = ctx.ind.v, ctx.ind
        if not ind.ready or v["atr"] is None or ctx.tf != "1m":
            return []
        p = self.params
        minute_of_day = (v["t"] // 60_000) % 1440
        out = []
        for name, start in self.SESSIONS.items():
            rng_end = start + p["range_minutes"]
            if not (rng_end <= minute_of_day < start + p["window_minutes"]):
                continue
            day0 = v["t"] - (v["t"] % 86_400_000)
            t0, t1 = day0 + start * 60_000, day0 + rng_end * 60_000
            rbars = [b for b in ind.bars if t0 <= b.open_time < t1]
            if len(rbars) < p["range_minutes"] * 0.8:
                continue
            hi, lo = max(b.high for b in rbars), min(b.low for b in rbars)
            prev = ind.bars[-2]
            htf = ctx.higher_dir(2)
            if v["close"] > hi >= prev.close and v["rvol"] >= p["min_rvol"] and htf >= 0:
                c = self.make(ctx, "LONG", v["close"], (hi + lo) / 2, (1.0, 2.0),
                              [f"{name} opening range breakout", f"range {lo:.6g}–{hi:.6g}", f"RVOL {v['rvol']:.1f}"],
                              time_stop=45)
                out += [c] if c else []
            elif v["close"] < lo <= prev.close and v["rvol"] >= p["min_rvol"] and htf <= 0:
                c = self.make(ctx, "SHORT", v["close"], (hi + lo) / 2, (1.0, 2.0),
                              [f"{name} opening range breakdown", f"range {lo:.6g}–{hi:.6g}", f"RVOL {v['rvol']:.1f}"],
                              time_stop=45)
                out += [c] if c else []
        return out


class LiquidationCascade(Strategy):
    id = "S7_liquidation_cascade"
    timeframes = ("1m",)
    description = "Fade liquidation bursts at VWAP extremes with absorption (futures data, live only)"
    needs = ("liquidations",)

    def on_bar(self, ctx: Context) -> list[Candidate]:
        v, d = ctx.ind.v, ctx.derivs
        if not ctx.ind.ready or not d or d.get("liq_z", 0) < 3 or not _ok(v["vwap"], v["vwap_sigma"], v["atr"]):
            return []
        rng = v["high"] - v["low"]
        absorption = rng > 0 and abs(v["close"] - v["open"]) / rng < 0.4
        dev = (v["close"] - v["vwap"]) / v["vwap_sigma"] if v["vwap_sigma"] else 0
        out = []
        # long liquidations (forced sells) push price down -> fade long
        if d.get("liq_side") == "SELL" and dev < -1.5 and absorption:
            c = self.make(ctx, "LONG", v["close"], v["low"] - 0.3 * v["atr"], (1.0, 2.0),
                          [f"long-liquidation burst z={d['liq_z']:.1f}", f"VWAP {dev:+.1f}σ", "absorption"], time_stop=20)
            out += [c] if c else []
        if d.get("liq_side") == "BUY" and dev > 1.5 and absorption:
            c = self.make(ctx, "SHORT", v["close"], v["high"] + 0.3 * v["atr"], (1.0, 2.0),
                          [f"short-squeeze burst z={d['liq_z']:.1f}", f"VWAP {dev:+.1f}σ", "absorption"], time_stop=20)
            out += [c] if c else []
        return out


class SupertrendMacd(Strategy):
    id = "S9_supertrend_macd"
    description = "Baseline: Supertrend flip confirmed by MACD histogram (benchmark the others must beat)"

    def on_bar(self, ctx: Context) -> list[Candidate]:
        v = ctx.ind.v
        if not ctx.ind.ready or not v["st_flip"] or not _ok(v["macd_hist"], v["st"]):
            return []
        if v["st_dir"] > 0 and v["macd_hist"] > 0:
            c = self.make(ctx, "LONG", v["close"], v["st"], (1.0, 2.0), ["Supertrend flipped up", "MACD hist > 0"])
            return [c] if c else []
        if v["st_dir"] < 0 and v["macd_hist"] < 0:
            c = self.make(ctx, "SHORT", v["close"], v["st"], (1.0, 2.0), ["Supertrend flipped down", "MACD hist < 0"])
            return [c] if c else []
        return []


class TrendCatcherEntry(Strategy):
    id = "S10_trend_catcher"
    timeframes = ("1m",)                 # runs every 1m close and reacts to trend events of any tf
    description = "Enter on Trend Catcher CONFIRMED with multi-timeframe alignment; exit by trailing stop"

    @classmethod
    def default_params(cls):
        return {"tfs": ["1m", "5m", "15m"], "min_alignment": 2, "trail_atr": 3.0}

    def on_bar(self, ctx: Context) -> list[Candidate]:
        out = []
        for ev in ctx.trend_events:
            if ev["state"] != "CONFIRMED" or ev["tf"] not in self.params["tfs"]:
                continue
            tc = ctx.trend.get(ev["tf"])
            ind = ctx.htf.get(ev["tf"])
            if tc is None or ind is None or not ind.ready:
                continue
            d = 1 if ev["direction"] == "UP" else -1
            align = sum(1 for t in ctx.trend.values() if t.direction == d and t.state in (1, 2, 3))
            if align < self.params["min_alignment"]:
                continue
            v = ind.v
            atr = v["atr"]
            close = ctx.ind.v["close"]
            stop_struct = tc.trail if tc.trail is not None else close - d * 3 * atr
            stop = stop_struct if abs(close - stop_struct) <= 3.5 * atr else close - d * 3 * atr
            side = "LONG" if d > 0 else "SHORT"
            c = self.make(ctx, side, close, stop, (1.5, 4.0),
                          [f"{ev['tf']} trend CONFIRMED {ev['direction']}", f"alignment {align}/{len(ctx.trend)}",
                           *tc.drivers[:2]],
                          time_stop=240, trailing=True, trail_atr=self.params["trail_atr"], atr=atr)
            if c:
                c.tf = ev["tf"]
                out.append(c)
        return out


class PumpMomentum(Strategy):
    """S11 is event-driven: the engine calls ``from_event`` with Pump & Dump Detector events."""
    id = "S11_pump_momentum"
    description = "Ride IGNITION/CONFIRMED pump/dump events with a strict chase limit and half risk"

    @classmethod
    def default_params(cls):
        return {"enter_on": "CONFIRMED", "risk_scale": 0.5}

    def from_event(self, ev: dict, ctx: Context | None, market: str = "spot") -> Candidate | None:
        e = ev.get("event") or {}
        if ev.get("stage") != self.params["enter_on"] or not e:
            return None
        d = 1 if e["direction"] == "PUMP" else -1
        if d < 0 and market != "futures":
            return None                      # spot cannot short: alert only
        price = e["last"]
        if (price - e["chase_limit"]) * d > 0:
            return None                      # too late
        ign_ref = e["ignition"]["price"] if e.get("ignition") else e["origin"]["price"]
        mid = e["origin"]["price"] + (ign_ref - e["origin"]["price"]) / 2
        stop = max(mid, e["vwap"]) if d > 0 else min(mid, e["vwap"])
        if (price - stop) * d <= 0:
            stop = mid
        if (price - stop) * d <= 0:
            return None
        symbol = ev["symbol"]
        from ..models import Candidate as C, Target
        risk = abs(price - stop)
        side = "LONG" if d > 0 else "SHORT"
        return C(self.key, symbol, "1s", side, price, stop,
                 [Target(price + d * 1.0 * risk, 50), Target(price + d * 3.0 * risk, 50)], ev["ts"], 30, "market",
                 [f"{e['direction']} {ev['stage']}", f"move {e['move_pct']:+.1f}%", f"volume ×{e['vol_x']}",
                  f"class: {e['classification']}"],
                 {"risk_scale": self.params["risk_scale"], "move_pct": e["move_pct"], "vol_x": e["vol_x"]}, True)


class BtcCatchup(Strategy):
    """S12 is cross-symbol: the engine calls ``candidates`` when BTC confirms a trend."""
    id = "S12_btc_catchup"
    description = "BTC confirms a move; high-beta followers that have not moved yet catch up"

    @classmethod
    def default_params(cls):
        return {"min_beta": 1.0, "max_done_frac": 0.35, "min_rho": 0.6}

    def candidates(self, btc_event: dict, btc_ind, alts: dict) -> list[Candidate]:
        """alts: symbol -> (IndicatorBundle on the same tf, coupling dict)."""
        d = 1 if btc_event["direction"] == "UP" else -1
        tc_origin = btc_event.get("origin") or {}
        t0 = tc_origin.get("ts")
        if not t0 or not btc_ind.ready:
            return []
        btc_bars = [b for b in btc_ind.bars if b.open_time >= t0]
        if not btc_bars:
            return []
        btc_move = (btc_bars[-1].close - tc_origin["price"]) / tc_origin["price"]
        if btc_move * d <= 0:
            return []
        out = []
        for sym, (ind, cp) in alts.items():
            if not cp or not ind.ready or ind.v.get("atr") is None:
                continue
            if cp["rho"] < self.params["min_rho"] or cp["beta"] < self.params["min_beta"]:
                continue
            bars = [b for b in ind.bars if b.open_time >= t0]
            if not bars:
                continue
            start = bars[0].open
            move = (ind.v["close"] - start) / start
            expected = cp["beta"] * btc_move
            if move * d >= self.params["max_done_frac"] * expected * d:
                continue
            entry = ind.v["close"]
            gap = (expected - move) * start
            stop = entry - d * 1.5 * ind.v["atr"]
            risk = abs(entry - stop)
            if abs(gap) < 1.5 * risk:
                continue
            from ..models import Candidate as C, Target
            lag = cp.get("lag_s")
            out.append(C(self.key, sym, btc_event["tf"], "LONG" if d > 0 else "SHORT", entry, stop,
                         [Target(entry + d * risk, 50), Target(entry + gap, 50)], btc_event["ts"], 15, "market",
                         [f"BTC {btc_event['tf']} trend confirmed {btc_event['direction']} ({btc_move:+.2%})",
                          f"β {cp['beta']:.2f} ρ {cp['rho']:.2f}" + (f" lag≈{lag}s" if lag else ""),
                          f"moved {move:+.2%} of expected {expected:+.2%}"],
                         {"beta": cp["beta"], "rho": cp["rho"], "btc_move": btc_move, "done_frac": move / expected if expected else 0}))
        return out


ALL_STRATEGIES: list[type[Strategy]] = [TrendPullback, VwapReversion, SqueezeBreakout, LiquiditySweep,
                                        OrderflowMomentum, SessionBreakout, LiquidationCascade, SupertrendMacd,
                                        TrendCatcherEntry, PumpMomentum, BtcCatchup]

BAR_STRATEGIES = (TrendPullback, VwapReversion, SqueezeBreakout, LiquiditySweep, SessionBreakout,
                  LiquidationCascade, SupertrendMacd, TrendCatcherEntry)


def build_strategies(cfg_strats: dict | None) -> dict[str, Strategy]:
    cfg_strats = cfg_strats or {}
    out = {}
    for cls in ALL_STRATEGIES:
        sc = cfg_strats.get(cls.id, {})
        if sc.get("enabled", True):
            out[cls.id] = cls(sc.get("params"))
    return out
