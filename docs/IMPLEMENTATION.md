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

### Strategies on futures: `scalper backtest --market futures --days 60 --save-stats` (6 majors, 2026-08-04 → 2026-10-02)

Futures fees (0.05% taker) plus 2 bps slippage per side, 8,573 trades. The saved statistics are in
[`results/stats-futures-60d.json`](../results/stats-futures-60d.json). To use them, copy the file to
`data/stats.json` and set `execution.market: futures`.

| Strategy | Trades | Win rate | Net expectancy | Profit factor | Walk-forward windows positive |
|---|---|---|---|---|---|
| S4 liquidity sweep | 332 | 52% | −0.12R | 0.79 | 1/6 |
| S6 session breakout | 180 | 48% | −0.16R | 0.68 | 2/6 |
| S3 squeeze breakout | 277 | 48% | −0.17R | 0.71 | 1/6 |
| S1 trend pullback | 1,171 | 47% | −0.19R | 0.65 | 0/6 |
| S12 BTC catch-up | 18 | 44% | −0.23R | 0.64 | 2/5 |
| S9 Supertrend+MACD (baseline) | 4,219 | 36% | −0.23R | 0.46 | 0/6 |
| S10 Trend Catcher entry | 2,376 | 30% | −0.27R | 0.59 | 1/6 |

None pass the gates. Splitting by timeframe, the gross edge before costs ranges from −0.07R to +0.21R,
and costs are still 0.10–0.30R per trade at these stop distances. The one positive slice is
**S4 liquidity sweep on 15m: +0.02R net, 58% win rate, 184 trades**. That is not statistically
significant, but it is the best candidate for further work. Statistics are currently pooled per
strategy, not per timeframe, so that slice is still suppressed live.

Trend continuation after CONFIRMED (≥ 2 ATR beyond the confirmation price) was 52–57% across
timeframes over 60 days.

## Next steps (suggested order)

1. Key statistics per strategy × timeframe (so slices like S4-15m can pass on their own), then train the meta-model (`scalper train --market futures`) to filter low-quality setups.
2. Enable `storage.record_trades` to build order-book/trade history, then backtest S5 and order-flow features.
3. Add volume profile, footprint and liquidity heatmap to the chart.
4. Wire the Binance executor behind an explicit testnet-first flow with a confirmation UI.
5. Add parameter sweeps with deflated-Sharpe/PBO reporting, and PSI drift detection for scheduled retraining.
