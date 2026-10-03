"""Trend Catcher validation (spec §6.3.6): recall / precision against labelled large moves, share of the
move still ahead at detection and confirmation, and false starts per day — per timeframe and sensitivity."""
from __future__ import annotations

import statistics

from ..bars import Aggregator
from ..detectors.trend import TrendStats
from ..indicators.core import ATR
from ..models import Bar
from ..symbol_state import SymbolState


def zigzag(bars: list[Bar], thr_pct: float) -> list[dict]:
    """Legs whose size is at least thr_pct (high/low based). Each leg: dir, start, end (ts, price)."""
    if not bars:
        return []
    legs = []
    d = 0
    piv_t, piv_p = bars[0].open_time, bars[0].close
    ext_t, ext_p = piv_t, piv_p
    for b in bars:
        if d >= 0:
            if b.high > ext_p or d == 0 and b.high > piv_p:
                ext_t, ext_p = b.open_time, b.high
            if d == 1 and b.low <= ext_p * (1 - thr_pct / 100):
                legs.append({"dir": 1, "start": (piv_t, piv_p), "end": (ext_t, ext_p)})
                d, piv_t, piv_p, ext_t, ext_p = -1, ext_t, ext_p, b.open_time, b.low
                continue
        if d <= 0:
            if b.low < ext_p or d == 0 and b.low < piv_p:
                ext_t, ext_p = b.open_time, b.low
            if d == -1 and b.high >= ext_p * (1 + thr_pct / 100):
                legs.append({"dir": -1, "start": (piv_t, piv_p), "end": (ext_t, ext_p)})
                d, piv_t, piv_p, ext_t, ext_p = 1, ext_t, ext_p, b.open_time, b.high
                continue
        if d == 0:
            if b.high >= piv_p * (1 + thr_pct / 100):
                d, ext_t, ext_p = 1, b.open_time, b.high
                piv_p = min(x.low for x in bars if x.open_time <= b.open_time)
            elif b.low <= piv_p * (1 - thr_pct / 100):
                d, ext_t, ext_p = -1, b.open_time, b.low
                piv_p = max(x.high for x in bars if x.open_time <= b.open_time)
    return [lg for lg in legs if abs(lg["end"][1] / lg["start"][1] - 1) * 100 >= thr_pct]


def large_move_threshold(bars_1m: list[Bar], k: float = 3.0, min_pct: float = 2.0) -> float:
    agg, atr, vals = Aggregator("1h"), ATR(14), []
    for b in bars_1m:
        for hb in agg.add(b):
            a = atr.update(hb.high, hb.low, hb.close)
            if a:
                vals.append(100 * a / hb.close)
    med = statistics.median(vals) if vals else 0.0
    return max(min_pct, k * med)


def run_trends(bars_1m: list[Bar], symbol: str, tfs: list[str], sensitivity: str) -> list[dict]:
    st = SymbolState(symbol, tfs, sensitivity, {})
    records = []
    for b in bars_1m:
        _, events = st.on_1m_close(b)
        for ev in events:
            if ev["state"] == "ENDED" and st.trends[ev["tf"]].last_ended:
                records.append(st.trends[ev["tf"]].last_ended.to_json())
    return records


def evaluate(bars_1m: list[Bar], records: list[dict], thr_pct: float, tfs: list[str]) -> dict:
    legs = zigzag(bars_1m, thr_pct)
    days = max((bars_1m[-1].open_time - bars_1m[0].open_time) / 86_400_000, 1e-9) if bars_1m else 1
    span = (bars_1m[-1].open_time - bars_1m[0].open_time) if bars_1m else 1
    covered = sum(lg["end"][0] - lg["start"][0] for lg in legs)
    # A random-direction entry at a random time lands inside a same-direction large move with this probability.
    baseline = round(covered / span / 2, 3) if span else None
    out = {"large_move_threshold_pct": round(thr_pct, 3), "large_moves": len(legs), "days": round(days, 2),
           "baseline_precision": baseline, "by_tf": {}}
    for tf in tfs:
        recs = [r for r in records if r["tf"] == tf]
        conf = [r for r in recs if r["confirmed"]]
        tp, rem_conf = 0, []
        for r in conf:
            d = 1 if r["direction"] == "UP" else -1
            tc, pc = r["confirmed"]["ts"], r["confirmed"]["price"]
            if any(lg["dir"] == d and lg["start"][0] <= tc <= lg["end"][0] and (lg["end"][1] - pc) * d > 0 for lg in legs):
                tp += 1
        detected = 0
        for lg in legs:
            d = lg["dir"]
            hits = sorted((r for r in conf if (1 if r["direction"] == "UP" else -1) == d
                           and lg["start"][0] <= r["confirmed"]["ts"] <= lg["end"][0]), key=lambda r: r["confirmed"]["ts"])
            if hits:
                detected += 1
                pc = hits[0]["confirmed"]["price"]
                span = lg["end"][1] - lg["start"][1]
                rem_conf.append(max(0.0, (lg["end"][1] - pc) / span) if span else 0.0)
        false_starts = sum(1 for r in recs if not r["confirmed"])
        out["by_tf"][tf] = {
            "detected": len(recs), "confirmed": len(conf),
            "precision": round(tp / len(conf), 3) if conf else None,
            "recall": round(detected / len(legs), 3) if legs else None,
            "median_move_remaining_at_confirm": round(statistics.median(rem_conf), 3) if rem_conf else None,
            "false_starts_per_day": round(false_starts / days, 2),
        }
    return out
