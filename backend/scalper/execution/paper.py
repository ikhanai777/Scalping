"""Paper broker: risk-sized bracket positions simulated with the shared TradeSim on live 1s bars."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from ..models import TF_MS, Bar, Candidate, Target
from ..sim import Costs, TradeSim
from .risk import RiskEngine


@dataclass
class Position:
    id: str
    sim: TradeSim
    qty: float
    risk_amount: float
    opened_at: int
    signal_id: str | None = None
    source: str = "manual"
    last_price: float = 0.0
    closed_at: int | None = None
    pnl: float = 0.0
    notes: str = ""

    def unrealized(self) -> float:
        s = self.sim
        if s.status != "open":
            return 0.0
        return s.remaining * s.d * (self.last_price - s.entry) * self.qty

    def to_json(self) -> dict:
        c, s = self.sim.c, self.sim
        return {"id": self.id, "symbol": c.symbol, "side": c.side, "qty": self.qty,
                "entry": getattr(s, "entry", c.entry), "stop": s.stop, "targets": [t.__dict__ for t in c.targets],
                "status": s.status, "remaining": round(s.remaining, 4), "last": self.last_price,
                "unrealized": round(self.unrealized(), 2), "pnl": round(self.pnl, 2), "r": round(s.r, 3),
                "risk_amount": round(self.risk_amount, 2), "opened_at": self.opened_at, "closed_at": self.closed_at,
                "exit_reason": s.exit_reason, "signal_id": self.signal_id, "source": self.source,
                "strategy": c.strategy, "fills": s.fills}


class PaperBroker:
    def __init__(self, risk: RiskEngine, costs: Costs, market: str = "spot"):
        self.risk = risk
        self.costs = costs
        self.market = market
        self.cash = risk.cfg.equity
        self.positions: dict[str, Position] = {}
        self.history: list[Position] = []
        self.last_price: dict[str, float] = {}

    @property
    def equity(self) -> float:
        return self.cash + sum(p.unrealized() for p in self.positions.values())

    def open(self, symbol: str, side: str, stop: float | None, targets: list[float] | None = None,
             price: float | None = None, risk_scale: float = 1.0, signal_id: str | None = None,
             strategy: str = "manual", time_stop_min: int = 240, trailing_atr: float | None = None,
             atr: float | None = None, step: float | None = None, min_notional: float = 0.0) -> tuple[Position | None, str | None]:
        px = price or self.last_price.get(symbol)
        if not px:
            return None, "no price for symbol yet"
        if side == "SHORT" and self.market == "spot":
            return None, "cannot short on spot (switch execution.market to futures)"
        blocked = self.risk.gate(self.equity, len(self.positions))
        if blocked:
            return None, blocked
        if stop is not None and ((side == "LONG" and stop >= px) or (side == "SHORT" and stop <= px)):
            return None, "stop is on the wrong side of the price"
        qty, why = self.risk.size(self.equity, px, stop, self.market, risk_scale, step, min_notional,
                                  cash=self.cash - self._locked())
        if why:
            return None, why
        d = 1 if side == "LONG" else -1
        risk = abs(px - stop)
        tg = targets or [px + d * risk, px + d * 2 * risk]
        share = 100 / len(tg)
        feats = {"trail_atr": trailing_atr, "atr": atr} if trailing_atr and atr else {}
        now = int(time.time() * 1000)
        cand = Candidate(strategy, symbol, "1m", side, px, stop, [Target(t, share) for t in tg], now,
                         max(1, time_stop_min), "market", [], feats, bool(trailing_atr))
        sim = TradeSim(cand, self.costs, fill_price=px, entry_time=now)
        pos = Position("pos_" + uuid.uuid4().hex[:10], sim, qty, qty * risk, now, signal_id,
                       "signal" if signal_id else "manual", px)
        self.positions[pos.id] = pos
        return pos, None

    def _locked(self) -> float:
        if self.market != "spot":
            return 0.0
        return sum(p.qty * p.sim.entry * p.sim.remaining for p in self.positions.values() if p.sim.status == "open")

    def on_bar_1s(self, symbol: str, bar: Bar) -> list[Position]:
        self.last_price[symbol] = bar.close
        done = []
        for pid, pos in list(self.positions.items()):
            if pos.sim.c.symbol != symbol:
                continue
            pos.last_price = bar.close
            if pos.sim.on_bar(bar, 1000):
                done.append(self._finalize(pid, bar.open_time + 1000))
        return done

    def on_price(self, symbol: str, price: float) -> None:
        self.last_price[symbol] = price
        for pos in self.positions.values():
            if pos.sim.c.symbol == symbol:
                pos.last_price = price

    def close(self, pid: str, reason: str = "manual") -> Position | None:
        pos = self.positions.get(pid)
        if not pos:
            return None
        px = self.last_price.get(pos.sim.c.symbol, pos.last_price)
        now = int(time.time() * 1000)
        pos.sim.close_now(px, now, reason)
        return self._finalize(pid, now)

    def move_stop_to_breakeven(self, pid: str) -> bool:
        pos = self.positions.get(pid)
        if not pos or pos.sim.status != "open":
            return False
        pos.sim.stop = pos.sim.entry
        return True

    def kill(self) -> list[Position]:
        out = [self.close(pid, "kill_switch") for pid in list(self.positions)]
        self.risk.killed = True
        return [p for p in out if p]

    def _finalize(self, pid: str, t: int) -> Position:
        pos = self.positions.pop(pid)
        pos.closed_at = t
        pos.pnl = pos.sim.r * pos.risk_amount
        self.cash += pos.pnl
        self.risk.on_close(pos.pnl, self.equity)
        self.history = (self.history + [pos])[-500:]
        return pos

    def account(self) -> dict:
        return {"mode": "paper", "market": self.market, "equity": round(self.equity, 2), "cash": round(self.cash, 2),
                "open": [p.to_json() for p in self.positions.values()],
                "history": [p.to_json() for p in reversed(self.history[-100:])],
                "risk": self.risk.status(self.equity, len(self.positions))}
