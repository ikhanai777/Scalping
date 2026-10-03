"""ML meta-labelling (spec §7.3): LightGBM per strategy, purged walk-forward CV, isotonic calibration,
SHAP-style drivers via LightGBM ``pred_contrib``."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .features import feature_row


def _ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> tuple[float, list[dict]]:
    edges = np.linspace(0, 1, bins + 1)
    ece, table = 0.0, []
    for i in range(bins):
        m = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if m.sum() == 0:
            continue
        conf, acc = float(p[m].mean()), float(y[m].mean())
        ece += m.sum() / len(p) * abs(conf - acc)
        table.append({"bin": f"{edges[i]:.1f}-{edges[i + 1]:.1f}", "n": int(m.sum()),
                      "predicted": round(conf, 3), "observed": round(acc, 3)})
    return float(ece), table


def _auc(p: np.ndarray, y: np.ndarray) -> float | None:
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def train_strategy(rows: list[dict], folds: int = 5, embargo_ms: int = 4 * 3_600_000,
                   min_rows: int = 200) -> dict | None:
    """rows: feature dicts with _r (net R) and _ts. Returns a model bundle with OOS metrics, or None."""
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression

    rows = sorted(rows, key=lambda r: r["_ts"])
    if len(rows) < min_rows:
        return None
    feats = sorted({k for r in rows for k in r if not k.startswith("_")})
    X = np.array([[r.get(f, np.nan) for f in feats] for r in rows], dtype=float)
    y = np.array([1 if r["_r"] > 0 else 0 for r in rows])
    ts = np.array([r["_ts"] for r in rows])
    if y.sum() < 20 or (1 - y).sum() < 20:
        return None
    params = dict(objective="binary", learning_rate=0.03, num_leaves=15, min_data_in_leaf=30,
                  feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, verbose=-1, seed=7)
    n_rounds = 300
    bounds = np.linspace(0, len(rows), folds + 2, dtype=int)     # first block is train-only
    oos_p, oos_y = [], []
    for k in range(1, folds + 1):
        te_lo, te_hi = bounds[k], bounds[k + 1]
        if te_hi <= te_lo:
            continue
        t_start = ts[te_lo]
        tr = np.where(ts < t_start - embargo_ms)[0]                # purge + embargo before the test block
        if len(tr) < 100 or y[tr].sum() < 10:
            continue
        m = lgb.train(params, lgb.Dataset(X[tr], y[tr]), n_rounds)
        oos_p.append(m.predict(X[te_lo:te_hi]))
        oos_y.append(y[te_lo:te_hi])
    if not oos_p:
        return None
    p_raw, y_oos = np.concatenate(oos_p), np.concatenate(oos_y)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99).fit(p_raw, y_oos)
    # Calibration quality must be judged on data the calibrator did not see: split OOS in halves.
    half = len(p_raw) // 2
    iso_a = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99).fit(p_raw[:half], y_oos[:half])
    p_cal_b = iso_a.predict(p_raw[half:])
    yb = y_oos[half:]
    ece, table = _ece(p_cal_b, yb)
    base = float(yb.mean())
    metrics = {"n_rows": len(rows), "n_oos": int(len(p_raw)), "base_rate": round(base, 4),
               "auc_oos": round(_auc(p_raw, y_oos) or float("nan"), 4),
               "brier_cal": round(float(np.mean((p_cal_b - yb) ** 2)), 4),
               "brier_base": round(base * (1 - base), 4), "ece": round(ece, 4), "reliability": table}
    metrics["beats_base_rate"] = metrics["brier_cal"] < metrics["brier_base"]
    metrics["calibrated"] = ece < 0.05
    final = lgb.train(params, lgb.Dataset(X, y), n_rounds)
    return {"booster": final.model_to_string(), "features": feats, "iso_x": iso.X_thresholds_.tolist(),
            "iso_y": iso.y_thresholds_.tolist(), "metrics": metrics}


class MetaModel:
    """Loads trained per-strategy models; ``predict`` returns calibrated P(win) and top drivers."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.models: dict[str, dict] = {}
        self._boosters: dict = {}
        if path and path.exists():
            self.load()

    def load(self) -> None:
        import joblib
        self.models = joblib.load(self.path)
        self._boosters = {}

    def save(self) -> None:
        import joblib
        self.path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.models, self.path)

    def has(self, sid: str) -> bool:
        m = self.models.get(sid)
        # only use models that passed out-of-sample checks
        return bool(m and m["metrics"].get("beats_base_rate"))

    def _booster(self, sid: str):
        if sid not in self._boosters:
            import lightgbm as lgb
            self._boosters[sid] = lgb.Booster(model_str=self.models[sid]["booster"])
        return self._boosters[sid]

    def predict(self, sid: str, c, ctx, conf: float, parts: dict | None = None):
        m = self.models[sid]
        try:
            row = feature_row(c, ctx, conf, parts or {})
            x = np.array([[row.get(f, np.nan) for f in m["features"]]], dtype=float)
            b = self._booster(sid)
            raw = float(b.predict(x)[0])
            p = float(np.interp(raw, m["iso_x"], m["iso_y"]))
            contrib = b.predict(x, pred_contrib=True)[0][:-1]
            top = np.argsort(-np.abs(contrib))[:5]
            drivers = [f"{m['features'][i]} {'+' if contrib[i] > 0 else '−'}{abs(contrib[i]):.2f}" for i in top]
            if not math.isfinite(p):
                return None, []
            return p, drivers
        except Exception:
            return None, []

    def summary(self) -> dict:
        return {sid: m["metrics"] for sid, m in self.models.items()}
