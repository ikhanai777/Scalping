"""Outcome statistics: win rates with confidence intervals and expectancy, per strategy/detector."""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


def wilson(wins: int, n: int, z: float = 1.645) -> tuple[float, float]:
    """Wilson score interval (default 90%)."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


@dataclass
class OutcomeStats:
    """Tracks R-multiples of finished trades/signals."""
    window: int = 50
    rs: list[float] = field(default_factory=list)
    recent: deque = field(default_factory=lambda: deque(maxlen=50))

    def add(self, r: float) -> None:
        self.rs.append(r)
        self.recent.append(r)

    @property
    def n(self) -> int:
        return len(self.rs)

    @property
    def wins(self) -> int:
        return sum(1 for r in self.rs if r > 0)

    def p_win(self) -> float:
        return (self.wins + 1) / (self.n + 2)          # Laplace / Beta(1,1) posterior mean

    def ci(self) -> tuple[float, float]:
        return wilson(self.wins, self.n)

    def avg_win(self) -> float:
        w = [r for r in self.rs if r > 0]
        return sum(w) / len(w) if w else 1.0

    def avg_loss(self) -> float:
        lo = [-r for r in self.rs if r <= 0]
        return sum(lo) / len(lo) if lo else 1.0

    def expectancy(self) -> float:
        return sum(self.rs) / self.n if self.n else 0.0

    def profit_factor(self) -> float:
        gw = sum(r for r in self.rs if r > 0)
        gl = -sum(r for r in self.rs if r < 0)
        return gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)

    def recent_expectancy(self) -> float:
        return sum(self.recent) / len(self.recent) if self.recent else 0.0

    def expectancy_ci(self, z: float = 1.645) -> tuple[float, float]:
        n = self.n
        if n < 2:
            return (-math.inf, math.inf)
        m = self.expectancy()
        sd = math.sqrt(sum((r - m) ** 2 for r in self.rs) / (n - 1))
        return m - z * sd / math.sqrt(n), m + z * sd / math.sqrt(n)

    def summary(self) -> dict:
        lo, hi = self.ci()
        pf = self.profit_factor()
        return {"n": self.n, "win_rate": round(self.wins / self.n, 4) if self.n else None,
                "p_win": round(self.p_win(), 4), "p_win_ci": [round(lo, 4), round(hi, 4)],
                "expectancy_r": round(self.expectancy(), 4),
                "profit_factor": round(pf, 3) if math.isfinite(pf) else None,
                "avg_win_r": round(self.avg_win(), 3), "avg_loss_r": round(self.avg_loss(), 3),
                "recent_expectancy_r": round(self.recent_expectancy(), 4)}
