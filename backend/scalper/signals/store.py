"""Per-strategy outcome statistics (backtest + live), persisted as JSON."""
from __future__ import annotations

import json
from pathlib import Path

from ..stats import OutcomeStats


def strategy_id(key: str) -> str:
    return key.split("@")[0]


class StatsStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.backtest: dict[str, OutcomeStats] = {}
        self.live: dict[str, OutcomeStats] = {}
        self.shadow: dict[str, str] = {}          # strategy id -> reason it was degraded
        if path and path.exists():
            self.load()

    def add(self, key: str, r: float, source: str = "live") -> None:
        sid = strategy_id(key)
        book = self.live if source == "live" else self.backtest
        book.setdefault(sid, OutcomeStats()).add(r)
        if source == "live":
            self._check_degrade(sid)

    def set_backtest(self, sid: str, rs: list[float]) -> None:
        st = OutcomeStats()
        for r in rs:
            st.add(r)
        self.backtest[sid] = st

    def combined(self, key: str) -> OutcomeStats:
        sid = strategy_id(key)
        out = OutcomeStats()
        for book in (self.backtest, self.live):
            for r in book.get(sid, OutcomeStats()).rs:
                out.add(r)
        return out

    def _check_degrade(self, sid: str) -> None:
        """Spec §8.5: rolling 50-signal expectancy below the backtest CI lower bound => shadow mode."""
        live, bt = self.live.get(sid), self.backtest.get(sid)
        if not live or not bt or len(live.recent) < 50 or bt.n < 30:
            return
        lo, _ = bt.expectancy_ci()
        if live.recent_expectancy() < lo:
            self.shadow[sid] = (f"live 50-signal expectancy {live.recent_expectancy():.2f}R "
                                f"< backtest lower bound {lo:.2f}R")
        elif sid in self.shadow and live.recent_expectancy() >= bt.expectancy():
            del self.shadow[sid]

    def summary(self) -> dict:
        ids = sorted(set(self.backtest) | set(self.live))
        return {sid: {"backtest": self.backtest.get(sid, OutcomeStats()).summary(),
                      "live": self.live.get(sid, OutcomeStats()).summary(),
                      "shadow": self.shadow.get(sid)} for sid in ids}

    def save(self) -> None:
        if not self.path:
            return
        data = {"backtest": {k: v.rs[-5000:] for k, v in self.backtest.items()},
                "live": {k: v.rs[-5000:] for k, v in self.live.items()},
                "shadow": self.shadow}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for name in ("backtest", "live"):
            book = getattr(self, name)
            for k, rs in data.get(name, {}).items():
                st = OutcomeStats()
                for r in rs:
                    st.add(r)
                book[k] = st
        self.shadow = data.get("shadow", {})
