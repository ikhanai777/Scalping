# Implementation status (v0.1)

This page maps [`SPEC.md`](SPEC.md) to what is built, records the first measured results, and lists
the gaps. All numbers below come from the built-in tools on real Binance spot data. None are
estimates.

## Spec coverage

| Spec | Status | Notes |
|---|---|---|
| §4 Market data | ✅ | Combined-stream WS manager (dynamic subscribe, reconnect + jitter, stale watchdog, 24h rotation), REST with weight governor (429/418 handling), local order book with documented snapshot + diff sync and gap resync, clock sync, data.binance.vision bulk history with Parquet cache, optional Parquet recorder for trades/book (`storage.record_trades`). |
| §5 Indicators | ✅ mostly | All trend/momentum/volatility indicators, VWAP ±σ, CVD/delta, OBI, microprice, walls, large trades, trade intensity, divergences, swing structure. TA-Lib conformance tests for SMA, EMA, RSI, ATR, ADX/±DI, Bollinger, MACD, KAMA, OBV, linear-regression slope. **Not yet:** volume profile (POC/VAH/VAL), footprint candles, Ichimoku, Parabolic SAR, fair-value gaps, time-of-day RVOL baseline (rolling SMA is used instead). |
| §5.5 Derivatives | ✅ when reachable | Funding, mark, OI and ΔOI, liquidation stream with z-score. Long/short and taker-ratio clients exist but are not used as features yet. No estimated liquidation heatmap. |
| §6.1 Strategies | ✅ S1–S7, S9–S12 | S8 is used as a confluence/risk input, not as a standalone strategy. S5 (book) and S7 (liquidations) run live only, because free history has no order book or liquidations. Record data to backtest them. |
| §6.3 Trend Catcher | ✅ | Evidence groups, score, state machine, sensitivity presets, Chandelier trail, MTF alignment, chart shading/markers/channel/strip, Trend Board, validation tool with random baseline. |
| §6.4 BTC coupling | ✅ | ρ (Pearson + Spearman), β, R², lead–lag with significance, residual move, labels, chart badge, BTC overlay, follower filter, catch-up strategy (S12). Not yet: ETH or sector leaders as second references, and a dedicated residual chart pane. |
| §6.5 Pump & Dump | ✅ | Two-tier scanning, robust z-scores, stages, organic/isolated/suspicious class, chase limit, S11, radar, markers, alerts, replay backtest. Not yet: spot-vs-perp leadership and social-mention spikes. |
| §7 Signal engine | ✅ | Vetoes, confluence, regime gating, model → stats → unvalidated probability chain, EV after costs, live tracking of every signal through the shared simulator, auto shadow mode. |
| §7.3 ML | ✅ | LightGBM, purged and embargoed walk-forward, isotonic calibration, Brier and ECE, SHAP drivers via `pred_contrib`. Retraining is manual (`scalper train`). Drift detection (PSI) is not implemented. |
| §7.4 Regime | ⚠️ heuristic | ADX / ATR percentile. No HMM or Hurst. |
| §8 Backtesting | ✅ | Event-driven portfolio replay of the live code path, realistic costs, walk-forward windows, bootstrap CI, promotion gates. Not yet: deflated Sharpe, PBO/CSCV, parameter optimisation sweeps, HTML report (JSON via `--out`). |
| §9 News | ✅ | 5 RSS feeds, Binance announcements, CryptoPanic (optional key), economic calendar, Fear & Greed, dedup, tagging, event typing, VADER + crypto lexicon (FinBERT optional), impact, vetoes, catalysts for the radar. Impact weights are heuristic and not yet calibrated against price reactions. |
| §10 UI | ✅ mostly | Scanner, chart with all overlays and hover cards, MTF strip, coupling badge, DOM with walls, tape, order ticket, hotkeys, tabs (signals, trend board, radar, news + calendar, positions, strategies, coupling). **Not yet:** liquidity heatmap, footprint view, drawing tools, journal screenshots, desktop push notifications. |
| §11 Execution & risk | ⚠️ paper only | Risk engine and paper broker are complete. The signed Binance executor (`execution/binance_trader.py`) is mock-tested only and **not wired into the UI**. Every order in v0.1 is simulated. |
| §12 Non-functional | ⚠️ | Status endpoint covers feed health, reconnects, weight and errors. No Prometheus/Grafana metrics. |

## First measured results (spot, fees 0.1% + 2 bps slippage per side, 14 days to 2026-10-03)

### Trend Catcher: `scalper evaluate-trends --days 14`

A "large move" is a zig-zag leg ≥ max(2%, 3 × median 1h ATR). Precision is the share of CONFIRMED
trends that fall inside a same-direction large move. A random-direction entry at a random time scores
about **0.45**.

| Symbol | Sensitivity | TF | Precision | Recall | Move left at confirm | False starts/day |
|---|---|---|---|---|---|---|
| BTCUSDT | balanced | 5m | 0.61 | 1.00 | 57% | 4.5 |
| SOLUSDT | balanced | 5m | 0.65 | 0.94 | 61% | 4.3 |
| DOGEUSDT | balanced | 5m | 0.84 | 0.92 | 59% | 5.2 |
| BTCUSDT | balanced | 1m | 0.56 | 1.00 | 74% | 26.9 |
| SOLUSDT | balanced | 15m | 0.81 | 0.81 | 37% | 1.5 |

Reading: on 5m the detector finds almost every large move while roughly 60% of the move is still
ahead, and its direction and timing beat random by 16–39 points. On 1m it is earlier but noisy, at
about 25 false starts per day. Sample: 11–24 large moves per symbol, so treat these as preliminary.

### Strategies: `scalper backtest --days 14` (6 majors)

| Strategy | Trades | Win rate | Net expectancy | Profit factor |
|---|---|---|---|---|
| S3 squeeze breakout | 24 | 54% | +0.04R | 1.07 |
| S9 Supertrend+MACD (baseline) | 506 | 36% | −0.24R | 0.43 |
| S10 Trend Catcher entry | 298 | 33% | −0.31R | 0.53 |
| S1 trend pullback | 77 | 40% | −0.32R | 0.51 |
| S4 liquidity sweep | 23 | 43% | −0.30R | 0.56 |

None pass the §8.4 gates. The cost breakdown shows why: before costs, most setups are close to
break-even (−0.05R to +0.3R), and spot round-trip costs are 0.15–0.36R per trade at these stop
distances. In practice this means:

- With these statistics stored, the signal engine suppresses negative-EV strategies automatically.
  The terminal shows few or no validated signals instead of plausible-looking losing ones.
- Lower fees change the picture most: futures (0.05% taker), BNB discount, or maker entries.
  Next most useful are longer holding periods (wider stops relative to costs) and the ML meta-model
  filtering out low-quality setups. Run `scalper backtest --market futures --days 60` and
  `scalper train` on your own history before trusting any strategy.
- Fourteen days is a small sample. Strategy parameters were deliberately **not** tuned to it, because
  that would overfit.

## Next steps (suggested order)

1. Run 60–90 day backtests on futures fees, then train the meta-model and compare calibrated vs. raw signals.
2. Enable `storage.record_trades` to build order-book/trade history, then backtest S5 and order-flow features.
3. Add volume profile, footprint and liquidity heatmap to the chart.
4. Wire the Binance executor behind an explicit testnet-first flow with a confirmation UI.
5. Add parameter sweeps with deflated-Sharpe/PBO reporting, and PSI drift detection for scheduled retraining.
