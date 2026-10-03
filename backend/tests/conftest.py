import math
import random

import pytest

from scalper.models import Bar


def make_bars(n: int = 3000, seed: int = 7, start: float = 100.0, drift: float = 0.0,
              vol: float = 0.002, t0: int = 1_700_000_000_000, step_ms: int = 60_000) -> list[Bar]:
    """Synthetic random-walk OHLCV bars (deterministic)."""
    rng = random.Random(seed)
    bars, price = [], start
    for i in range(n):
        o = price
        r = rng.gauss(drift, vol)
        c = o * math.exp(r)
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        v = abs(rng.gauss(100, 30)) + 1
        tb = v * min(max(0.5 + r / (4 * vol), 0.05), 0.95)
        bars.append(Bar(t0 + i * step_ms, o, h, l, c, v, v * c, int(v), tb, True))
        price = c
    return bars


@pytest.fixture
def bars():
    return make_bars()
