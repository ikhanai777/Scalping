import asyncio

import httpx
import pytest

from scalper.backtest.engine import Backtester
from scalper.bars import Aggregator
from scalper.binance.orderbook import LocalOrderBook
from scalper.config import load_config
from scalper.execution.binance_trader import BinanceTrader, round_step, sign
from scalper.execution.paper import PaperBroker
from scalper.execution.risk import RiskConfig, RiskEngine
from scalper.models import Bar, Candidate, Target
from scalper.news.service import NewsService
from scalper.signals.engine import SignalEngine
from scalper.signals.store import StatsStore
from scalper.sim import Costs, TradeSim

from .conftest import make_bars

NO_COST = Costs(0, 0, 0, 0)


def _bar(t, o, h, l, c, v=1.0):
    return Bar(t, o, h, l, c, v, v * c, 1, v / 2)


def _cand(side="LONG", entry=100.0, stop=99.0, targets=(101.0, 102.0), tf="1m", ts=0, **kw):
    return Candidate("T@1", "X", tf, side, entry, stop, [Target(p, 50) for p in targets], ts, kw.pop("time_stop", 60),
                     "market", [], kw.pop("features", {}), kw.pop("trailing", False))


# ---------------------------------------------------------------- trade simulator
def test_sim_stop_first_when_bar_hits_both():
    sim = TradeSim(_cand(), NO_COST, fill_price=100.0, entry_time=0)
    assert sim.on_bar(_bar(60_000, 100, 102.5, 98.5, 101))       # touches SL and both TPs
    assert sim.exit_reason == "stop" and sim.r == pytest.approx(-1.0)


def test_sim_targets_then_breakeven():
    sim = TradeSim(_cand(), NO_COST, fill_price=100.0, entry_time=0)
    assert not sim.on_bar(_bar(60_000, 100, 101.2, 99.6, 101))   # TP1 hit, stop -> breakeven
    assert sim.stop == pytest.approx(100.0)
    assert sim.on_bar(_bar(120_000, 101, 101.1, 99.9, 100))      # back to entry
    assert sim.exit_reason == "breakeven" and sim.r == pytest.approx(0.5)


def test_sim_full_target_and_costs():
    costs = Costs(0.001, 0.001, 0.001, 0.0)
    sim = TradeSim(_cand(), costs, fill_price=100.0, entry_time=0)
    sim.on_bar(_bar(60_000, 100, 102.5, 99.8, 102))
    assert sim.exit_reason == "tp2"
    gross = 0.5 * 1 + 0.5 * 2
    fees = 0.1 + 0.5 * 0.101 + 0.5 * 0.102
    assert sim.r == pytest.approx(gross - fees, rel=1e-6)


def test_sim_time_stop_and_short():
    sim = TradeSim(_cand("SHORT", 100, 101, (99, 98), time_stop=2), NO_COST, fill_price=100.0, entry_time=0)
    assert not sim.on_bar(_bar(0, 100, 100.2, 99.5, 99.6))
    assert sim.on_bar(_bar(60_000, 99.6, 100.1, 99.4, 99.5))
    assert sim.exit_reason == "time_stop" and sim.r == pytest.approx(0.5)


def test_sim_trailing_stop():
    c = _cand(targets=(101.0, 110.0), trailing=True, features={"trail_atr": 1.0, "atr": 1.0})
    sim = TradeSim(c, NO_COST, fill_price=100.0, entry_time=0)
    sim.on_bar(_bar(60_000, 100, 101.5, 99.9, 101.4))     # TP1 (50%), trail = 100.5
    sim.on_bar(_bar(120_000, 101.4, 104, 101.3, 103.8))   # trail -> 103
    assert sim.stop == pytest.approx(103.0)
    assert sim.on_bar(_bar(180_000, 103.8, 103.9, 102.5, 102.6))
    assert sim.exit_reason == "trailing_stop"
    assert sim.r == pytest.approx(0.5 * 1 + 0.5 * 3)


# ---------------------------------------------------------------- risk & paper broker
def test_risk_sizing_and_locks():
    r = RiskEngine(RiskConfig(equity=10_000, risk_per_trade=0.01, max_daily_loss=0.02, max_consecutive_losses=2))
    qty, why = r.size(10_000, 100.0, 99.0, "futures")
    assert why is None and qty == pytest.approx(100.0)        # 1% of 10k / 1.0 distance
    qty, why = r.size(10_000, 100.0, None, "spot")
    assert qty == 0 and "stop" in why
    qty, _ = r.size(10_000, 100.0, 99.99, "spot")              # capped by 1x notional on spot
    assert qty * 100 <= 10_000 + 1e-6
    r.on_close(-10, 9990)
    r.on_close(-10, 9980)
    assert "cooldown" in r.gate(9980, 0)
    r.cooldown_until = 0
    r.on_close(-190, 9790)
    assert "daily loss" in r.gate(9790, 0)


def test_paper_broker_bracket_lifecycle():
    risk = RiskEngine(RiskConfig(equity=1000, risk_per_trade=0.01))
    br = PaperBroker(risk, NO_COST, "futures")
    br.last_price["X"] = 100.0
    pos, err = br.open("X", "LONG", stop=99.0, targets=[102.0])
    assert err is None and pos.qty == pytest.approx(10.0)
    assert br.open("X", "LONG", stop=101.0)[1] == "stop is on the wrong side of the price"
    done = br.on_bar_1s("X", _bar(pos.opened_at + 1000, 100, 102.5, 99.5, 102))
    assert done and done[0].pnl == pytest.approx(20.0)        # +2R on 10 risk
    assert br.cash == pytest.approx(1020.0)
    spot = PaperBroker(RiskEngine(RiskConfig()), NO_COST, "spot")
    spot.last_price["X"] = 100.0
    assert "cannot short" in spot.open("X", "SHORT", stop=101.0)[1]


# ---------------------------------------------------------------- bars & order book
def test_aggregator_5m_and_gap():
    agg = Aggregator("5m")
    closed = []
    for b in make_bars(12, t0=1_700_000_100_000 - 1_700_000_100_000 % 300_000):
        closed += agg.add(b)
    assert len(closed) == 2 and all(c.closed for c in closed)
    assert closed[0].volume == pytest.approx(sum(b.volume for b in make_bars(12)[:5]))
    agg2 = Aggregator("5m")
    bars = make_bars(10, t0=0)
    out = []
    for b in bars[:3] + bars[6:]:          # missing minutes 3-5 -> first period closed by the gap
        out += agg2.add(b)
    assert out[0].open_time == 0 and out[1].open_time == 300_000


def test_orderbook_sync_and_gap():
    ob = LocalOrderBook("X")
    ob.on_diff({"E": 1, "U": 98, "u": 100, "b": [["10", "1"]], "a": [["11", "1"]]})   # buffered, stale
    ob.on_diff({"E": 2, "U": 101, "u": 103, "b": [["10", "2"]], "a": []})
    ob.on_snapshot({"lastUpdateId": 101, "bids": [["10", "5"], ["9", "1"]], "asks": [["11", "3"]]})
    assert ob.synced and ob.bids[10.0] == 2.0
    assert ob.on_diff({"E": 3, "U": 104, "u": 104, "b": [["9", "0"]], "a": [["11.5", "1"]]})
    assert 9.0 not in ob.bids and ob.best() == (10.0, 2.0, 11.0, 3.0)
    assert not ob.on_diff({"E": 4, "U": 110, "u": 111, "b": [], "a": []})              # gap
    assert not ob.synced and ob.resyncs == 1


# ---------------------------------------------------------------- backtest determinism
def test_backtest_deterministic():
    cfg = load_config()
    data = {"BTCUSDT": make_bars(1500, seed=11, start=50000, vol=0.0015),
            "ETHUSDT": make_bars(1500, seed=12, start=3000, vol=0.002)}
    r1 = Backtester(cfg, apply_filters=False).run(data)
    r2 = Backtester(cfg, apply_filters=False).run(data)
    assert [t["r"] for t in r1.trades] == [t["r"] for t in r2.trades]
    assert r1.trades, "expected some trades on 1500 random-walk bars without filters"


# ---------------------------------------------------------------- signal vetoes
def test_signal_engine_cost_veto_and_unvalidated():
    eng = SignalEngine({"min_confluence": 0, "show_unvalidated": True}, StatsStore(), Costs(0.001, 0.001, 0.001, 0.0002))
    tiny = _cand(entry=100, stop=99.9, targets=(100.1, 100.2))
    assert eng.evaluate(tiny, None) is None and "cost" in eng.rejected[-1]["reasons"][0]
    ok = _cand(entry=100, stop=98, targets=(102, 104))
    sig = eng.evaluate(ok, None)
    assert sig is not None and not sig.validated and sig.p_win is None
    eng2 = SignalEngine({"min_confluence": 0}, StatsStore(), Costs(0.001, 0.001, 0.001, 0.0002),
                        macro_block=lambda ts: {"title": "CPI"})
    assert eng2.evaluate(ok, None) is None and "macro" in eng2.rejected[-1]["reasons"][0]


def test_signal_engine_uses_stats_and_ev():
    st = StatsStore()
    st.set_backtest("T", [1.3] * 40 + [-1.0] * 60)          # 40% win, +1.3R / -1R => EV < 0
    eng = SignalEngine({"min_confluence": 0, "min_p_win": 0.35}, st, NO_COST)
    assert eng.evaluate(_cand(entry=100, stop=98, targets=(102, 104)), None) is None   # EV <= 0 rejected
    st.set_backtest("T", [2.0] * 45 + [-1.0] * 55)
    sig = eng.evaluate(_cand(entry=100, stop=98, targets=(102, 104)), None)
    assert sig.validated and sig.probability_source == "stats" and sig.expected_r > 0


# ---------------------------------------------------------------- news
def test_news_tagging_and_vetoes():
    ns = NewsService({"rss": [], "macro_block_before_min": 15, "macro_block_after_min": 30})
    ns.set_universe({"BTC", "ETH", "SOL", "LINK", "NEAR"})
    it = ns.ingest("X", "Solana surges as ETH lags; Binance will delist LINK", "u1", 1000)
    assert set(it.symbols) >= {"SOL", "ETH"} and it.event_type == "delisting"
    assert ns.coin_veto("SOLUSDT", "LONG", 2000)
    assert ns.coin_veto("BTCUSDT", "LONG", 2000) is None
    assert ns.ingest("Y", "Solana surges as ETH lags; Binance will delist LINK!", "u2", 1100) is None   # near-dup
    assert "NEAR" not in ns.tag("We are near the end")           # ambiguous ticker without context
    ns.events = [{"ts": 30 * 60_000, "title": "CPI", "country": "USD", "impact": "High"}]
    assert ns.macro_block(0) is None and ns.macro_block(20 * 60_000)["title"] == "CPI"


# ---------------------------------------------------------------- order signing (mocked exchange)
def test_signing_and_rounding(monkeypatch):
    import hashlib
    import hmac
    assert sign("secret", {"a": 1, "b": "x"}) == hmac.new(b"secret", b"a=1&b=x", hashlib.sha256).hexdigest()
    assert round_step(1.23456, 0.001) == 1.234 and round_step(105, 10) == 100


def test_trader_rejects_withdrawal_keys(monkeypatch):
    monkeypatch.setenv("K", "key")
    monkeypatch.setenv("S", "sec")
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path.endswith("/time"):
            return httpx.Response(200, json={"serverTime": 0})
        if req.url.path.endswith("/exchangeInfo"):
            return httpx.Response(200, json={"symbols": []})
        if req.url.path.endswith("/order"):
            assert req.headers["X-MBX-APIKEY"] == "key" and "signature" in req.url.params
            return httpx.Response(200, json={"orderId": 1, "executedQty": "1"})
        return httpx.Response(200, json={})

    t = BinanceTrader("futures", "testnet", "K", "S", client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                                                 base_url="https://testnet.binancefuture.com"))
    asyncio.run(t.start())
    out = asyncio.run(t.bracket("BTCUSDT", "LONG", 0.01, 49000, 52000))
    assert calls.count("/fapi/v1/order") == 3 and out["entry"]["orderId"] == 1
