"""Feature rows for the ML meta-model (spec §7.3). Directional features are multiplied by the trade
direction so the model learns "favourable vs unfavourable" independent of side."""
from __future__ import annotations

import math

from ..models import Candidate
from ..strategies.base import Context

NAN = float("nan")


def _f(x) -> float:
    try:
        return float(x) if x is not None else NAN
    except (TypeError, ValueError):
        return NAN


def feature_row(c: Candidate, ctx: Context, conf: float, parts: dict[str, float]) -> dict[str, float]:
    d = 1 if c.side == "LONG" else -1
    v = ctx.ind.v
    atr = v.get("atr") or NAN
    close = v.get("close") or NAN
    row: dict[str, float] = {
        "rsi_dir": (_f(v.get("rsi")) - 50) * d,
        "rsi2_dir": (_f(v.get("rsi2")) - 50) * d,
        "stoch_dir": (_f(v.get("stoch_k")) - 50) * d,
        "adx": _f(v.get("adx")),
        "di_dir": (_f(v.get("pdi")) - _f(v.get("mdi"))) * d,
        "close_ema21_atr": (close - _f(v.get("ema21"))) / atr * d,
        "close_ema50_atr": (close - _f(v.get("ema50"))) / atr * d,
        "ema21_50_atr": (_f(v.get("ema21")) - _f(v.get("ema50"))) / atr * d,
        "close_ema200_atr": (close - _f(v.get("ema200"))) / atr * d,
        "vwap_dev": ((close - _f(v.get("vwap"))) / v["vwap_sigma"] * d) if v.get("vwap_sigma") else NAN,
        "bb_width": _f(v.get("bb_width")),
        "atr_rel": atr / close,
        "atr_pct": _f(v.get("atr_pct")),
        "er": _f(v.get("er")),
        "lr_t_dir": _f(v.get("lr_t")) * d,
        "lr_r2": _f(v.get("lr_r2")),
        "cvd_slope_dir": _f(v.get("cvd_slope")) * d,
        "rvol": _f(v.get("rvol")),
        "delta_dir": (_f(v.get("delta")) / v["vol_sma"] * d) if v.get("vol_sma") else NAN,
        "macd_hist_atr_dir": _f(v.get("macd_hist")) / atr * d,
        "st_dir": _f(v.get("st_dir")) * d,
        "squeeze": _f(v.get("squeeze")),
        "div_rsi_dir": (_f(v.get("div_bull_rsi")) - _f(v.get("div_bear_rsi"))) * d,
        "div_cvd_dir": (_f(v.get("div_bull_cvd")) - _f(v.get("div_bear_cvd"))) * d,
        "risk_atr": c.risk / atr,
        "target1_r": c.r_multiple(c.targets[0].price) if c.targets else NAN,
        "time_stop": float(c.time_stop_bars),
        "confluence": conf,
    }
    for k in ("trend", "momentum", "flow", "location"):
        row[f"conf_{k}"] = parts.get(k, NAN)
    for tf in ("5m", "15m", "1h"):
        row[f"htf_{tf}_dir"] = float(ctx.htf_dir(tf) * d) if tf in ctx.htf else NAN
    for tf, tc in ctx.trend.items():
        row[f"trend_{tf}_score_dir"] = tc.score * d
        row[f"trend_{tf}_state"] = float(tc.state if tc.direction == d else -tc.state)
    if ctx.btc_trend:
        for tf in ("1m", "5m", "15m"):
            tc = ctx.btc_trend.get(tf)
            row[f"btc_{tf}_dir"] = float(tc.direction * d if tc and tc.state in (1, 2, 3, 4) else 0)
    cp = ctx.coupling or {}
    row["rho"] = _f(cp.get("rho"))
    row["beta"] = _f(cp.get("beta"))
    hour = (c.ts // 3_600_000) % 24
    row["hour_sin"] = math.sin(2 * math.pi * hour / 24)
    row["hour_cos"] = math.cos(2 * math.pi * hour / 24)
    row["weekday"] = float(((c.ts // 86_400_000) + 3) % 7)       # 0 = Monday
    for k, val in c.features.items():
        row[f"cand_{k}"] = _f(val)
    return row
