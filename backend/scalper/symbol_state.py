"""Per-symbol state: indicator bundles, bar aggregators and trend catchers for every timeframe.

Used identically by the live engine and the backtester.
"""
from __future__ import annotations

from typing import Callable

from .bars import Aggregator
from .detectors.trend import TrendCatcher, TrendStats
from .indicators.bundle import IndicatorBundle
from .models import TF_MS, Bar

ACTIVE_STATES = (1, 2, 3, 4)   # EARLY, CONFIRMED, MATURE, EXHAUSTING


class SymbolState:
    def __init__(self, symbol: str, tfs: list[str], sensitivity: str = "balanced",
                 trend_stats: dict[str, TrendStats] | None = None, chandelier_atr: float = 3.0,
                 mature_atr: float = 3.0, keep: int = 1000):
        self.symbol = symbol
        self.tfs = list(tfs)
        if "1m" not in self.tfs:
            self.tfs.insert(0, "1m")
        self.bundles = {tf: IndicatorBundle(tf, keep) for tf in self.tfs}
        self.aggs = {tf: Aggregator(tf) for tf in self.tfs if tf != "1m"}
        stats = trend_stats if trend_stats is not None else {}
        self.trends = {tf: TrendCatcher(symbol, tf, sensitivity, chandelier_atr, mature_atr,
                                        stats.setdefault(tf, TrendStats()), keep) for tf in self.tfs}
        self.partial_1m: Bar | None = None
        self.last_update_ms = 0
        self.quote_volume_24h = 0.0
        self.price_change_24h = 0.0

    def higher_tf(self, tf: str) -> str | None:
        i = self.tfs.index(tf)
        return self.tfs[i + 1] if i + 1 < len(self.tfs) else None

    def trend_dir(self, tf: str) -> int:
        tc = self.trends.get(tf)
        return tc.direction if tc and tc.state in ACTIVE_STATES else 0

    def _process(self, tf: str, bar: Bar, market: Callable[[str], dict] | None) -> list[dict]:
        ind = self.bundles[tf]
        ind.update(bar)
        m = market(tf) if market else {}
        htf = self.higher_tf(tf)
        return self.trends[tf].update(ind, btc_dir=m.get("btc_dir"), rho=m.get("rho"),
                                      oi_change_pct=m.get("oi_change_pct"), funding=m.get("funding"),
                                      htf_dir=self.trend_dir(htf) if htf else 0)

    def on_1m_close(self, bar: Bar, market: Callable[[str], dict] | None = None) -> tuple[list[str], list[dict]]:
        """Feed a closed 1m bar. Returns (timeframes that closed, trend events)."""
        closed, events = ["1m"], self._process("1m", bar, market)
        for tf, agg in self.aggs.items():
            for b in agg.add(bar):
                closed.append(tf)
                events += self._process(tf, b, market)
        self.partial_1m = None
        return closed, events

    # ---------------------------------------------------------------- history seeding
    def seed(self, tf: str, bars: list[Bar], market: Callable[[str], dict] | None = None) -> None:
        """Seed a timeframe with closed historical bars (oldest first)."""
        for b in bars:
            if b.closed:
                self._process(tf, b, market)

    def seed_forming(self, bars_1m: list[Bar]) -> None:
        """Prime higher-timeframe aggregators with the 1m bars of their current, unfinished period."""
        if not bars_1m:
            return
        last = bars_1m[-1].open_time
        for tf, agg in self.aggs.items():
            start = last - last % TF_MS[tf]
            if last + 60_000 >= start + TF_MS[tf]:
                continue                         # period complete, already seeded as closed bar
            for b in bars_1m:
                if b.open_time >= start:
                    agg.add(b)

    def forming(self, tf: str) -> Bar | None:
        if tf == "1m":
            return self.partial_1m
        return self.aggs[tf].peek(self.partial_1m)
