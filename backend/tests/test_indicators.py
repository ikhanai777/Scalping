"""Conformance of incremental indicators against TA-Lib (spec §14)."""
import numpy as np
import pytest

from scalper.indicators import core as ind
from scalper.indicators.bundle import IndicatorBundle

talib = pytest.importorskip("talib")


def _arrays(bars):
    return (np.array([b.high for b in bars]), np.array([b.low for b in bars]),
            np.array([b.close for b in bars]), np.array([b.volume for b in bars]))


def _run(make, bars, fn):
    obj = make()
    out = []
    for b in bars:
        r = fn(obj, b)
        out.append(np.nan if r is None else r)
    return np.array(out, dtype=float)


def _close_enough(ours, ref, rtol=1e-9, tail=None):
    mask = ~np.isnan(ref)
    if tail:
        mask[:-tail] = False
    assert mask.sum() > 100
    assert not np.isnan(ours[mask]).any(), "indicator still warming up where TA-Lib has values"
    np.testing.assert_allclose(ours[mask], ref[mask], rtol=rtol, atol=1e-9)


def test_sma(bars):
    h, l, c, v = _arrays(bars)
    _close_enough(_run(lambda: ind.SMA(20), bars, lambda o, b: o.update(b.close)), talib.SMA(c, 20))


def test_ema(bars):
    h, l, c, v = _arrays(bars)
    for n in (9, 21, 200):
        _close_enough(_run(lambda: ind.EMA(n), bars, lambda o, b: o.update(b.close)), talib.EMA(c, n))


def test_rsi(bars):
    h, l, c, v = _arrays(bars)
    for n in (2, 14):
        _close_enough(_run(lambda: ind.RSI(n), bars, lambda o, b: o.update(b.close)), talib.RSI(c, n))


def test_atr(bars):
    h, l, c, v = _arrays(bars)
    _close_enough(_run(lambda: ind.ATR(14), bars, lambda o, b: o.update(b.high, b.low, b.close)),
                  talib.ATR(h, l, c, 14))


def test_adx_and_di(bars):
    h, l, c, v = _arrays(bars)
    def get(i):
        return lambda o, b: (lambda r: None if r is None or r[i] is None else r[i])(o.update(b.high, b.low, b.close))
    _close_enough(_run(lambda: ind.ADX(14), bars, get(0)), talib.PLUS_DI(h, l, c, 14))
    _close_enough(_run(lambda: ind.ADX(14), bars, get(1)), talib.MINUS_DI(h, l, c, 14))
    _close_enough(_run(lambda: ind.ADX(14), bars, get(2)), talib.ADX(h, l, c, 14))


def test_bollinger(bars):
    h, l, c, v = _arrays(bars)
    up, mid, lo = talib.BBANDS(c, 20, 2.0, 2.0, 0)
    _close_enough(_run(lambda: ind.Bollinger(20, 2.0), bars, lambda o, b: (r[1] if (r := o.update(b.close)) else None)), up)
    _close_enough(_run(lambda: ind.Bollinger(20, 2.0), bars, lambda o, b: (r[2] if (r := o.update(b.close)) else None)), lo)


def test_macd_tail(bars):
    h, l, c, v = _arrays(bars)
    macd, sig, hist = talib.MACD(c, 12, 26, 9)
    ours = _run(lambda: ind.MACD(12, 26, 9), bars, lambda o, b: (r[0] if (r := o.update(b.close)) else None))
    _close_enough(ours, macd, tail=2000)
    ours = _run(lambda: ind.MACD(12, 26, 9), bars, lambda o, b: (r[1] if (r := o.update(b.close)) else None))
    _close_enough(ours, sig, tail=2000)


def test_kama_tail(bars):
    h, l, c, v = _arrays(bars)
    _close_enough(_run(lambda: ind.KAMA(10), bars, lambda o, b: o.update(b.close)), talib.KAMA(c, 10), tail=2000)


def test_obv(bars):
    h, l, c, v = _arrays(bars)
    ours = _run(ind.OBV, bars, lambda o, b: o.update(b.close, b.volume))
    ref = talib.OBV(c, v)
    # TA-Lib starts OBV at the first volume; ours starts at 0 — differences are a constant offset.
    np.testing.assert_allclose(ours - ours[0], ref - ref[0], rtol=1e-9, atol=1e-6)


def test_linreg_slope(bars):
    h, l, c, v = _arrays(bars)
    ours = _run(lambda: ind.LinReg(20), bars, lambda o, b: (r[0] if (r := o.update(b.close)) else None))
    _close_enough(ours, talib.LINEARREG_SLOPE(c, 20), rtol=1e-7)


def test_bundle_no_lookahead(bars):
    """Values at bar t must not depend on bars after t (spec §14)."""
    full = IndicatorBundle("1m")
    snaps = [dict(full.update(b)) for b in bars[:400]]
    for cut in (120, 250, 399):
        part = IndicatorBundle("1m")
        for b in bars[:cut + 1]:
            last = part.update(b)
        for key, val in last.items():
            ref = snaps[cut][key]
            if isinstance(val, float) and isinstance(ref, float):
                assert val == pytest.approx(ref, rel=1e-12, abs=1e-12), key
            else:
                assert val == ref, key
