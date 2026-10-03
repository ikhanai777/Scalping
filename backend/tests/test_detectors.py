import math
import random

import numpy as np

from scalper.detectors.coupling import CouplingTracker, lead_lag
from scalper.detectors.pump import PumpDetector
from scalper.detectors.trend import TrendCatcher
from scalper.indicators.bundle import IndicatorBundle
from scalper.models import Bar

from .conftest import make_bars


def _run_trend(bars, **kw):
    ind = IndicatorBundle("5m")
    tc = TrendCatcher("TEST", "5m", **kw)
    events = []
    for b in bars:
        ind.update(b)
        events += tc.update(ind)
    return tc, events


def test_trend_catcher_detects_uptrend_after_range():
    flat = make_bars(300, seed=1, drift=0.0, vol=0.001)
    up = make_bars(150, seed=2, start=flat[-1].close, drift=0.0015, vol=0.001,
                   t0=flat[-1].open_time + 60_000)
    tc, events = _run_trend(flat + up)
    confirmed = [e for e in events if e["state"] == "CONFIRMED" and e["direction"] == "UP"
                 and e["ts"] >= up[0].open_time]
    assert confirmed, "uptrend was not confirmed"
    first = confirmed[0]
    # detected early: within the first third of the up-leg
    assert first["ts"] <= up[50].open_time


def test_trend_catcher_detects_downtrend():
    flat = make_bars(300, seed=3, vol=0.001)
    down = make_bars(150, seed=4, start=flat[-1].close, drift=-0.0015, vol=0.001,
                     t0=flat[-1].open_time + 60_000)
    tc, events = _run_trend(flat + down)
    assert any(e["state"] == "CONFIRMED" and e["direction"] == "DOWN" for e in events)
    snap = tc.snapshot()
    assert snap["direction"] in ("DOWN", None)


def test_trend_history_and_no_lookahead():
    bars = make_bars(500, seed=5, drift=0.0004)
    tc_full, _ = _run_trend(bars)
    tc_part, _ = _run_trend(bars[:300])
    h_full = {row[0]: row[:4] for row in tc_full.history()}
    for row in tc_part.history():
        assert h_full[row[0]] == row[:4]


def _sec_bar(t, price, vol, buy_frac=0.5, n=5):
    return Bar(t, price, price * 1.0002, price * 0.9998, price, vol, vol * price, n, vol * buy_frac, True)


def test_pump_detector_ignition_and_confirmation():
    rng = random.Random(1)
    det = PumpDetector({"min_move_pct": 1.0})
    t, p = 1_700_000_000_000, 10.0
    events = []
    for _ in range(1200):                                   # quiet market
        p *= math.exp(rng.gauss(0, 0.0003))
        events += det.on_bar_1s("PUMPUSDT", _sec_bar(t, p, rng.uniform(5, 15), rng.uniform(0.4, 0.6)))
        t += 1000
    assert not [e for e in events if e["stage"] == "IGNITION"]
    for i in range(120):                                    # pump: +0.15%/s with 10x volume, buyers
        p *= 1.0015
        events += det.on_bar_1s("PUMPUSDT", _sec_bar(t, p, rng.uniform(100, 150), 0.85, 50))
        t += 1000
    stages = [e["stage"] for e in events if e["symbol"] == "PUMPUSDT"]
    assert "IGNITION" in stages and "CONFIRMED" in stages
    ign = next(e for e in events if e["stage"] == "IGNITION")
    assert ign["direction"] == "PUMP"
    ev = ign["event"]
    assert ev["origin"]["price"] < ev["ignition"]["price"] < ev["chase_limit"]


def test_pump_detector_dump():
    rng = random.Random(2)
    det = PumpDetector()
    t, p = 1_700_000_000_000, 5.0
    events = []
    for _ in range(900):
        p *= math.exp(rng.gauss(0, 0.0003))
        events += det.on_bar_1s("X", _sec_bar(t, p, rng.uniform(5, 15)))
        t += 1000
    for _ in range(90):
        p *= 0.998
        events += det.on_bar_1s("X", _sec_bar(t, p, rng.uniform(100, 150), 0.1, 50))
        t += 1000
    assert any(e["stage"] == "IGNITION" and e["direction"] == "DUMP" for e in events)


def test_coupling_follower_and_lag():
    rng = np.random.default_rng(0)
    tr = CouplingTracker()
    btc, alt, ind = 50000.0, 10.0, 1.0
    t0 = 1_700_000_000_000
    for i in range(300):
        r = rng.normal(0, 0.001)
        btc *= math.exp(r)
        alt *= math.exp(1.5 * r + rng.normal(0, 0.0004))
        ind *= math.exp(rng.normal(0, 0.001))
        for s, c in (("BTCUSDT", btc), ("ALTUSDT", alt), ("INDUSDT", ind)):
            tr.add_bar(s, t0 + i * 60_000, c)
    a = tr.compute("ALTUSDT")
    assert a["label"].startswith("FOLLOWER") and 1.3 < a["beta"] < 1.7
    assert tr.compute("INDUSDT")["label"] in ("INDEPENDENT", "DECOUPLING")


def test_lead_lag_detects_lagging_series():
    rng = np.random.default_rng(1)
    y = rng.normal(0, 1, 800)
    x = np.roll(y, 3) * 0.8 + rng.normal(0, 0.5, 800)
    ll = lead_lag(x[10:], y[10:], 10)
    assert ll["best_lag"] == 3 and ll["significant"]
