"""BTC coupling / leader–follower analysis (spec §6.4)."""
from __future__ import annotations

import math
from collections import deque

import numpy as np


def _rank(a: np.ndarray) -> np.ndarray:
    order = a.argsort()
    r = np.empty_like(order, dtype=float)
    r[order] = np.arange(len(a), dtype=float)
    return r


def correlation_stats(x: np.ndarray, y: np.ndarray) -> dict | None:
    """x = coin log returns, y = leader log returns (aligned). Returns rho, spearman, beta, r2."""
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < 30 or x.std() == 0 or y.std() == 0:
        return None
    rho = float(np.corrcoef(x, y)[0, 1])
    sp = float(np.corrcoef(_rank(x), _rank(y))[0, 1])
    beta = float(np.cov(x, y, ddof=0)[0, 1] / y.var())
    return {"rho": rho, "spearman": sp, "beta": beta, "r2": rho * rho, "n": int(len(x))}


def lead_lag(x: np.ndarray, y: np.ndarray, max_lag: int = 10) -> dict | None:
    """Cross-correlation corr(x[t], y[t-lag]) for lag in [-max_lag, max_lag].

    Positive best lag => the coin (x) follows the leader (y) by ``lag`` steps.
    Significance: peak must beat the zero-lag correlation and the ~99% noise band 2.58/sqrt(n).
    """
    n = len(x)
    if n < 60 or x.std() == 0 or y.std() == 0:
        return None
    xs = (x - x.mean()) / x.std()
    ys = (y - y.mean()) / y.std()
    corrs = {}
    for lag in range(-max_lag, max_lag + 1):
        if lag > 0:
            a, b = xs[lag:], ys[:-lag]
        elif lag < 0:
            a, b = xs[:lag], ys[-lag:]
        else:
            a, b = xs, ys
        corrs[lag] = float(np.mean(a * b)) if len(a) > 10 else 0.0
    best = max(corrs, key=lambda k: corrs[k])
    band = 2.58 / math.sqrt(n)
    significant = best > 0 and corrs[best] > band and corrs[best] > corrs[0] + band / 2
    return {"best_lag": best, "best_corr": corrs[best], "zero_corr": corrs[0], "significant": bool(significant),
            "corrs": corrs}


class CouplingTracker:
    """Keeps aligned 1m returns of every symbol and 5s returns (from 1s bars) for lead–lag."""

    def __init__(self, leader: str = "BTCUSDT", window_short: int = 240, window_long: int = 1440,
                 follower_rho: float = 0.7, follower_r2: float = 0.5, partial_rho: float = 0.4,
                 max_lag: int = 10):
        self.leader = leader
        self.ws, self.wl = window_short, window_long
        self.follower_rho, self.follower_r2, self.partial_rho = follower_rho, follower_r2, partial_rho
        self.max_lag = max_lag
        self.closes: dict[str, dict[int, float]] = {}       # symbol -> {open_time: close}
        self.fast: dict[str, deque] = {}                    # symbol -> deque[(t5s, close)]
        self.results: dict[str, dict] = {}
        self.history: dict[str, deque] = {}                 # rho history for decoupling detection

    def add_bar(self, symbol: str, open_time: int, close: float) -> None:
        d = self.closes.setdefault(symbol, {})
        d[open_time] = close
        if len(d) > self.wl + 120:
            for k in sorted(d)[: len(d) - self.wl - 60]:
                del d[k]

    def add_fast(self, symbol: str, t_ms: int, close: float) -> None:
        """Feed 1s closes; sampled to 5s buckets, keeping ~1h."""
        q = self.fast.setdefault(symbol, deque(maxlen=720))
        b = t_ms - t_ms % 5000
        if q and q[-1][0] == b:
            q[-1] = (b, close)
        else:
            q.append((b, close))

    def _aligned_returns(self, symbol: str, window: int):
        a, b = self.closes.get(symbol), self.closes.get(self.leader)
        if not a or not b:
            return None
        ts = sorted(set(a) & set(b))[-(window + 1):]
        if len(ts) < 31:
            return None
        ca = np.array([a[t] for t in ts])
        cb = np.array([b[t] for t in ts])
        return np.diff(np.log(ca)), np.diff(np.log(cb)), ts

    def compute(self, symbol: str) -> dict | None:
        if symbol == self.leader:
            return None
        out: dict = {"symbol": symbol, "leader": self.leader}
        rs = self._aligned_returns(symbol, self.ws)
        if rs is None:
            return None
        x, y, ts = rs
        st = correlation_stats(x, y)
        if st is None:
            return None
        out.update({k: round(v, 4) if isinstance(v, float) else v for k, v in st.items()})
        rl = self._aligned_returns(symbol, self.wl)
        if rl is not None:
            lt = correlation_stats(rl[0], rl[1])
            if lt:
                out["rho_24h"], out["beta_24h"] = round(lt["rho"], 4), round(lt["beta"], 4)
        # Residual (idiosyncratic) move over the session window: coin return minus beta * leader return.
        day = 86_400_000
        today = [i for i, t in enumerate(ts[1:]) if t // day == ts[-1] // day]
        if today:
            out["residual_session_pct"] = round(100 * float(np.sum(x[today]) - st["beta"] * np.sum(y[today])), 3)
        last60 = slice(-60, None)
        resid = x[last60] - st["beta"] * y[last60]
        resid_sd = float(np.std(x - st["beta"] * y)) or 1e-12
        out["residual_1h_pct"] = round(100 * float(np.sum(resid)), 3)
        out["residual_1h_z"] = round(float(np.sum(resid)) / (resid_sd * math.sqrt(len(resid))), 2)
        # Lead-lag on 5s returns.
        fa, fb = self.fast.get(symbol), self.fast.get(self.leader)
        if fa and fb and len(fa) > 70 and len(fb) > 70:
            da, db = dict(fa), dict(fb)
            common = sorted(set(da) & set(db))
            if len(common) > 70:
                xa = np.diff(np.log([da[t] for t in common]))
                xb = np.diff(np.log([db[t] for t in common]))
                ll = lead_lag(xa, xb, self.max_lag)
                if ll:
                    out["lag_s"] = ll["best_lag"] * 5
                    out["lag_corr"] = round(ll["best_corr"], 3)
                    out["lag_significant"] = ll["significant"]
        hist = self.history.setdefault(symbol, deque(maxlen=240))
        hist.append(st["rho"])
        out["label"] = self.classify(out, hist)
        self.results[symbol] = out
        return out

    def classify(self, r: dict, hist: deque) -> str:
        rho = r["rho"]
        decoupling = (len(hist) >= 30 and max(hist) - rho > 0.3) or abs(r.get("residual_1h_z", 0)) > 2
        if decoupling and rho < self.follower_rho:
            label = "DECOUPLING"
        elif rho >= self.follower_rho and r["r2"] >= self.follower_r2:
            label = "FOLLOWER"
        elif rho >= self.partial_rho:
            label = "PARTIAL"
        else:
            label = "INDEPENDENT"
        if label in ("FOLLOWER", "PARTIAL") and r.get("lag_significant") and r.get("lag_s", 0) > 0:
            label += f"+LAGGER"
        return label

    def get(self, symbol: str) -> dict | None:
        return self.results.get(symbol)

    def badge(self, symbol: str) -> str | None:
        if symbol == self.leader:
            return "LEADER"
        r = self.results.get(symbol)
        return r["label"] if r else None
