"""Trade simulator shared by the backtester, paper trading and the live signal tracker.

Conservative assumptions: if a bar touches both stop and target, the stop is assumed first;
stops fill with slippage at taker fee, targets fill at the limit price with maker fee.

Maker (limit) entries: a resting buy limit fills only when price trades *through* it by
``maker_through`` (touching the price is not enough — the queue ahead of us may absorb it), at the
limit or a better gap-open price, with maker fee and no slippage. The bar that fills the order may
stop it out but may not also hit a target (we cannot know the intrabar order). Orders not filled
within ``maker_expiry_bars`` signal-timeframe bars expire as "missed" (0R, excluded from statistics).
This captures adverse selection: limit orders fill most reliably when price moves against them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import TF_MS, Bar, Candidate


@dataclass
class Costs:
    entry_fee: float = 0.001           # taker entry
    exit_fee_taker: float = 0.001
    exit_fee_maker: float = 0.001
    slippage: float = 0.0002          # fraction of price
    entry_fee_maker: float | None = None

    def round_trip(self, maker_entry: bool = False) -> float:
        if maker_entry:
            return self.maker_entry_fee() + self.exit_fee_taker + self.slippage
        return self.entry_fee + self.exit_fee_taker + 2 * self.slippage

    def maker_entry_fee(self) -> float:
        return self.exit_fee_maker if self.entry_fee_maker is None else self.entry_fee_maker

    @staticmethod
    def from_cfg(fees: dict, market: str = "spot") -> "Costs":
        disc = 0.75 if (fees.get("bnb_discount") and market == "spot") else 1.0
        taker = fees.get(f"{market}_taker", 0.001) * disc
        maker = fees.get(f"{market}_maker", 0.001) * disc
        return Costs(taker, taker, maker, fees.get("slippage_bps", 2.0) / 10_000, maker)


@dataclass
class TradeSim:
    c: Candidate
    costs: Costs
    fill_price: float | None = None    # None => fill at the next bar's open (backtests)
    entry_time: int | None = None
    status: str = "pending"            # pending | open | done
    exit_reason: str | None = None
    remaining: float = 1.0
    realized: float = 0.0              # sum(portion * signed price move) in price units
    fees_paid: float = 0.0             # fraction-of-price fees * portion
    stop: float = 0.0
    trail: float | None = None
    extreme: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0
    targets_hit: int = 0
    exit_time: int | None = None
    fills: list = field(default_factory=list)
    maker_through: float = 0.0001      # limit fills need price to trade 1 bp through it
    maker_expiry_bars: int = 2         # signal-timeframe bars a resting entry order lives
    rested_ms: int = 0

    def __post_init__(self):
        self.d = 1 if self.c.side == "LONG" else -1
        self.stop = self.c.stop
        self.limit_ms = self.c.time_stop_bars * TF_MS.get(self.c.tf, 60_000)
        self.risk = abs(self.c.entry - self.c.stop)
        self.trail_k = self.c.features.get("trail_atr")
        self.trail_atr = self.c.features.get("atr")
        if self.fill_price is not None:
            self._open(self.fill_price, self.entry_time or self.c.ts)

    def _open(self, price: float, t: int, maker: bool = False) -> None:
        self.entry = price if maker else price + self.d * price * self.costs.slippage
        self.entry_time = t
        self.extreme = self.entry
        self.status = "open"
        self.maker_entry = maker
        self.fees_paid += (self.costs.maker_entry_fee() if maker else self.costs.entry_fee) * self.entry
        self.fills.append(("entry", t, self.entry))
        # keep the planned risk distance if slippage moved the entry
        if self.d * (self.entry - self.stop) <= 0:
            self.stop = self.entry - self.d * self.risk

    def _exit(self, portion: float, price: float, t: int, maker: bool, reason: str) -> None:
        fee = self.costs.exit_fee_maker if maker else self.costs.exit_fee_taker
        if not maker:
            price = price - self.d * price * self.costs.slippage
        self.realized += portion * self.d * (price - self.entry)
        self.fees_paid += portion * fee * price
        self.remaining -= portion
        self.fills.append((reason, t, price, portion))
        if self.remaining <= 1e-9:
            self.status, self.exit_reason, self.exit_time = "done", reason, t

    def on_bar(self, bar: Bar, bar_ms: int = 60_000) -> bool:
        """Advance with one bar. Returns True when the trade is finished."""
        if self.status == "done":
            return True
        fill_bar = False
        if self.status == "pending":
            if self.c.entry_type == "limit":
                lim = self.c.entry
                through = lim * self.maker_through
                if (self.d > 0 and bar.low <= lim - through) or (self.d < 0 and bar.high >= lim + through):
                    gap_better = (bar.open < lim) if self.d > 0 else (bar.open > lim)
                    self._open(bar.open if gap_better else lim, bar.open_time, maker=True)
                    fill_bar = True
                else:
                    self.rested_ms += bar_ms        # time the order has rested unfilled
                    if self.rested_ms >= self.maker_expiry_bars * TF_MS.get(self.c.tf, 60_000):
                        self.status, self.exit_reason, self.exit_time = "done", "missed", bar.open_time + bar_ms
                        return True
                    return False
            else:
                self._open(bar.open, bar.open_time)
        risk = self.risk or 1e-12
        adverse = bar.low if self.d > 0 else bar.high
        favorable = bar.high if self.d > 0 else bar.low
        self.mae = min(self.mae, self.d * (adverse - self.entry) / risk)
        self.mfe = max(self.mfe, self.d * (favorable - self.entry) / risk)
        # stop first (conservative)
        if self.d * (adverse - self.stop) <= 0:
            px = self.stop if self.d * (bar.open - self.stop) > 0 else bar.open    # gap through stop
            self._exit(self.remaining, px, bar.open_time, False,
                       "trailing_stop" if self.trail is not None and self.stop == self.trail else
                       ("breakeven" if self.targets_hit and abs(self.stop - self.entry) < 1e-12 else "stop"))
            return True
        if fill_bar:                        # intrabar order unknown: no targets on the fill bar
            self.extreme = max(self.entry, bar.close) if self.d > 0 else min(self.entry, bar.close)
            return False
        targets = self.c.targets[:1] if self.c.trailing else self.c.targets
        while self.targets_hit < len(targets):
            tg = targets[self.targets_hit]
            if self.d * (favorable - tg.price) < 0:
                break
            last = self.targets_hit == len(targets) - 1 and not self.c.trailing
            portion = self.remaining if last else min(self.remaining, tg.size_pct / 100)
            self._exit(portion, tg.price, bar.open_time, True, f"tp{self.targets_hit + 1}")
            self.targets_hit += 1
            if self.status == "done":
                return True
            if not self.c.trailing:
                self.stop = self.entry          # move to breakeven after the first target
        # trailing stop update (after evaluating this bar)
        self.extreme = max(self.extreme, bar.high) if self.d > 0 else min(self.extreme, bar.low)
        if self.c.trailing and self.trail_k and self.trail_atr:
            t = self.extreme - self.d * self.trail_k * self.trail_atr
            self.trail = t if self.trail is None else (max(self.trail, t) if self.d > 0 else min(self.trail, t))
            if self.d * (self.trail - self.stop) > 0:
                self.stop = self.trail
        if bar.open_time + bar_ms - self.entry_time >= self.limit_ms:
            self._exit(self.remaining, bar.close, bar.open_time + bar_ms, False, "time_stop")
            return True
        return False

    def close_now(self, price: float, t: int, reason: str = "manual") -> None:
        if self.status == "open":
            self._exit(self.remaining, price, t, False, reason)
        elif self.status == "pending":
            self.status, self.exit_reason = "done", reason

    @property
    def r(self) -> float:
        """Net R multiple including fees and slippage."""
        if self.status == "pending" or not self.risk:
            return 0.0
        if self.exit_reason in ("expired", "missed"):
            return 0.0
        return (self.realized - self.fees_paid) / self.risk

    def to_json(self) -> dict:
        return {"strategy": self.c.strategy, "symbol": self.c.symbol, "side": self.c.side, "tf": self.c.tf,
                "signal_ts": self.c.ts, "entry_time": self.entry_time, "exit_time": self.exit_time,
                "entry": getattr(self, "entry", None), "stop": self.c.stop, "r": round(self.r, 4),
                "exit_reason": self.exit_reason, "mae": round(self.mae, 3), "mfe": round(self.mfe, 3),
                "targets_hit": self.targets_hit, "reasons": self.c.reasons}


def as_maker(c: Candidate, atr: float | None, offset_atr: float = 0.0) -> Candidate:
    """Turn a market-entry candidate into a resting limit order ``offset_atr`` ATRs better than the
    signal price. Stop and targets keep their prices, so risk is measured from the limit price."""
    import dataclasses
    if c.entry_type == "limit":
        return c
    d = 1 if c.side == "LONG" else -1
    entry = c.entry - d * offset_atr * (atr or 0.0)
    if d * (entry - c.stop) <= 0:                 # offset would put the entry beyond the stop
        entry = c.entry
    return dataclasses.replace(c, entry=entry, entry_type="limit",
                               reasons=c.reasons + [f"limit entry {'-' if d > 0 else '+'}{offset_atr:g} ATR"])
