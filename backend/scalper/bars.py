"""Bar aggregation: build higher-timeframe bars from 1m bars (live and in backtests)."""
from __future__ import annotations

from .models import TF_MS, Bar


class Aggregator:
    """Aggregates consecutive base bars into bars of ``tf``.

    ``add`` returns the bars that closed (usually zero or one; two after a data gap).
    ``forming`` is the partial bar.
    """

    def __init__(self, tf: str, base_ms: int = 60_000):
        self.tf = tf
        self.tf_ms = TF_MS[tf]
        self.base_ms = base_ms
        self.forming: Bar | None = None

    def _start(self, t: int) -> int:
        return t - (t % self.tf_ms)

    def add(self, b: Bar) -> list[Bar]:
        out: list[Bar] = []
        start = self._start(b.open_time)
        f = self.forming
        if f is not None and start != f.open_time:      # gap: previous period never got its last bar
            f.closed = True
            out.append(f)
            f = None
        if f is None:
            f = Bar(start, b.open, b.high, b.low, b.close, b.volume, b.quote_volume, b.trades,
                    b.taker_buy_volume, False)
        else:
            f.high = max(f.high, b.high)
            f.low = min(f.low, b.low)
            f.close = b.close
            f.volume += b.volume
            f.quote_volume += b.quote_volume
            f.trades += b.trades
            f.taker_buy_volume += b.taker_buy_volume
        self.forming = f
        if b.open_time + self.base_ms >= start + self.tf_ms:  # last base bar of the period
            f.closed = True
            self.forming = None
            out.append(f)
        return out

    def peek(self, partial: Bar | None = None) -> Bar | None:
        """Forming bar including an unfinished base bar (for display only)."""
        f = self.forming
        if partial is None:
            return f
        start = self._start(partial.open_time)
        if f is None or f.open_time != start:
            return Bar(start, partial.open, partial.high, partial.low, partial.close, partial.volume,
                       partial.quote_volume, partial.trades, partial.taker_buy_volume, False)
        return Bar(f.open_time, f.open, max(f.high, partial.high), min(f.low, partial.low), partial.close,
                   f.volume + partial.volume, f.quote_volume + partial.quote_volume,
                   f.trades + partial.trades, f.taker_buy_volume + partial.taker_buy_volume, False)
