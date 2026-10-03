"""Risk engine (spec §11.3): pre-trade checks that the UI cannot bypass, sizing, daily locks, cooldowns."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class RiskConfig:
    equity: float = 10_000
    risk_per_trade: float = 0.005
    max_daily_loss: float = 0.02
    max_consecutive_losses: int = 4
    cooldown_minutes: int = 30
    max_open_positions: int = 3
    max_leverage: float = 5
    require_stop: bool = True

    @staticmethod
    def from_cfg(d: dict) -> "RiskConfig":
        return RiskConfig(**{k: v for k, v in d.items() if k in RiskConfig.__dataclass_fields__})


class RiskEngine:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.day = self._day()
        self.day_start_equity = cfg.equity
        self.realized_today = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = 0.0
        self.killed = False

    @staticmethod
    def _day() -> int:
        return int(time.time() // 86_400)

    def _roll(self, equity: float) -> None:
        d = self._day()
        if d != self.day:
            self.day, self.day_start_equity, self.realized_today = d, equity, 0.0
            self.killed = False

    def on_close(self, pnl: float, equity: float) -> None:
        self._roll(equity)
        self.realized_today += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.cooldown_until = time.time() + self.cfg.cooldown_minutes * 60
                self.consecutive_losses = 0
        elif pnl > 0:
            self.consecutive_losses = 0

    def lock_reason(self, equity: float) -> str | None:
        """Account-level locks only (kill switch, daily loss, cooldown) — used as a signal veto."""
        return self.gate(equity, -10**9)

    def gate(self, equity: float, open_positions: int) -> str | None:
        """Reason trading is blocked, or None."""
        self._roll(equity)
        if self.killed:
            return "kill switch active (until next UTC day or manual reset)"
        if self.realized_today <= -self.cfg.max_daily_loss * self.day_start_equity:
            return f"daily loss limit {self.cfg.max_daily_loss:.1%} reached"
        if time.time() < self.cooldown_until:
            return f"cooldown after {self.cfg.max_consecutive_losses} losses ({int(self.cooldown_until - time.time())}s left)"
        if open_positions >= self.cfg.max_open_positions:
            return f"max open positions ({self.cfg.max_open_positions})"
        return None

    def size(self, equity: float, entry: float, stop: float | None, market: str, risk_scale: float = 1.0,
             step: float | None = None, min_notional: float = 0.0, cash: float | None = None) -> tuple[float, str | None]:
        """Position size from risk per trade; returns (qty, rejection reason)."""
        if stop is None or stop == entry:
            if self.cfg.require_stop:
                return 0.0, "stop loss required"
            return 0.0, "cannot size without a stop"
        risk_amt = equity * self.cfg.risk_per_trade * risk_scale
        qty = risk_amt / abs(entry - stop)
        max_notional = equity * (self.cfg.max_leverage if market == "futures" else 1.0)
        if cash is not None and market == "spot":
            max_notional = min(max_notional, cash)
        if qty * entry > max_notional:
            qty = max_notional / entry
        if step:
            qty = (qty // step) * step
        if qty <= 0:
            return 0.0, "size rounds to zero"
        if qty * entry < min_notional:
            return 0.0, f"below min notional {min_notional}"
        return qty, None

    def status(self, equity: float, open_positions: int) -> dict:
        return {"blocked": self.gate(equity, open_positions), "realized_today": round(self.realized_today, 2),
                "daily_loss_limit": round(self.cfg.max_daily_loss * self.day_start_equity, 2),
                "consecutive_losses": self.consecutive_losses, "killed": self.killed,
                "cooldown_s": max(0, int(self.cooldown_until - time.time())),
                "config": self.cfg.__dict__}
