"""Strategy plug-in interface (spec §6)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..indicators.bundle import IndicatorBundle
from ..models import Candidate, Target


@dataclass
class Context:
    symbol: str
    tf: str
    ts: int
    ind: IndicatorBundle                          # primary timeframe bundle (bar just closed)
    htf: dict[str, IndicatorBundle]               # all timeframe bundles of this symbol
    trend: dict[str, Any] = field(default_factory=dict)       # tf -> TrendCatcher
    btc_trend: dict[str, Any] = field(default_factory=dict)   # tf -> TrendCatcher of BTC
    coupling: dict | None = None
    derivs: dict = field(default_factory=dict)    # funding, oi_change_pct, liq_z, liq_side ...
    book: dict | None = None                      # obi, spread_bps, microprice, mid, intensity_z ...
    trend_events: list[dict] = field(default_factory=list)

    def htf_dir(self, tf: str) -> int:
        b = self.htf.get(tf)
        return b.trend_state() if b else 0

    def higher(self, n: int = 1) -> str | None:
        """The timeframe ``n`` steps above the context timeframe (None if there is none)."""
        tfs = list(self.htf)
        if self.tf not in tfs:
            return None
        i = tfs.index(self.tf) + n
        return tfs[i] if i < len(tfs) else None

    def higher_dir(self, n: int = 1) -> int:
        tf = self.higher(n)
        return self.htf_dir(tf) if tf else 0


class Strategy:
    id: str = "base"
    version: str = "1.0.0"
    timeframes: tuple[str, ...] = ("1m", "5m", "15m")   # bar closes this strategy runs on
    regimes: tuple[str, ...] = ()                 # empty = any
    needs: tuple[str, ...] = ()                   # "book", "derivs", "liquidations" for live-only data
    description: str = ""

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params(), **(params or {})}

    @classmethod
    def default_params(cls) -> dict:
        return {}

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    def on_bar(self, ctx: Context) -> list[Candidate]:
        return []

    def on_tick(self, ctx: Context) -> list[Candidate]:
        return []

    # helpers -----------------------------------------------------------------------------
    def make(self, ctx: Context, side: str, entry: float, stop: float, rr: tuple[float, ...] = (1.0, 2.0),
             reasons: list[str] | None = None, time_stop: int = 15, trailing: bool = False,
             entry_type: str = "market", **features) -> Candidate | None:
        risk = abs(entry - stop)
        if risk <= 0 or (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
            return None
        sign = 1 if side == "LONG" else -1
        share = 100.0 / len(rr)
        targets = [Target(entry + sign * r * risk, share) for r in rr]
        feats = {k: float(v) for k, v in features.items() if isinstance(v, (int, float))}
        return Candidate(self.key, ctx.symbol, ctx.tf, side, entry, stop, targets, ctx.ts, time_stop,
                         entry_type, reasons or [], feats, trailing)
