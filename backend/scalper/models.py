"""Core data types shared across the engine."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

TF_MS: dict[str, int] = {
    "1s": 1_000,
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class Bar:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    trades: int = 0
    taker_buy_volume: float = 0.0
    closed: bool = True

    @property
    def delta(self) -> float:
        """Aggressive buy volume minus aggressive sell volume."""
        return 2.0 * self.taker_buy_volume - self.volume

    @staticmethod
    def from_kline(k: list | dict) -> "Bar":
        """Build from a REST kline array or a WebSocket kline payload (`k` object)."""
        if isinstance(k, dict):
            return Bar(int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                       float(k["v"]), float(k["q"]), int(k["n"]), float(k["V"]), bool(k["x"]))
        return Bar(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]),
                   float(k[7]), int(k[8]), float(k[9]), True)

    def to_json(self) -> dict:
        return {"t": self.open_time, "o": self.open, "h": self.high, "l": self.low, "c": self.close,
                "v": self.volume, "q": self.quote_volume, "n": self.trades, "tb": self.taker_buy_volume,
                "x": self.closed}


@dataclass(slots=True)
class Trade:
    ts: int
    price: float
    qty: float
    is_buyer_maker: bool
    agg_id: int = 0

    @property
    def side(self) -> str:
        return "SELL" if self.is_buyer_maker else "BUY"


@dataclass
class Target:
    price: float
    size_pct: float


@dataclass
class Candidate:
    """A strategy's raw trade idea before filtering and scoring."""
    strategy: str
    symbol: str
    tf: str
    side: str                      # LONG | SHORT
    entry: float
    stop: float
    targets: list[Target]
    ts: int
    time_stop_bars: int = 15
    entry_type: str = "market"     # market | limit
    reasons: list[str] = field(default_factory=list)
    features: dict[str, float] = field(default_factory=dict)
    trailing: bool = False         # exit by trailing stop instead of final target

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    def r_multiple(self, price: float) -> float:
        if self.risk <= 0:
            return 0.0
        sign = 1 if self.side == "LONG" else -1
        return sign * (price - self.entry) / self.risk


@dataclass
class Signal:
    candidate: Candidate
    p_win: float | None
    p_win_ci: tuple[float, float] | None
    expected_r: float | None
    confluence: float
    confluence_parts: dict[str, float]
    regime: str
    risks: list[str]
    validated: bool
    probability_source: str        # model | stats | none
    id: str = field(default_factory=lambda: "sig_" + uuid.uuid4().hex[:12])
    status: str = "pending"        # pending|triggered|tp1_hit|tp|stopped|time_stopped|invalidated|expired
    outcome_r: float | None = None
    drivers: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        c = self.candidate
        return {
            "id": self.id, "ts": c.ts, "symbol": c.symbol, "tf": c.tf, "side": c.side,
            "strategy": c.strategy, "entry": {"type": c.entry_type, "price": c.entry},
            "stop": c.stop, "targets": [asdict(t) for t in c.targets],
            "time_stop_bars": c.time_stop_bars, "trailing": c.trailing,
            "p_win": self.p_win, "p_win_ci": list(self.p_win_ci) if self.p_win_ci else None,
            "expected_R": self.expected_r, "confluence": round(self.confluence, 1),
            "confluence_parts": {k: round(v, 1) for k, v in self.confluence_parts.items()},
            "regime": self.regime, "reasons": c.reasons, "risks": self.risks,
            "validated": self.validated, "probability_source": self.probability_source,
            "status": self.status, "outcome_r": self.outcome_r, "drivers": self.drivers,
        }
