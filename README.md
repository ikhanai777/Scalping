# Scalping Terminal

A free, open-source scalping terminal for Binance. It combines live charts and order flow with buy/sell
signals that carry a measured probability, a Trend Catcher that runs on every active coin, BTC-coupling
analysis, a market-wide Pump & Dump radar, and a news/macro feed. All of it runs on free software and
free data. The full product specification is in [`docs/SPEC.md`](docs/SPEC.md). What's built, first measured results
and known gaps are in [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).

> **Not financial advice.** Signals are probabilistic and can be wrong. Crypto trading, and leveraged
> scalping in particular, carries a high risk of loss. The terminal starts in **paper trading** mode.
> Keep it there until a strategy has proven itself on your own data.

## What's included

| Area | What it does |
|---|---|
| **Market data** | Binance public WebSocket/REST (spot via `data-stream.binance.vision`, futures optional): 1m klines for the trend universe, 1s klines for the whole radar universe, depth/aggTrades/bookTicker for focused or escalated symbols. Local order book with gap detection and resync, reconnects, rate-limit governor, clock sync. |
| **Indicators** | Incremental, TA-Lib-conformant (tested): EMA/SMA/KAMA, RSI, Stoch RSI, MACD, ATR, ADX/DI, Bollinger, Keltner, squeeze, Supertrend, Donchian, linear-regression t-stat, CUSUM change-points, session VWAP ±σ, CVD/delta, OBV, RVOL, swing structure, RSI/CVD divergences. |
| **Trend Catcher** | Early up/down trend detection per coin × timeframe (1m–4h). Combines structure breaks, adaptive slope, EMA ribbon, ADX, change-points, CVD/volume, OI and BTC alignment. States run EARLY → CONFIRMED → MATURE → EXHAUSTING → ENDED, with a Chandelier trailing stop. On the chart: shading, T▲/T▼ markers, trend channel and a multi-timeframe strip. Trend Board panel. Sensitivity can be set to early, balanced or conservative. |
| **BTC coupling** | Rolling correlation, β, R², lead–lag (5s cross-correlation with a significance test) and residual "own move" for each coin. Labels: FOLLOWER / PARTIAL / INDEPENDENT / DECOUPLING / +LAGGER. Follower signals that go against BTC's trend are penalised or vetoed. |
| **Pump & Dump radar** | Scans every liquid USDT pair second by second using robust z-scores of price velocity, volume, aggressor flow, book pressure, liquidations and OI. Stages run WATCH → IGNITION → CONFIRMED → EXHAUSTION → REVERSAL. Each move gets a chase limit and is classed as organic, isolated or suspicious. |
| **Strategies** | S1 trend pullback · S2 VWAP reversion · S3 squeeze breakout · S4 liquidity sweep · S5 order-flow momentum (live book) · S6 session breakout · S7 liquidation cascade (futures) · S9 Supertrend+MACD baseline · S10 Trend Catcher entry · S11 pump momentum · S12 BTC catch-up. |
| **Signal engine** | Vetoes (spread, stale data, macro window, coin news, risk locks, target < 3× costs, regime, degraded strategy) → explainable confluence score → probability (calibrated ML model if trained, otherwise backtest+live win rate with confidence interval) → expected value after fees. Signals are labelled **unvalidated** until a strategy has enough results. |
| **Backtester** | Event-driven and portfolio-wide, running the same code path as live. Includes fees, slippage, next-bar fills, conservative stop-first ordering, partial targets, breakeven and trailing exits. Reports walk-forward windows, bootstrap confidence intervals and promotion gates. Includes a pump-detector replay on 1s bars and a Trend Catcher recall/precision evaluation. |
| **ML meta-model** | LightGBM per strategy with purged and embargoed walk-forward CV, isotonic calibration, Brier score and ECE, and per-signal SHAP drivers. A model is only used if it beats the base rate out-of-sample. |
| **News & events** | RSS (CoinDesk, Cointelegraph, The Block, Decrypt, Blockworks), Binance listing/delisting announcements, optional CryptoPanic, economic calendar, Fear & Greed. Coin tagging, event typing, sentiment (VADER + crypto lexicon, optional FinBERT), impact score, macro and coin vetoes. |
| **Trading** | Paper broker with risk-based sizing, bracket orders, breakeven and kill switch. The risk engine enforces max risk per trade, daily loss lock, loss-streak cooldown and max positions, and requires a stop. A signed Binance executor (testnet/live, refuses keys that can withdraw) is included as a module but is **not yet connected to the UI**: in v0.1 every order is paper-simulated. |
| **Alerts** | Telegram and Discord for validated signals, confirmed multi-timeframe trends, pump/dump stages and high-impact news. |
| **UI** | React + TradingView Lightweight Charts: scanner, chart with signal and trend overlays, hover cards, DOM ladder, time & sales, order ticket, plus tabs for signals, trend board, radar, news and calendar, positions, strategy stats and coupling. |

## Quick start

### Docker (simplest)

```bash
docker compose up --build
# open http://127.0.0.1:8000
```

### Local (Python 3.11+, Node 20+)

```bash
cd backend && pip install -e ".[ml]" && cd ..
cd frontend && npm install && npm run build && cd ..
scalper serve                    # http://127.0.0.1:8000
```

For UI development, run `scalper serve` and `cd frontend && npm run dev` together, then open http://localhost:5173.

The first start takes a minute or two. The terminal backfills history for the trend universe, warms up
the radar on recent 1s klines, and runs a short backtest in the background (`bootstrap` in the config)
so strategies have initial statistics.

## Making the signals trustworthy (recommended workflow)

```bash
# 1. Backtest on real history (data.binance.vision, cached under data/) and store the stats for the live engine
scalper backtest --days 60 --save-stats

# 2. Check how early and how reliably the Trend Catcher finds large moves, per timeframe and sensitivity
scalper evaluate-trends --days 30 --symbols BTCUSDT,ETHUSDT,SOLUSDT

# 3. Train the calibrated ML meta-model (only models that beat the base rate out-of-sample are used)
scalper train --days 90

# 4. Replay the pump detector on recent 1s data for coins you care about
scalper pump-backtest --symbols PEPEUSDT,WIFUSDT --hours 12

# 5. Paper trade for at least 2 weeks and compare live vs backtest in the "Strategies & stats" tab
```

Every number the UI shows is measured: P(win), its confidence interval, expected R after costs, trend
continuation rates and live results. Nothing is hard-coded. Strategies whose live results fall below
their backtest confidence band are moved to shadow mode automatically.

### A note on fees

Spot taker fees (0.1% per side, plus slippage) are larger than most 1-minute moves on major coins. The
engine therefore rejects setups whose first target is under 3× the round-trip cost. Expect most valid
signals on 5m/15m, or on futures (lower fees). This is intentional: a 1m signal that cannot cover its
costs loses money even when it is "right".

## Configuration

Defaults are in [`config/default.yaml`](config/default.yaml). Override them in `config/local.yaml`
(gitignored) or with environment variables such as `SCALPER_SIGNALS__MIN_CONFLUENCE=60` or
`SCALPER_ALERTS__TELEGRAM_TOKEN=...` (see `.env.example`). Useful settings:

- `universe.trend_universe_size`: how many top-volume pairs get full trend tracking (default 60).
- `trend.sensitivity`: `early` | `balanced` | `conservative`.
- `pump.*`: radar thresholds (volume multiple, velocity z, chase limit).
- `execution.market`: `spot` | `futures` (futures enables paper shorts and uses futures fees).
- `risk.*`: equity, risk per trade, daily loss limit, cooldowns, max positions/leverage.

Binance restricts access from some countries, and futures endpoints are blocked in more places than
spot market data. If futures data is unreachable, the terminal keeps running and turns the derivatives
features off. Check that using Binance is legal where you live.

## Hotkeys

`Shift+B` buy · `Shift+S` sell (futures) · `Shift+X` close all positions on the current symbol. Click the
chart or the order book to set the stop price.

## Tests

```bash
cd backend && pip install -e ".[dev]" && pytest      # indicators vs TA-Lib, detectors, simulator, risk, book sync, …
cd frontend && npm run typecheck
```

## Project layout

```
backend/scalper/
  binance/       REST + WebSocket clients, local order book, tape, book metrics
  indicators/    incremental indicator library + per-timeframe bundle
  detectors/     trend catcher, BTC coupling, pump & dump detector
  strategies/    S1–S12
  signals/       vetoes, confluence, probability, stats store
  backtest/      portfolio backtester, data loader, trend evaluation
  ml/            features + LightGBM meta-model
  news/          sources, tagging, sentiment, impact, vetoes
  execution/     risk engine, paper broker, signed Binance executor (experimental)
  engine.py      live orchestration     api.py  FastAPI + WebSocket     cli.py  commands
frontend/src/    React UI (chart, scanner, DOM, tabs)
config/          default.yaml
docs/SPEC.md     product & technical specification
```

## Licence & attribution

MIT. Charts use [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts) (Apache 2.0).
