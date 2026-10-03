"""Command line: serve the terminal, backtest, evaluate the trend catcher, train the meta-model."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

from .config import data_dir, load_config


def _symbols(s: str) -> list[str]:
    return [x.strip().upper() for x in s.split(",") if x.strip()]


def _load(cfg, symbols, days, market="spot", interval="1m"):
    from .backtest.data import load_history
    out = {}
    for s in symbols:
        t = time.time()
        rest = cfg.binance.spot_rest if market == "spot" else cfg.binance.futures_rest
        out[s] = load_history(s, interval, days, data_dir(cfg), market, rest)
        print(f"  loaded {s:<12} {len(out[s]):>7} bars ({time.time() - t:.1f}s)", file=sys.stderr)
    return out


def _table(metrics: dict) -> str:
    cols = ["n", "win_rate", "expectancy_r", "profit_factor", "total_r", "max_dd_r", "walk_forward_positive"]
    lines = [f"{'strategy':<24}" + "".join(f"{c:>14}" for c in cols) + "  gate"]
    for sid, m in sorted(metrics.items()):
        row = f"{sid:<24}"
        for c in cols:
            v = m.get(c)
            row += f"{('—' if v is None else (f'{v:.3f}' if isinstance(v, float) else str(v))):>14}"
        row += "  " + ("PASS" if m.get("gates", {}).get("passed") else "fail")
        lines.append(row)
    return "\n".join(lines)


def cmd_serve(args, cfg):
    import uvicorn
    from .api import create_app
    host = args.host or cfg.get_path("server.host", "127.0.0.1")
    port = args.port or cfg.get_path("server.port", 8000)
    print(f"Scalping terminal on http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="info")


def cmd_backtest(args, cfg):
    from .backtest.engine import Backtester
    from .signals.store import StatsStore
    data = _load(cfg, _symbols(args.symbols), args.days, args.market)
    t = time.time()
    bt = Backtester(cfg, args.market, sensitivity=args.sensitivity, apply_filters=not args.no_filters)
    res = bt.run(data, progress=lambda i, n: print(f"  {i / n:.0%}", end="\r", file=sys.stderr))
    m = res.metrics()
    print(f"\nBacktest {args.days}d on {', '.join(data)} ({args.market}, fees+slippage included) in {time.time() - t:.0f}s\n")
    print(_table(m))
    print("\nTrend Catcher (CONFIRMED trends that extended ≥ 2 ATR):")
    for tf, s in res.trend_stats.items():
        sm = s.summary()
        c = sm["confirmed_reached_2atr"]
        print(f"  {tf:>4}: detected {sm['detected']:>5}  confirmed {sm['confirmed']:>5}  "
              f"reached 2ATR {c['win_rate'] if c['win_rate'] is not None else '—'}  (n={c['n']})")
    if args.save_stats:
        st = StatsStore(data_dir(cfg) / "stats.json")
        for sid, rs in res.by_strategy().items():
            st.set_backtest(sid, rs)
        st.save()
        print(f"\nSaved per-strategy statistics to {data_dir(cfg) / 'stats.json'} (used by the live signal engine)")
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"metrics": m, "trades": res.trades, "trend": {tf: s.summary() for tf, s in res.trend_stats.items()}},
                      f, indent=1, default=str)
        print(f"Wrote {args.out}")


def cmd_trends(args, cfg):
    from .backtest.trend_eval import evaluate, large_move_threshold, run_trends
    tfs = cfg.get_path("timeframes.trend", ["1m", "5m", "15m", "1h", "4h"])
    data = _load(cfg, _symbols(args.symbols), args.days)
    for sym, bars in data.items():
        thr = args.threshold or large_move_threshold(bars, args.k, args.min_pct)
        print(f"\n{sym}: large move = {thr:.2f}% ")
        print(f"  {'sensitivity':<13}{'tf':>5}{'confirmed':>11}{'precision':>11}{'recall':>9}{'move left':>11}{'false/day':>11}")
        for sens in ("early", "balanced", "conservative"):
            e = evaluate(bars, run_trends(bars, sym, tfs, sens), thr, tfs)
            for tf, r in e["by_tf"].items():
                f = lambda v: "—" if v is None else f"{v:.2f}"   # noqa: E731
                print(f"  {sens:<13}{tf:>5}{r['confirmed']:>11}{f(r['precision']):>11}{f(r['recall']):>9}"
                      f"{f(r['median_move_remaining_at_confirm']):>11}{r['false_starts_per_day']:>11}")
        print(f"  ({e['large_moves']} large moves in {e['days']} days; a random-direction entry would score "
              f"precision ≈ {e['baseline_precision']})")


def cmd_train(args, cfg):
    from .backtest.engine import Backtester
    from .ml.meta import MetaModel, train_strategy
    data = _load(cfg, _symbols(args.symbols), args.days, args.market)
    res = Backtester(cfg, args.market, collect_features=True).run(
        data, progress=lambda i, n: print(f"  {i / n:.0%}", end="\r", file=sys.stderr))
    by: dict[str, list] = {}
    for row in res.features:
        by.setdefault(row["_strategy"], []).append(row)
    mm = MetaModel(data_dir(cfg) / "models.joblib")
    mm.models = {}
    for sid, rows in by.items():
        m = train_strategy(rows, min_rows=args.min_rows)
        if m is None:
            print(f"  {sid:<24} skipped ({len(rows)} rows; need ≥ {args.min_rows} with both classes)")
            continue
        mt = m["metrics"]
        print(f"  {sid:<24} rows {mt['n_rows']:>6}  AUC(oos) {mt['auc_oos']:.3f}  Brier {mt['brier_cal']:.4f} "
              f"vs base {mt['brier_base']:.4f}  ECE {mt['ece']:.3f}  "
              f"{'USED' if mt['beats_base_rate'] else 'not used (no edge over base rate)'}")
        mm.models[sid] = m
    mm.save()
    print(f"Saved models to {data_dir(cfg) / 'models.joblib'}")


def cmd_pump(args, cfg):
    from .backtest.data import fetch_klines_rest, bars_from_df
    from .backtest.engine import run_pump_backtest
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = end - int(args.hours * 3_600_000)
    data = {}
    for s in _symbols(args.symbols):
        data[s] = bars_from_df(fetch_klines_rest(s, "1s", start, end, cfg.binance.spot_rest))
        print(f"  loaded {s} {len(data[s])} 1s bars", file=sys.stderr)
    out = run_pump_backtest(cfg, data, args.market)
    print(json.dumps({"events": len(out["events"]), "metrics": out["metrics"],
                      "detector_continuation": out["detector_continuation"]}, indent=1))
    for e in out["closed_events"][-20:]:
        print(f"  {e['symbol']:<12} {e['direction']:<5} {e['stage']:<10} move {e['move_pct']:+.2f}%  outcome {e['outcome']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="scalper", description="Free Binance scalping terminal")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the terminal (API + web UI)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    b = sub.add_parser("backtest", help="walk-forward backtest of all bar strategies")
    b.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--market", default="spot", choices=["spot", "futures"])
    b.add_argument("--sensitivity", default=None, choices=[None, "early", "balanced", "conservative"])
    b.add_argument("--no-filters", action="store_true", help="skip confluence/cost/regime filters")
    b.add_argument("--save-stats", action="store_true", help="store results as the live engine's strategy stats")
    b.add_argument("--out", help="write trades + metrics JSON")
    t = sub.add_parser("evaluate-trends", help="Trend Catcher recall/precision vs labelled large moves")
    t.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    t.add_argument("--days", type=int, default=30)
    t.add_argument("--k", type=float, default=3.0, help="large move = max(k x ATR(1h), min-pct)")
    t.add_argument("--min-pct", type=float, default=2.0)
    t.add_argument("--threshold", type=float, help="fixed large-move threshold in percent")
    tr = sub.add_parser("train", help="train the ML meta-model (LightGBM, purged walk-forward, calibrated)")
    tr.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT")
    tr.add_argument("--days", type=int, default=60)
    tr.add_argument("--market", default="spot", choices=["spot", "futures"])
    tr.add_argument("--min-rows", type=int, default=200)
    pb = sub.add_parser("pump-backtest", help="replay recent 1s bars through the Pump & Dump Detector")
    pb.add_argument("--symbols", required=True)
    pb.add_argument("--hours", type=float, default=6)
    pb.add_argument("--market", default="spot", choices=["spot", "futures"])
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):      # Windows consoles/pipes may not be UTF-8 (≥, —, emoji)
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    {"serve": cmd_serve, "backtest": cmd_backtest, "evaluate-trends": cmd_trends, "train": cmd_train,
     "pump-backtest": cmd_pump}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
