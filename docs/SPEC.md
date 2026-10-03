# Binance Scalping Terminal — Product & Technical Specification

| | |
|---|---|
| **Version** | 1.0 (draft) |
| **Date** | 2026-10-03 |
| **Status** | Proposed |
| **Licence target** | 100% free / open-source stack, free data sources only |

---

## 0. Read this first: what "highest possible accuracy" means here

No indicator, strategy, or model predicts short-term price moves reliably all the time. Scalping
on 1s–5m timeframes is close to a zero-sum game where fees and slippage decide most outcomes. The
terminal therefore does **not** try to maximise a raw win rate. It aims for:

1. **Calibrated probabilities.** When a signal says "62% probability of hitting target before stop",
   historically about 62% of such signals should have done that. This is measured and shown on screen.
2. **Positive expectancy after costs.** A signal is emitted only if
   `P(win) × avg_win − (1 − P(win)) × avg_loss − fees − expected_slippage > 0`.
3. **Confluence over single indicators.** Signals need agreement between independent evidence
   families (trend, momentum, volume/flow, order book, derivatives positioning, news/regime).
4. **Honest, continuous validation.** Every strategy is backtested with walk-forward and purged
   cross-validation, paper-traded forward, and tracked live. Strategies that degrade are disabled
   automatically.
5. **Knowing when not to trade.** Filters for news events, spread spikes, low liquidity, and
   unfavourable regimes often add more to accuracy than any entry rule.

Any accuracy figure in the UI must come from measured out-of-sample or live data and must never
be hard-coded or marketing copy.

---

## 1. Goals and non-goals

### 1.1 Goals
- A real-time scalping terminal for **Binance Spot and USDⓈ-M Futures**.
- Fast charts (tick, 1s, 1m, 3m, 5m, 15m, plus 1h/4h for context) with **buy/sell signal markers**,
  entry/stop/target levels, and the reasons behind each signal.
- Order-flow tools: depth ladder (DOM), liquidity heatmap, time & sales, cumulative volume delta,
  footprint candles.
- A news and sentiment feed tagged per coin, with impact scoring and event-based trade blocking.
- A **Trend Catcher** that detects and marks up/down trends early on every active coin, with each
  coin's **BTC coupling** (follower / independent, β, lag) shown.
- A market-wide **Pump & Dump Detector** that flags abnormal moves on any Binance USDT pair within
  seconds of ignition.
- Strategy engine, backtester, paper trading, and optional live execution with strict risk controls.
- Uses only **free** software and **free** data sources. Binance trading fees still apply to real trades.

### 1.2 Non-goals (v1)
- Fully autonomous trading by default. Auto-execution exists but is off by default and gated
  behind paper-trading results (see §11.4).
- Exchanges other than Binance (the architecture leaves room for adapters later).
- HFT co-location, sub-millisecond strategies, market making.
- Guaranteed returns. The product is decision support plus tooling.

---

## 2. Users and core use cases

| Persona | Needs |
|---|---|
| Discretionary scalper | Clean fast chart, DOM, flow, signals as confirmation, one-click/hotkey orders |
| Semi-systematic trader | Configurable strategies, confluence scores, alerts to phone |
| Quant/tinkerer | Backtester, feature store, ML pipeline, exportable data |

**Top use cases**
1. Watch 5–20 pairs, get ranked "opportunity" alerts, open the best pair, confirm with flow, enter with a hotkey.
2. Receive a Telegram alert with entry/SL/TP and probability, then execute in the terminal or on Binance.
3. Backtest a strategy on 6–24 months of 1m data plus recorded order-book/trade data, then paper trade it.
4. Avoid trading into CPI/FOMC releases, Binance listing/delisting announcements, or liquidation cascades.

---

## 3. System architecture

```
                ┌──────────────────────── Binance (free public APIs) ───────────────────────┐
                │ Spot WS/REST · Futures WS/REST · Futures data endpoints · Announcements    │
                └──────────────┬────────────────────────────────────────┬────────────────────┘
                               │ market data                             │ orders / user data (signed)
┌──────────────────────────────▼───────────────┐            ┌───────────▼──────────────┐
│ Ingestion service (asyncio)                  │            │ Execution service        │
│ - WS multiplexer, reconnect, gap recovery    │            │ - order router, OCO/brkt │
│ - local order book builder                   │            │ - risk gate, kill switch │
│ - candle builder (tick→1s→1m…)               │            │ - user data stream       │
└──────┬──────────────────────────┬────────────┘            └───────────▲──────────────┘
       │ normalized events (in-proc bus / Redis Streams)                │
┌──────▼─────────┐   ┌────────────▼───────────┐   ┌────────────────┐    │
│ Storage        │   │ Feature & indicator    │   │ News/sentiment │    │
│ DuckDB/Parquet │   │ engine (incremental)   │◄──┤ service        │    │
│ (+ optional    │   └────────────┬───────────┘   └────────────────┘    │
│  TimescaleDB)  │                │                                     │
└──────▲─────────┘   ┌────────────▼───────────┐                         │
       │             │ Strategy & signal      │── signals ──────────────┤
       │             │ engine + ML scorer     │                         │
       │             │ + regime + filters     │                         │
       │             └────────────┬───────────┘                         │
       │                          │                                     │
┌──────┴──────────┐   ┌───────────▼────────────┐   ┌──────────────────┐ │
│ Backtester /    │   │ API gateway (FastAPI,  │◄──┤ Alerts: Telegram │ │
│ research (CLI,  │   │ WebSocket push)        │   │ Discord, browser │ │
│ notebooks)      │   └───────────┬────────────┘   └──────────────────┘ │
└─────────────────┘               │                                     │
                      ┌───────────▼────────────┐                        │
                      │ Web UI (React + TS,    │── order intents ───────┘
                      │ Lightweight Charts)    │
                      └────────────────────────┘
```

### 3.1 Technology stack (all free / open-source)

| Layer | Choice | Licence / cost |
|---|---|---|
| Backend language | Python 3.12+ (asyncio, uvloop) | Free |
| Hot paths | NumPy + Numba (or Rust via PyO3 if profiling shows it's needed) | Free |
| API | Starlette + native WebSockets (pure Python, also runs inside the Android app) | BSD |
| Binance client | Own thin client, or `python-binance` / `binance-connector` (official) | MIT |
| Indicators | Own incremental implementations, cross-checked against TA-Lib and `pandas-ta` | BSD/MIT |
| Storage | DuckDB + Parquet (research and history); SQLite (app state); optional TimescaleDB | MIT/PostgreSQL |
| Message bus | In-process asyncio queues; Redis Streams when running multiple processes | BSD |
| ML | LightGBM, scikit-learn, Optuna; optional PyTorch | MIT/BSD |
| NLP sentiment | Hugging Face `transformers`, FinBERT / crypto-tuned BERT models run locally on CPU | Apache 2.0 |
| Frontend | React + TypeScript + Vite | MIT |
| Charts | TradingView **Lightweight Charts** (Apache 2.0, attribution required) + custom canvas/WebGL panes for heatmap & footprint | Apache 2.0 |
| Alerts | Telegram Bot API, Discord webhooks, Web Push | Free |
| Packaging | Docker Compose; optional Tauri desktop wrapper | Free |
| Hosting | Local PC, or a free-tier VM (e.g. Oracle Cloud Always Free). A Tokyo region keeps latency to Binance's matching engine low | Free |

---

## 4. Market data (Binance, free)

All market data endpoints below are public and need no API key. Use the market-data-only hosts
(`data-api.binance.vision`, `data-stream.binance.vision`) where they serve the data, to stay clear of
trading rate limits. Binance changes endpoints and limits from time to time, so the client must read
`exchangeInfo` at startup and keep endpoint paths in configuration, not code.

### 4.1 WebSocket streams

| Purpose | Spot stream | Futures stream (`fstream.binance.com`) |
|---|---|---|
| Trades (aggressor side) | `<sym>@aggTrade`, `<sym>@trade` | `<sym>@aggTrade` |
| Best bid/ask | `<sym>@bookTicker` | `<sym>@bookTicker` |
| Order book diffs | `<sym>@depth@100ms` | `<sym>@depth@100ms` (or `@0ms` where offered) |
| Partial book | `<sym>@depth20@100ms` | `<sym>@depth20@100ms` |
| Klines | `<sym>@kline_1s`, `@kline_1m`… | `<sym>@kline_1m`… |
| Mark price & funding | — | `<sym>@markPrice@1s` |
| Liquidations | — | `<sym>@forceOrder`, `!forceOrder@arr` |
| 24h stats | `<sym>@ticker`, `!miniTicker@arr` | same |

**Requirements**
- Combined streams over as few connections as possible, within Binance's per-connection stream limit.
- Respond to ping frames. Reconnect before the 24h forced disconnect. Exponential backoff with jitter.
- **Gap detection.** Order book: use the documented snapshot + diff procedure (`U`/`u`/`pu` sequence IDs).
  On a gap, resync from a REST snapshot. Trades: check `aggTrade` ID continuity and backfill via REST.
- **Clock sync.** Measure offset against `/api/v3/time` every 60s. Every event stores exchange time,
  local receive time, and the computed latency.

### 4.2 REST endpoints (backfill and context)

| Data | Endpoint (Spot `/api/v3`, Futures `/fapi/v1`) |
|---|---|
| Symbols, filters, tick/lot size | `exchangeInfo` |
| Historical klines | `klines` (paged), plus bulk history from **data.binance.vision** (free daily/monthly zip dumps of klines, aggTrades, trades, futures metrics) |
| Book snapshot | `depth?limit=1000` (futures up to 1000) |
| Historical trades | `aggTrades` |
| Funding rate history | `/fapi/v1/fundingRate` |
| Open interest | `/fapi/v1/openInterest`, `/futures/data/openInterestHist` |
| Long/short ratios | `/futures/data/globalLongShortAccountRatio`, `/futures/data/topLongShortPositionRatio`, `/futures/data/takerlongshortRatio` |
| Basis | `/futures/data/basis` |

- A rate-limit governor tracks `X-MBX-USED-WEIGHT-*` response headers, keeps a safety margin, and
  backs off on HTTP 429. It stops completely on 418 (IP ban).

### 4.3 Data recording
- Record raw `aggTrade`, `bookTicker`, depth diffs (or top-N snapshots every 100ms), mark price, and
  liquidations for watched symbols to Parquet, partitioned by `symbol/date/hour`.
- This recorded data is what makes order-flow strategies backtestable, because Binance's free dumps
  do not include historical order books.
- Retention is configurable (default: 90 days raw book, unlimited trades/klines). Budget roughly
  1–5 GB per symbol-month for top-20 book snapshots, compressed.

### 4.4 Derived bars
- Time bars: 1s, 5s, 15s, 1m, 3m, 5m, 15m, 1h, 4h, built locally from trades so they match recorded data.
- Activity bars: tick bars, volume bars, dollar bars. These often give cleaner statistics for ML.
- Range/Renko bars as an optional chart type.

---

## 5. Indicator library

All indicators must:
- update **incrementally** per tick and per bar close, without recomputing history;
- match TA-Lib / `pandas-ta` reference outputs within 1e-9 relative error on a test dataset
  (CI test, see §14);
- clearly separate the *forming* bar value from the *closed* bar value. **Signals use closed-bar
  values only**, unless a strategy is explicitly tick-based. This prevents repainting.

### 5.1 Trend
EMA (9, 21, 50, 200), SMA, HMA, VWMA · Supertrend (ATR 10, ×3) · Ichimoku (fast scalping preset 6/13/26) ·
Parabolic SAR · ADX/DMI (14) · Linear-regression slope & channel · Multi-timeframe trend state
(1m/5m/15m/1h aligned up / down / mixed).

### 5.2 Momentum and oscillators
RSI (14, plus RSI(2) for pullbacks) · Stochastic RSI (14,14,3,3) · MACD (12,26,9, plus a fast 5,13,6
preset) · CCI · Williams %R · ROC · Automatic **divergence detection** (regular and hidden) for RSI,
MACD, and CVD against price swing pivots.

### 5.3 Volatility
ATR (14) and ATR percentile · Bollinger Bands (20, 2) and bandwidth · Keltner Channels · **Squeeze**
(Bollinger inside Keltner) · Donchian channels · Realised volatility (Parkinson / Garman-Klass) per window.

### 5.4 Volume and order flow (the main scalping edge)
| Indicator | Definition |
|---|---|
| **VWAP** (session / anchored / rolling) | Plus ±1/2/3σ bands; session resets at 00:00 UTC, with optional Asia/London/NY anchors |
| **Volume profile** | Visible-range and session profiles: POC, VAH/VAL (70%), HVN/LVN |
| **Delta & CVD** | Buy minus sell aggressor volume from `aggTrade` `m` flag; per-bar delta, cumulative, session CVD |
| **Footprint** | Bid × ask volume per price level per bar; diagonal imbalance ≥ 300%; stacked imbalances |
| **Order book imbalance (OBI)** | `(Σbid_qty − Σask_qty)/(Σbid_qty + Σask_qty)` over top N levels or within ±x bps, smoothed |
| **Microprice** | `(ask·bid_qty + bid·ask_qty)/(bid_qty + ask_qty)` |
| **Liquidity walls** | Resting size > k × median level size; tracks persistence, pulls, and spoofing-like behaviour (walls that vanish as price approaches) |
| **Absorption** | High aggressive volume at a level with little price progress |
| **Large-trade detector** | Trades above the 99th percentile of size (rolling), shown as bubbles |
| **Trade intensity** | Trades per second vs. rolling baseline |
| OBV, MFI, relative volume (RVOL vs. same time-of-day average) | Standard |

### 5.5 Derivatives positioning (Futures, free)
Funding rate and predicted funding · Open interest and ΔOI (price↑ + OI↑ = new longs, and so on) ·
Long/short account and top-trader ratios · Taker buy/sell volume ratio · **Liquidation stream**
(clusters, cascade detection) · Basis (perp vs. spot / quarterly) · Estimated liquidation-level heatmap
(derived from OI changes and common leverage tiers; clearly labelled as an estimate).

### 5.6 Market context
BTC and ETH trend/volatility as a regime driver for altcoins · BTC dominance and total market cap
(CoinGecko free API) · Rolling altcoin–BTC correlation and beta · Fear & Greed index (alternative.me,
free) · Session (Asia/London/NY) and time-of-day volatility profile · Weekend/holiday flag.

### 5.7 Structure
Swing highs/lows (fractal or ZigZag with ATR threshold) · Market structure (HH/HL/LH/LL, break of
structure, change of character) · Previous day/week high, low, close · Round numbers · Fair-value gaps
· Liquidity pools (equal highs/lows) · Automatic support/resistance from volume profile and swing
clusters, scored by number of touches.

---

## 6. Strategy library

Each strategy is a plug-in with a common interface:

```python
class Strategy(Protocol):
    id: str
    timeframes: list[str]          # e.g. ["1m", "5m"] (primary + filter TF)
    params: StrategyParams          # typed, with ranges for optimisation
    def on_bar(self, ctx: Context) -> list[Candidate]: ...
    def on_tick(self, ctx: Context) -> list[Candidate]: ...   # optional
# Candidate = side, entry, stop, targets[], invalidation, features{}, reasons[]
```

Every candidate carries a **stop based on structure or ATR**, at least one target, an invalidation
condition, and a time stop (for example, close after N bars if neither stop nor target is hit).

### 6.1 Built-in strategies (v1)

| # | Strategy | Logic (long side; short is mirrored) | Best regime |
|---|---|---|---|
| S1 | **Trend pullback (EMA 9/21 + VWAP)** | 5m/15m trend up (EMA21 > EMA50, price > VWAP). On 1m, price pulls back into the EMA9–21 zone or VWAP, RSI(2) < 10 or StochRSI crosses up from < 20, then a bullish close with positive delta. SL below pullback low − 0.2·ATR. TP 1.5–2R or VWAP +1σ | Trending |
| S2 | **VWAP band mean reversion** | Price at VWAP −2σ/−3σ, ADX < 20, RSI < 25 with bullish divergence, absorption or CVD divergence at the low. TP at VWAP. SL beyond −3σ or the swing low | Ranging |
| S3 | **Squeeze breakout** | BB inside KC for ≥ N bars. Break of range high with RVOL > 2, delta > 0, OBI > 0.2, then retest or immediate entry. SL inside the range. TP = measured move or ATR multiple | Compression → expansion |
| S4 | **Liquidity sweep reversal** | Price takes out equal lows / previous-session low / round number by a small amount, then reclaims it within 1–3 bars, with a CVD bullish divergence or absorption on the footprint. SL below the sweep wick. TP at opposite liquidity or VWAP | Any, best at key levels |
| S5 | **Order-flow momentum (tick-based)** | OBI > threshold for > X ms, microprice > mid, aggressive buy burst (trade intensity z > 2), ask wall pulled. Very short holding time (seconds to a few minutes). Needs a low-latency setup and maker/taker fee awareness | Liquid pairs only |
| S6 | **Session / opening-range breakout** | Range of the first 15–30 min of London or NY session. Breakout with volume and HTF trend agreement, retest entry | Session opens |
| S7 | **Liquidation cascade fade / follow** | Liquidation burst above the 99th percentile with ΔOI sharply negative: *fade* when price reaches a high-volume node with absorption; *follow* when the book is thin and OBI agrees | High volatility |
| S8 | **Funding / positioning extreme** | Extreme funding + crowded long/short ratio + price failing at a level → contrarian bias. Used mainly as a **filter / score input**, not a standalone trigger | Any |
| S9 | **Supertrend + MACD fast** | Classic, simple baseline, kept as a benchmark that the rest must beat | Trending |
| S10 | **Trend Catcher entry** | Enter on the `EARLY` → `CONFIRMED` transition of the Trend Catcher (§6.3), or on the first pullback after it. SL = Chandelier/structure stop. Exit with a trailing stop, not a fixed target | Trend onset |
| S11 | **Pump / dump momentum** | Ride an `IGNITION`/`CONFIRMED` event from the Pump & Dump Detector (§6.5) with a strict chase limit. An optional fade setup on `EXHAUSTION` is off by default | Abnormal moves |
| S12 | **BTC lead–lag catch-up** | BTC starts a confirmed move. A high-beta follower whose lag is measured as significant (§6.4) has not moved yet → enter in BTC's direction on the follower. Exit when the residual gap closes or BTC's move fails | BTC-driven trends |

### 6.2 Strategy rules
- Parameters live in versioned YAML files. Every signal records the strategy version and parameter hash.
- No strategy is enabled for live signals until it passes the validation gates in §8.4.
- Ship strategies with **conservative defaults**, and only tune them through walk-forward optimisation,
  never in-sample.

### 6.3 Trend Catcher (all active coins, all timeframes)

**Purpose.** Detect the start of an up or down trend on every active coin as early as possible, show
it on the chart, and track it until it ends, so large moves can be joined near their beginning
instead of chased.

**Trade-off to be explicit about.** Detecting earlier means more false starts; detecting later means
less of the move is left. The detector exposes a **sensitivity** setting (Early / Balanced /
Conservative) and the UI shows the measured trade-off for each setting (see "Validation" below).

#### 6.3.1 Coverage
- Runs for every symbol in the active universe (watchlist + scanner universe, default top 100 USDT
  pairs by volume) on 1m, 5m, 15m, 1h and 4h.
- Updates on every bar close per timeframe. An intrabar "provisional" state is allowed for display
  only; signals use closed bars.
- Budget: 100 symbols × 5 timeframes must update in < 200 ms total per 1m close on a 4-core machine.

#### 6.3.2 Evidence used (each normalised to −1…+1, positive = up)
| Group | Evidence | Why it catches trends early |
|---|---|---|
| Structure | Break of structure: close above the last swing high after a higher low (mirror for down); Donchian 20/55 breakout | Price structure changes before averages turn |
| Adaptive slope | Kaufman Efficiency Ratio (trend vs. noise), KAMA slope, Kalman-filter slope and its t-statistic, linear-regression slope t-stat over 20/50 bars | Separates real drift from noise with less lag than plain EMAs |
| Moving-average ribbon | EMA 8/13/21/34/55 order and spread expansion; price vs. EMA 200 on 1h | Ribbon "fanning out" marks trend acceleration |
| Trend strength | ADX rising through 20→25 with DI+/DI− cross; Supertrend flip | Classic confirmation, used as a later-stage input |
| Change-point | CUSUM / Bayesian online change-point on returns; volatility breakout from a squeeze (§5.3) | Statistical "something changed" alarm, often the earliest input |
| Participation | RVOL > 1.5, CVD slope in the same direction, taker buy/sell ratio, bid/ask depth shift | Real trends usually come with volume and aggressor flow |
| Derivatives | ΔOI in the trend direction (new positions, not just short covering), funding not yet extreme | Fresh positioning supports continuation |
| Market | BTC/ETH trend state and the coin's coupling to BTC (§6.4); sector trend | Followers rarely sustain a trend against BTC |

#### 6.3.3 Trend score and state machine
- `trend_score` in −100…+100 per symbol/timeframe = weighted evidence sum. Weights are learned with
  walk-forward ML (same meta-labelling approach as §7.3), with sensible hand-set defaults.
- States (separately for up and down):

```
NONE ──► EARLY ──► CONFIRMED ──► MATURE ──► EXHAUSTING ──► ENDED
            └───────── false start (invalidated) ──────────► NONE
```

| State | Default entry rule (Balanced) |
|---|---|
| `EARLY` | Change-point or structure break **plus** ≥ 2 other groups agreeing; |score| ≥ 35 |
| `CONFIRMED` | |score| ≥ 55 for 2 closed bars, higher timeframe not opposing, volume confirmation |
| `MATURE` | Move ≥ 3 × ATR from origin, ADX > 30 |
| `EXHAUSTING` | Any 2 of: RSI/CVD divergence, climax volume bar with long wick, funding extreme, price > 3 ATR from EMA 21, OI falling while price still rises |
| `ENDED` | Trailing stop hit (Chandelier 3×ATR or Supertrend), or opposite `CONFIRMED` |
| Invalidated | Price back beyond the trend origin (the swing point that started it) |

- **Multi-timeframe alignment:** an "alignment" value counts how many of 1m/5m/15m/1h/4h agree.
  A 1m trend aligned with 15m and 1h is ranked far higher than an isolated 1m trend.
- For each trend the engine stores: origin price/time, detection price/time, lead (how far price had
  moved from origin when detected, in % and ATR), current extension, max favourable move, and end reason.

#### 6.3.4 Output per symbol/timeframe
```json
{"symbol": "SOLUSDT", "tf": "5m", "direction": "UP", "state": "CONFIRMED", "score": 68,
 "alignment": "4/5", "origin": {"ts": 1759479600000, "price": 142.10},
 "detected": {"ts": 1759479900000, "price": 143.05, "lead_atr": 0.9},
 "p_continue_2atr": 0.58, "trail_stop": 141.70, "btc_coupling": "FOLLOWER",
 "drivers": ["BOS above 142.9", "ER 0.62", "CVD rising", "OI +3.1%"]}
```

#### 6.3.5 Chart display
- **Background shading** of the price pane: green for up, red for down. Opacity reflects state
  (light = `EARLY`, solid = `CONFIRMED`/`MATURE`, hatched = `EXHAUSTING`). Toggleable.
- **Trend-start markers** at the origin bar (`T▲` / `T▼`) and a smaller marker at the detection bar,
  so the user can see the detection lag honestly.
- **Trailing-stop line** (Chandelier/Supertrend) drawn while the trend is active.
- **Auto trend lines / channel** from the swing points that define the trend.
- **MTF trend strip**: a thin row of coloured cells above the chart (1m · 5m · 15m · 1h · 4h) showing
  each timeframe's direction and state, plus the BTC state for comparison.
- **Trend Board** (new panel): grid of all active coins × timeframes, coloured by direction and
  strength, sortable by "new trends in the last N minutes", alignment, and score. Clicking a cell opens
  that chart.
- Alerts on `EARLY` (optional) and `CONFIRMED` (default) per coin, filtered by minimum alignment.

#### 6.3.6 Validation (reported in the UI per sensitivity setting)
- A "large move" is labelled historically as a move ≥ max(k × ATR(1h), X%) within horizon H
  (defaults: k = 3, X = 2%, H = 4h, configurable per timeframe).
- Metrics: **recall** (share of large moves detected), **precision** (share of `CONFIRMED` trends that
  became large moves), **false starts per coin per day**, **median lead** (share of the total move still
  ahead at detection time), and expectancy of S10 after costs.
- Target for v1 (Balanced, 5m, top-20 coins, out-of-sample): recall ≥ 60%, median ≥ 60% of the move
  remaining at `CONFIRMED`, S10 profit factor ≥ 1.3 after costs. These are goals to measure against,
  not promises.

### 6.4 BTC coupling (leader–follower analysis)

**Purpose.** Many altcoins move with BTC. The terminal measures this for every active coin, shows it,
and uses it in signals.

#### 6.4.1 Measurements (rolling, per coin vs. BTCUSDT, also vs. ETHUSDT)
| Metric | Method | Windows |
|---|---|---|
| Correlation ρ | Pearson and rank (Spearman) correlation of log returns | 1m returns over 4h and 24h; 5m returns over 3d |
| Beta β | OLS / robust regression of coin returns on BTC returns; R² | Same |
| Lead–lag | Cross-correlation of returns at lags −10…+10 bars on 1s/5s bars (Hayashi–Yoshida estimator for tick data optional); peak lag and its significance vs. a shuffled baseline | 1h, 4h |
| Residual return | `coin_ret − β × btc_ret`, cumulative over the session ("idiosyncratic move") | Session, 1h |
| Relative strength | Coin/BTC ratio trend (the `COINBTC` cross where listed, or synthetic) | 1h, 4h |
| Stability | How much ρ and β changed over the last 24h | 24h |

#### 6.4.2 Classification shown per coin
| Label | Rule (defaults) |
|---|---|
| `FOLLOWER` | ρ ≥ 0.7 and R² ≥ 0.5 |
| `PARTIAL` | 0.4 ≤ ρ < 0.7 |
| `INDEPENDENT` | ρ < 0.4, or the coin is in a strong own trend with large residual return |
| `LAGGER (≈ N s)` | Significant positive peak lag; shown as an extra tag on followers |
| `DECOUPLING` | ρ dropped by > 0.3 in the last few hours, or the residual exceeds 2σ (news or coin-specific flow) |

Honest note: on liquid majors the BTC lag is usually seconds or less and gets arbitraged quickly.
Measurable lags are more common on less liquid altcoins, where slippage is also higher. The S12
strategy must pass the same validation gates as every other strategy.

#### 6.4.3 How it is used
- **Chart header badge:** e.g. `BTC FOLLOWER · ρ 0.86 · β 1.4 · lag ≈ 20s`, colour-coded.
- **BTC overlay:** optional normalised BTC line on the coin's chart (and ETH), plus a **residual pane**
  showing the coin's own move after removing BTC.
- **Signal filter:** for `FOLLOWER` coins, long signals while BTC is in a confirmed downtrend (and the
  reverse) get a confluence penalty or veto (configurable).
- **Catch-up scanner:** when BTC enters `CONFIRMED` on 1m/5m, list followers ranked by β × expected
  move − move already made. Feeds S12.
- **Relative-strength alerts:** a coin trending up while BTC is flat or falling (`INDEPENDENT` /
  `DECOUPLING` with positive residual) is flagged as relative strength, which often comes before large
  coin-specific moves.
- **BTC shock alert:** BTC moves > k × σ in 1 minute → alert on all `FOLLOWER` positions and signals.

### 6.5 Pump & Dump Detector (early detection of abnormal moves)

**Purpose.** Catch large, fast moves (pumps and dumps) within their first seconds to minutes, across
**all** Binance USDT pairs, and either ride them with strict risk or stay out of the way.

**Realistic expectation.** A move can only be detected after it starts. The aim is to detect it in
the early part, enter only while enough of the move is plausibly left, and avoid buying the top. Many
pumps on small coins reverse violently, so position size and chase limits are part of the strategy.

#### 6.5.1 Two-tier market scanning
- **Tier 1 – whole market (all spot and futures USDT pairs):** lightweight streams — 1s klines or
  `aggTrade` for every pair, `!miniTicker@arr`, `!markPrice@arr@1s`, `!forceOrder@arr` — spread over
  several connections within Binance's per-connection stream limits. Computes cheap anomaly features.
- **Tier 2 – escalated symbols:** when a symbol's tier-1 score crosses the watch threshold, subscribe
  automatically to its full depth, `bookTicker`, and futures OI polling, and load its chart in the
  radar. Escalation must complete in < 2 s.

#### 6.5.2 Features (robust z-scores against a time-of-day-adjusted baseline using median/MAD)
| Group | Features |
|---|---|
| Price velocity | Return over 5s / 15s / 60s / 5m divided by realised volatility; acceleration (velocity change) |
| Volume | Volume and trade-count z-scores at 5s/1m vs. the same hour's baseline; RVOL |
| Aggressor flow | Taker buy share, delta and CVD burst, clustering of large trades (> p99 size) |
| Order book | Ask-side depletion (levels eaten or pulled), bid walls stepping up, OBI, spread change, book thinness ahead of price |
| Derivatives | OI surge, funding jump, **short liquidations** (squeeze) or long liquidations (dump cascade), perp-vs-spot basis jump |
| Origin | Which market leads: spot-led moves tend to be more sustained than perp-led ones; measured, not assumed |
| Catalysts | Binance listing/delisting/monitoring announcements, news impact score (§9), spike in news/social mention count |
| Context | Market cap / liquidity tier, Binance Monitoring or Seed tag, BTC state (is the whole market moving?), sector peers moving together |

**Pre-move signs (lower confidence, watchlist only):** volume creeping up while price stays flat,
OI rising on flat price, tightening range, repeated absorption of sells at a level, bid walls moving up.
These put a coin on `WATCH`; they never produce a trade signal on their own.

#### 6.5.3 Event stages
```
WATCH ──► IGNITION ──► CONFIRMED ──► EXHAUSTION ──► REVERSAL / FADE
   └──────── fizzle (no follow-through within T) ──────► NONE
```
| Stage | Default rule |
|---|---|
| `WATCH` | Pre-move signs, or a single feature group at z > 3 |
| `IGNITION` | ≥ 3 feature groups at z > 3 within 30 s, price velocity z > 4, move ≥ max(1%, 3σ) |
| `CONFIRMED` | Follow-through after 60 s: price holds above (below) the ignition midpoint, flow still one-sided, volume still elevated |
| `EXHAUSTION` | Any 2 of: climax volume with long wick, delta divergence, ask (bid) wall reloading and holding, funding spike, OI dropping as price keeps going (squeeze ending), move > p90 of historical pumps for that coin's tier |
| `REVERSAL` | Break back below the move's VWAP (above, for dumps) with opposite flow |

Each event is also classified as:
- **Broad / organic:** BTC or sector moving too, or a verified news catalyst, decent liquidity.
- **Isolated / suspicious:** small cap, thin book, no catalyst, perp-led with extreme funding. Shown
  with a warning badge; these tend to reverse sharply.

#### 6.5.4 Trading rules (S11)
- **Momentum follow (default):** enter on `IGNITION` (aggressive mode) or `CONFIRMED` (default) in the
  move's direction. Stop below the ignition bar low or the move's VWAP, whichever is tighter but still
  outside noise. Trail with 1s/5s structure or a fast ATR trail. Take partial profit on the first
  `EXHAUSTION` sign.
- **Chase limit:** no entry if price is already more than k × ATR (default 4 × 1m ATR) or more than
  p60 of the typical pump size for that liquidity tier away from the ignition origin.
- **Size:** half the normal risk by default; size also capped by estimated slippage from the live book.
- **Dumps:** short only on futures (spot users get an alert and a "protect positions" prompt).
- **Fade (opt-in, off by default):** counter-trade only after `EXHAUSTION` + `REVERSAL` confirmation,
  with a stop beyond the extreme.
- **Filters:** minimum 24h quote volume and depth; skip pairs with very new listings for the first N
  minutes unless the user enables listing mode; respect the §7.1 vetoes.

#### 6.5.5 Model and validation
- Historical event labelling: |return| ≥ max(X%, k·σ) within H minutes (defaults: 3%, 5σ, 15 min),
  separately for pumps and dumps, from data.binance.vision trades and recorded data.
- A LightGBM model scores, at each stage, `P(continuation ≥ y% more)` and `P(reversal before +y%)`,
  calibrated as in §7.3.
- Reported metrics: precision and recall of `IGNITION` and `CONFIRMED`, median detection delay,
  share of the move captured, false alarms per day (whole market), S11 expectancy after realistic
  slippage. Same promotion gates as §8.4.

#### 6.5.6 Display
- **Pump/Dump Radar panel:** live list of events with stage, direction, % move, time since ignition,
  volume ×, OI change, liquidations, catalyst link, organic/suspicious badge. Sorted by stage and
  score; click opens the chart.
- **Chart markers:** `🚀` at ignition of a pump, `💥` at ignition of a dump, shaded event zone,
  ignition origin line, and the chase-limit line (beyond it the UI shows "too late").
- **Alerts:** highest priority (distinct sound, Telegram with chart snapshot) on `IGNITION` and
  `CONFIRMED` for symbols passing the liquidity filter; `EXHAUSTION` alert for anyone holding the coin.

---

## 7. Signal engine: getting from candidates to accurate signals

```
candidates (S1..S12) ─► hard filters ─► confluence score ─► ML meta-model ─► calibration ─► EV & risk check ─► SIGNAL
```

### 7.1 Hard filters (veto layer)
Reject a candidate if any of these is true (each threshold is configurable):
- Spread > p90 of its rolling distribution, or > X bps.
- Top-of-book depth within ±10 bps < minimum notional (slippage risk).
- High-impact macro event within −15/+30 min (CPI, FOMC, NFP… see §9.3).
- Coin-specific critical news in the last N min (listing, delisting, hack, unlock, exploit).
- Data feed stale (> 2s since last update) or order book out of sync.
- Daily loss limit or max-trades limit reached (see §11).
- Target distance < 3 × round-trip cost (fees + expected slippage).
- Strategy marked as degraded by the live monitor (§8.5).
- Coin is a BTC `FOLLOWER` and the signal opposes BTC's confirmed trend on the filter timeframe (§6.4.3; veto or penalty, configurable).

### 7.2 Confluence score (0–100, explainable)
Weighted sum of normalised evidence from **independent families**. Weights are learned or set by the
user, and each contribution is shown in the UI.

| Family | Example features | Default weight |
|---|---|---|
| Higher-TF trend alignment | Trend Catcher state/score and MTF alignment (§6.3), BTC coupling and BTC trend (§6.4) | 20 |
| Momentum | RSI/StochRSI state, MACD histogram slope, divergences | 10 |
| Volume & flow | Delta, CVD slope/divergence, RVOL, footprint imbalances, absorption | 25 |
| Order book | OBI, microprice skew, walls, book pressure change | 15 |
| Location / structure | Distance to VWAP bands, POC/VAH/VAL, S/R, sweep of liquidity | 15 |
| Derivatives | Funding, ΔOI, L/S ratio, liquidations | 10 |
| Sentiment / context | News sentiment, BTC regime, Fear & Greed | 5 |

Correlated features from the same family are capped so that five oscillators do not count as five
independent confirmations.

### 7.3 ML meta-labelling (primary accuracy booster)
- **Labels.** Triple-barrier method: for each historical candidate, label 1 if TP is hit before SL
  within the time stop, otherwise 0. Use the actual strategy SL/TP and include fees and slippage.
- **Model.** Gradient boosting (LightGBM) per strategy family, trained on candidate-time features
  (all §5 features, regime, time-of-day, spread, volatility, news state). The output is `P(win)`.
- **Validation.** Purged and embargoed time-series K-fold cross-validation plus walk-forward
  retraining (for example, train on 90 days, test on the next 7, roll forward). Never use random splits.
- **Calibration.** Isotonic or Platt scaling on held-out folds. Report the Brier score and a reliability
  diagram, and show the diagram in the UI's strategy page.
- **Explainability.** SHAP values per signal; show the top 5 drivers in the signal card.
- **Retraining.** Scheduled weekly, plus on drift detection (PSI on features, or rolling calibration
  error above threshold). A new model is promoted only if it beats the current one out-of-sample.

### 7.4 Regime detection
- Classify each symbol and timeframe as **trending up / trending down / ranging / high-vol chaotic /
  illiquid**, using ADX, ATR percentile, Hurst exponent or variance ratio, and BB width. An optional
  Hidden Markov Model on returns and volatility can be added.
- Each strategy declares the regimes it is allowed in. The meta-model also receives the regime as a feature.

### 7.5 Final decision and output
A signal is published when:
`calibrated P(win) ≥ threshold_strategy` **and** `EV_after_costs > 0` **and** `confluence ≥ min_score`
**and** no veto applies.

Signal object:

```json
{
  "id": "sig_01H...", "ts": 1759480000123, "symbol": "BTCUSDT", "market": "futures",
  "side": "LONG", "strategy": "S1_trend_pullback@1.3.0", "timeframe": "1m",
  "entry": {"type": "limit", "price": 61234.5, "valid_until": 1759480060000},
  "stop": 61180.0, "targets": [{"price": 61320.0, "size_pct": 50}, {"price": 61400.0, "size_pct": 50}],
  "time_stop_bars": 15,
  "p_win": 0.61, "p_win_ci": [0.56, 0.66], "expected_R": 0.34, "confluence": 78,
  "regime": "trend_up",
  "reasons": ["15m trend up", "pullback to VWAP", "CVD bullish divergence", "OBI +0.31"],
  "risks": ["funding elevated +0.04%", "BTC 1h at resistance"],
  "model_version": "lgbm_s1_2026-09-28", "params_hash": "a1b2c3"
}
```

Signal lifecycle: `pending → triggered → (tp1_hit | stopped | time_stopped | invalidated | expired)`.
Every outcome is stored and feeds the live performance tracker.

---

## 8. Backtesting and validation

### 8.1 Engine
- **Event-driven** replay of recorded trades and book data for order-flow strategies, plus a
  vectorised fast mode on bars for quick parameter sweeps.
- The same strategy code runs in backtest, paper trading, and live (one code path, no reimplementation).

### 8.2 Realism requirements
- Fees by account tier and market (configurable maker/taker, BNB discount toggle). Futures funding
  payments applied at funding timestamps.
- Slippage: from the recorded order book (walk the book for market orders), or from a model
  `k × spread + impact(size/depth)` when no book is available.
- Limit-order fills: filled only if price trades **through** the limit, or touches it and queue
  position is estimated as cleared. Missed fills are counted.
- Latency: configurable signal-to-order delay (default 150 ms) applied in replay.
- No look-ahead: features are computed only from data available at decision time, verified by a
  "shift test" in CI (§14).
- Exchange filters (tick size, lot size, min notional) enforced.

### 8.3 Methodology
- Walk-forward optimisation (anchored and rolling); parameter stability heatmaps.
- Purged/embargoed cross-validation for ML; **deflated Sharpe ratio** and probability of backtest
  overfitting (PBO / CSCV) reported when many variants are tried.
- Monte Carlo trade-order reshuffling and bootstrap confidence intervals for every metric.
- Tests across symbols (majors and mid-caps) and across market regimes (bull, bear, chop).

### 8.4 Promotion gates (backtest → paper → live signals → auto-execution)

| Gate | Requirement (defaults, configurable) |
|---|---|
| Backtest out-of-sample | ≥ 300 trades, profit factor ≥ 1.3 after costs, expectancy > 0.1R, max DD < 15R, positive across ≥ 70% of walk-forward windows |
| Calibration | Brier score better than the base rate; expected calibration error < 5% |
| Paper trading | ≥ 2 weeks and ≥ 100 signals; results within the backtest's 90% confidence interval |
| Live signals → auto-exec | ≥ 4 weeks of live signals meeting the same criteria, plus explicit user opt-in |

### 8.5 Live performance monitor
- Rolling per strategy/symbol/regime: win rate, profit factor, expectancy (R), average MAE/MFE,
  calibration curve, slippage vs. modelled slippage.
- **Auto-degrade:** if rolling 50-signal expectancy falls below the lower bound of the backtest
  confidence interval, or calibration error exceeds 10%, the strategy is set to *shadow mode*
  (signals still computed and logged, but not shown or executed) and the user is notified.

### 8.6 Reports
HTML report per run: equity curve in R and in quote currency, drawdown, trade list, distribution of R,
metrics by hour/session/regime, calibration plot, parameter sensitivity.

---

## 9. News, events, and sentiment (free sources)

### 9.1 Sources
| Source | Type | Notes |
|---|---|---|
| Binance announcements (new listings, delistings, maintenance, Launchpool) | Polling of the public announcement page/feed | Highest impact for altcoins; poll every 15–30s, respecting robots/ToS |
| Binance Square / official X accounts | Optional | Only via free, ToS-compliant access |
| CryptoPanic | Aggregator API (free tier with API key) | Coin tags plus community votes |
| RSS: CoinDesk, Cointelegraph, The Block, Decrypt, Bitcoin Magazine, Blockworks | RSS | Free |
| Reddit (r/CryptoCurrency, coin subreddits) | Free API (rate-limited) | Sentiment volume |
| Economic calendar (CPI, FOMC, NFP, PCE, GDP, rate decisions) | Free public calendar JSON/RSS feeds; manual CSV import as fallback | Drives the macro veto |
| Token unlocks / major on-chain events | Free public calendars where available; manual CSV fallback | Supply shock flag |
| Fear & Greed Index | alternative.me API | Daily |
| CoinGecko | Free API (demo key) | Market cap, dominance, trending coins |

All sources are pluggable adapters with their own rate limits and health checks. If a source fails,
the terminal shows a "news degraded" badge and does not stop working.

### 9.2 Processing pipeline
1. **Fetch and deduplicate** (URL canonicalisation plus near-duplicate detection by title similarity / MinHash).
2. **Entity tagging**: map to symbols using ticker/name dictionaries built from `exchangeInfo` and
   CoinGecko, with disambiguation rules (for example, "SOL" vs. "sol" in normal text).
3. **Classification**: event type (listing, delisting, hack/exploit, regulation, ETF, partnership,
   unlock, macro, outage, rumour).
4. **Sentiment**: local transformer model (FinBERT or a crypto-tuned BERT) producing a score in −1…+1
   with confidence. Lexicon fallback (VADER plus a crypto lexicon) if no model is loaded.
5. **Impact score** (0–100) = source credibility × event-type weight × sentiment magnitude × novelty ×
   coin liquidity factor. Impact weights are **calibrated empirically** by measuring historical price
   reaction (abnormal return and volatility in the following 1–60 min) per event type.
6. **Actions**: show in the feed, add chart markers, feed sentiment into features, trigger vetoes,
   send alerts above a threshold.

### 9.3 Event-based rules (defaults)
- Macro high-impact: block new entries from −15 min to +30 min around release time; widen stops on open positions only if the user opts in.
- Binance delisting/monitoring-tag announcement for a coin: block longs on that coin for 24h and alert immediately.
- Hack/exploit with confidence > 0.7: block longs on the affected coin; alert.
- Listing announcement: alert only. Listing pumps are extremely volatile, so no automatic signals by default.

---

## 10. User interface

### 10.1 Layout (dockable, savable workspaces)

```
┌──────────────┬─────────────────────────────────────────────┬───────────────────┐
│ Watchlist &  │  Main chart (multi-pane)                    │ DOM / depth       │
│ Scanner      │  candles + signals + VWAP/bands + profile   │ ladder            │
│ (ranked by   │  ───────────────────────────────────────    │ (click to trade)  │
│ opportunity) │  CVD / delta pane                           ├───────────────────┤
│              │  OI / funding / liquidations pane           │ Time & sales      │
├──────────────┼─────────────────────────────────────────────┼───────────────────┤
│ Signal panel │  Liquidity heatmap / footprint (tab)        │ News & events     │
│ (active +    ├─────────────────────────────────────────────┤ feed (filtered    │
│ history)     │  Positions · Orders · Fills · P&L · Journal │ to symbol)        │
├──────────────┴──────────────────────┬──────────────────────┴───────────────────┤
│ Trend Board (coins × TFs, §6.3.5)   │ Pump/Dump Radar (§6.5.6)                 │
└─────────────────────────────────────┴──────────────────────────────────────────┘
```

The main chart header always shows the MTF trend strip and the BTC coupling badge (§6.3.5, §6.4.3).

### 10.2 Chart requirements
- Candles, Heikin-Ashi, line, range, and footprint modes. Synced crosshair across panes and linked charts.
- **Signal markers:** ▲ buy / ▼ sell arrows on the bar where the signal triggered. Colour intensity
  reflects `P(win)`. Hover shows the signal card (probability, reasons, risks, R:R). Entry, SL, and TP
  lines are drawn as draggable price lines when the signal is acted on.
- Historical signals and their outcomes (✓ TP / ✗ SL / ⏱ time stop) can be shown, so the user can
  see visually how the strategy behaved.
- News markers (📰 icon coloured by sentiment) and macro event vertical lines.
- Trend Catcher shading, `T▲`/`T▼` markers, trailing-stop line and auto trend channel (§6.3.5).
- Pump/dump `🚀`/`💥` markers, event zone and chase-limit line (§6.5.6).
- Optional normalised BTC/ETH overlay and residual (coin minus β × BTC) pane (§6.4.3).
- Overlays: any §5 indicator, volume profile (visible range / session), VWAP bands, S/R zones,
  liquidity pools, estimated liquidation levels, previous day high/low.
- **Liquidity heatmap**: order book depth over time (price × time × size), WebGL rendered, plus
  large-trade bubbles.
- Drawing tools: horizontal/trend lines, rectangles, fib retracement, measure tool, alerts on drawings.
- Performance: 60 fps panning with 50k bars loaded; new tick to chart update < 50 ms.

### 10.3 Scanner
- Ranks all watched pairs every N seconds by: active signal quality, RVOL, volatility percentile,
  spread/depth quality, momentum, news impact, new Trend Catcher `CONFIRMED` events with MTF
  alignment, active pump/dump events, and BTC catch-up candidates.
- Default universe: top-N USDT pairs by 24h volume, excluding pairs flagged as illiquid or under monitoring.

### 10.4 Trading panel
- Order types: market, limit, post-only, stop-market, stop-limit, OCO (spot), bracket (entry + SL + TP)
  implemented client-side for futures when no native bracket is available, plus reduce-only and close-all.
- **Risk-based sizing:** user sets risk per trade (for example, 0.5% of equity). Size = risk ÷ distance
  to stop, rounded to the lot size. Leverage is shown, not chosen directly.
- Hotkeys (configurable): buy/sell market, buy/sell at bid/ask, flatten, cancel all, move SL to
  break-even, take partial profit. Confirmation dialogs can be enabled per action.
- "Take signal" button fills the order ticket from the signal with one click; user confirms.

### 10.5 Journal and analytics
- Automatic trade journal: screenshot of the chart at entry/exit, signal ID, notes, tags, and emotion tags.
- Stats by strategy, symbol, hour, weekday, and setup; manual vs. signal-following comparison.

### 10.6 Alerts
- Channels: in-app sound, desktop/web push, Telegram bot, Discord webhook.
- Alert types: new signal (above probability threshold), price/indicator conditions, drawing crosses,
  news above impact threshold, risk events (daily loss limit near, liquidation price near), system
  health (feed disconnect).
- Telegram message includes symbol, side, entry/SL/TP, P(win), R:R, top reasons, chart snapshot, and
  a deep link to the terminal.

---

## 11. Execution and risk management

### 11.1 Binance account integration
- API key with **trading only — withdrawals must be disabled**. Enforced at startup by reading the API
  key permissions; the terminal refuses to run with a withdrawal-enabled key.
- IP whitelisting recommended and documented. Ed25519 or HMAC keys supported.
- Keys stored encrypted at rest (OS keychain, or a libsodium-encrypted file with a master password),
  never logged, never sent to the frontend.
- User data stream (via the current Binance mechanism) for real-time order/fill/balance/position updates,
  plus periodic REST reconciliation.
- Testnet support: Spot testnet and Futures testnet, selectable per profile.

### 11.2 Order management
- Idempotent `newClientOrderId` on every order. Retry only safe operations; reconcile after timeouts
  before re-sending.
- Handle all exchange filters before sending (PRICE_FILTER, LOT_SIZE, MIN_NOTIONAL, PERCENT_PRICE).
- Track order rate limits (orders per 10s / per day) in the rate-limit governor.
- Futures: one-way and hedge mode, margin type (isolated recommended), leverage per symbol.

### 11.3 Risk engine (pre-trade checks, cannot be bypassed from the UI)
| Rule | Default |
|---|---|
| Max risk per trade | 0.5% of equity |
| Max daily loss | 2% of equity → trading locked until the next UTC day |
| Max consecutive losses | 4 → 30 min cooldown |
| Max open positions | 3; max 1 per symbol |
| Max leverage (futures) | 5× effective |
| Max notional per order | Configurable |
| Mandatory stop loss | Yes. Orders without SL are rejected (can be disabled only for spot manual trades) |
| Liquidation buffer | Stop must sit well inside the liquidation price (≥ 50% of the distance) |
| Kill switch | Hotkey and UI button: cancel all orders and flatten all positions |

### 11.4 Auto-execution (optional, off by default)
- Enabled per strategy, only after passing the §8.4 gates, with a separate risk budget.
- Dead-man switch: if the backend loses market data or the exchange connection for > N seconds,
  cancel resting entries. Exchange-side stops protect open positions.
- Every automated action is logged with full context and is reproducible.

---

## 12. Non-functional requirements

| Area | Requirement |
|---|---|
| Latency (local host, excluding network) | Tick → indicators → signal < 20 ms p99 for 20 symbols; UI push < 50 ms |
| Throughput | 50 symbols × (aggTrade + depth@100ms + bookTicker) on a 4-core machine with < 50% CPU |
| Reliability | Automatic reconnect < 3 s; zero silent gaps (every gap detected, logged, and backfilled or flagged) |
| Correctness | Local order book matches REST snapshot checksum/spot checks 100% after resync |
| Time | All timestamps in UTC ms; clock offset tracked; alarms if offset > 500 ms |
| Security | No withdrawal keys; encrypted secrets; local-only UI binding by default (127.0.0.1); auth token if exposed |
| Observability | Structured logs (JSON), Prometheus metrics, Grafana dashboard (free): feed lag, gap count, signal count, latency histograms |
| Portability | Runs on Linux/macOS/Windows via Docker; minimum 4 GB RAM (8 GB with NLP model) |
| Cost | $0 software and data. Optional: VPS (free tier possible), Binance trading fees |

---

## 13. Data model (core tables)

| Table | Key fields |
|---|---|
| `symbols` | symbol, market, base, quote, tick_size, step_size, min_notional, status |
| `candles_{tf}` | symbol, open_time, o, h, l, c, v, quote_v, trades, taker_buy_v, delta |
| `trades` (Parquet) | symbol, agg_id, ts, price, qty, is_buyer_maker |
| `book_snapshots` (Parquet) | symbol, ts, bids[N], asks[N] |
| `derivs` | symbol, ts, funding, mark, index, oi, ls_ratio, taker_ratio |
| `liquidations` | symbol, ts, side, price, qty |
| `news` | id, ts, source, title, url, symbols[], event_type, sentiment, impact |
| `events` | id, ts, kind, country, importance, actual, forecast, previous |
| `signals` | full signal JSON, status, outcome, realised_R, mae, mfe |
| `orders` / `fills` / `positions` | standard execution records with signal_id link |
| `models` | model_id, strategy, trained_range, metrics, calibration, artifact_path |
| `trends` | id, symbol, tf, direction, state history, score, alignment, origin/detection ts+price, lead_atr, max_move, end_reason |
| `btc_coupling` | symbol, ts, window, rho, rank_rho, beta, r2, peak_lag, lag_significance, residual, label |
| `move_events` | id, symbol, market, direction, stage history, ignition ts/price, features snapshot, classification (organic/suspicious), catalyst_id, peak move, outcome |

---

## 14. Testing and quality

- **Indicator conformance:** golden-file tests against TA-Lib / `pandas-ta` for every indicator.
- **No-look-ahead test:** computing features on data truncated at time t must equal the live
  values at t for all t (bar and tick level).
- **Order book tests:** replay recorded diffs including injected gaps; verify resync.
- **Backtest determinism:** same input data and config give an identical trade list (hash check).
- **Execution tests:** run against Binance testnet in CI-optional jobs; mock exchange for unit tests,
  including partial fills, rejects, timeouts, and disconnects.
- **Paper vs. backtest parity:** run the same day in replay and paper mode; the trade lists must match
  within latency tolerance.
- **Load test:** synthetic 10× message rate for 10 minutes without lag growth.
- Coverage target ≥ 80% for core (indicators, signal engine, risk engine, order management).

---

## 15. Delivery roadmap

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 – Foundations** (2–3 wks) | Repo, CI, config, Binance WS/REST clients, local order book, candle builder, recorder, DuckDB storage | 24h recording run with zero undetected gaps |
| **P1 – Charting terminal** (3–4 wks) | React UI, Lightweight Charts, core indicators, DOM, time & sales, watchlist, workspaces | 60 fps chart; indicators pass conformance tests |
| **P2 – Order flow** (2–3 wks) | CVD, footprint, volume profile, heatmap, OBI, large trades, liquidations, derivatives panes | Visual check vs. reference tools; perf targets met |
| **P3 – Strategies & backtester** (4 wks) | S1–S9, event-driven and vector backtester, reports, walk-forward | Reproducible reports; no-look-ahead test green |
| **P3b – Detectors** (3–4 wks) | Trend Catcher, BTC coupling, market-wide tier-1/tier-2 scanner, Pump & Dump Detector, S10–S12, Trend Board, Radar, chart overlays | Detector metrics (§6.3.6, §6.5.5) reported out-of-sample; 100-symbol update budget met |
| **P4 – Signal intelligence** (3–4 wks) | Filters, confluence, regime, LightGBM meta-model, calibration, SHAP, live monitor | Calibration ECE < 5% out-of-sample on at least 2 strategies |
| **P5 – News & events** (2–3 wks) | Adapters, tagging, sentiment model, impact calibration, vetoes, markers | Event veto verified on historical CPI/FOMC days |
| **P6 – Execution & risk** (3 wks) | Order manager, risk engine, hotkeys, testnet, journal | Full testnet test suite green; kill switch < 1 s |
| **P7 – Alerts & hardening** (2 wks) | Telegram/Discord/push, observability, security review, docs | 2-week paper trading soak with no critical incidents |

---

## 16. Repository layout (proposed)

```
scalping/
├── backend/
│   ├── ingest/          # binance ws/rest clients, order book, recorder
│   ├── bars/            # candle & activity bar builders
│   ├── indicators/      # incremental indicator library
│   ├── features/        # feature store for signals/ML
│   ├── strategies/      # S1..S12 plug-ins + YAML params
│   ├── detectors/       # trend catcher, btc coupling, pump/dump detector + market-wide scanner
│   ├── signals/         # filters, confluence, regime, meta-model, calibration
│   ├── news/            # source adapters, NLP, impact scoring
│   ├── execution/       # order manager, risk engine, user data stream
│   ├── backtest/        # event-driven + vectorised engines, reports
│   ├── api/             # FastAPI REST + WS gateway
│   └── alerts/          # telegram, discord, push
├── frontend/            # React + TS + Lightweight Charts
├── research/            # notebooks, experiments
├── config/              # default.yaml, strategies/*.yaml, risk.yaml
├── data/                # (gitignored) parquet/duckdb
├── tests/
├── docker-compose.yml
└── docs/SPEC.md
```

---

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Overfitting makes backtests look great and live results poor | Walk-forward, purged CV, deflated Sharpe, PBO, paper gate, live monitor with auto-degrade |
| Fees and slippage cancel the edge | Cost-aware EV filter, min target ≥ 3× costs, maker-first entries, BNB fee discount option |
| Binance API changes or rate limits | Config-driven endpoints, rate governor, contract tests against testnet, quick-patch process |
| Regional availability / regulation (Binance is restricted in some countries) | Users must check local law; adapter layer allows other exchanges later |
| Free news APIs change terms or limits | Multiple redundant sources, RSS first, graceful degradation |
| Data gaps cause false signals | Gap detection, stale-data veto, book resync |
| Security of API keys | No withdrawal permission enforced, encrypted storage, local binding, IP whitelist |
| User over-trusts signals | Probabilities with confidence intervals, visible live track record, mandatory risk limits, clear disclaimer |

---

## 18. Acceptance criteria (v1 release)

1. Streams and charts at least 20 Binance Spot/Futures pairs in real time with order flow panes, within the latency targets in §12.
2. All §5 indicators pass conformance tests; signals never repaint (verified by the no-look-ahead test).
3. At least 3 strategies pass the §8.4 backtest and paper gates on at least 2 major pairs (e.g. BTCUSDT, ETHUSDT).
4. Every displayed signal shows a calibrated P(win), its confidence interval, expected R after costs, reasons, and the strategy's live track record.
5. News feed ingests at least 5 free sources, tags coins, scores sentiment and impact, and enforces macro/coin event vetoes.
6. Risk engine blocks orders that break any §11.3 rule; the kill switch flattens everything in under 1 second on testnet.
7. Alerts reach Telegram within 2 seconds of signal creation.
8. Trend Catcher state, markers and MTF strip are shown for every active coin; the Trend Board covers at least 100 coins × 5 timeframes; its recall, precision, false-start rate and median lead are reported per sensitivity setting.
9. Every active coin shows its BTC coupling label (ρ, β, lag); follower signals against BTC's trend are filtered.
10. The Pump & Dump Detector scans all Binance USDT spot and futures pairs, escalates candidates in < 2 s, shows them in the radar and on the chart, and its detection delay and precision are measured on historical events.
11. The whole system runs with zero paid software or data subscriptions.

---

## 19. Disclaimer (shown in-app on first run)

> This software provides analytical tools and probabilistic signals. It is not financial advice.
> Past and simulated performance does not guarantee future results. Crypto trading, especially
> leveraged scalping, carries a high risk of loss, including losses larger than your deposit on
> futures. Use testnet and paper trading first, risk only money you can afford to lose, and make
> sure using Binance is legal where you live.
