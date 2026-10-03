# Android app

The Android app runs the **complete terminal on the phone**: the same Python engine as the desktop
version (Binance streams, indicators, Trend Catcher, BTC coupling, Pump & Dump radar, all strategies,
signal engine, news, paper broker) plus the web UI with a dedicated phone layout. It needs no PC
or server.

| | |
|---|---|
| APK | `releases/ScalpingTerminal-0.2.0-arm64.apk` (≈ 23 MB) |
| Android | 7.0+ (API 24), 64-bit ARM (arm64-v8a) — practically every phone since ~2017 |
| Permissions | Internet, notifications, foreground service |
| Data usage | ≈ 40 KB/s per 100 coins on the pump radar (default 60 coins); trend tracking and the chart add little |
| Trading | Paper only (simulated orders) |

## Install

1. Copy the APK to the phone, or open it from GitHub on the phone (`releases/` folder → the APK → "View raw"/download).
2. Tap it. Android asks to allow installing from this source (browser or file manager). Allow it once.
3. Open **Scalping Terminal**. Allow notifications if you want alerts.
4. The first start takes 1–2 minutes while the engine loads market history and runs a short backtest
   to seed strategy statistics. After that, a permanent "Scalping engine running" notification shows
   the engine is live.

The APK is signed with a **public development key** that is committed in the repo
(`android/app/dev-release.keystore`). Every build from this repo can therefore install over the
previous one. Anyone could sign an APK with that key, so only install builds from this repository.
For store distribution, sign with your own key (see "Build").

## Using it

- **Chart**: pick a timeframe, tap **Layers** to toggle overlays (EMA, VWAP bands, trend shading, trailing stop,
  channel, signals, trend markers, pump/dump, news, BTC overlay). Tap the trend card for details. Tapping the
  chart sets the stop price for the order ticket.
- **Trade** button: order book with walls, risk-sized paper order ticket (Buy/Sell), and the KILL switch.
- **Scanner**: all coins ranked by opportunity, with trend dots, BTC-coupling labels and pump badges. Tap a coin to chart it.
- **Signals / Radar**: live signal cards (P(win), EV, reasons, "Take (paper)") and pump/dump events
  (stage, move, volume ×, "too late beyond" price).
- **More**: Trend Board, news + economic calendar, positions, strategy statistics, BTC coupling, settings, engine log.
- **Settings**: coins tracked, radar size, trend sensitivity, spot/futures fees, paper risk settings,
  notifications, Telegram. **Save**, then **Restart engine** to apply. **Stop engine & exit** stops the
  background service.
- **Back** closes sheets and sub-screens. From the main screens it sends the app to the background, and the
  engine keeps running so alerts still arrive.

Notifications fire for validated signals with P(win) above the alert threshold, pump/dump IGNITION /
CONFIRMED / EXHAUSTION on liquid coins, CONFIRMED multi-timeframe trends (5m and higher, alignment ≥ 3),
and high-impact news.

### Battery

The engine holds live connections, so it uses battery and data while running. To reduce that:
lower "Coins on pump radar" and "Coins with trend tracking", or stop the engine from Settings when you
don't need it. Some phone brands (Xiaomi, Huawei, Samsung "deep sleep") kill background apps
aggressively. If alerts stop with the screen off, set the app's battery usage to "Unrestricted".

## How it works

```
┌──────────────────────── APK ────────────────────────┐
│ MainActivity (WebView) ── http://127.0.0.1:8765 ──┐ │
│                                                   │ │
│ EngineService (foreground) → Chaquopy Python 3.11 │ │
│   scalper.mobile.android_main()                   │ │
│     Engine + Starlette/uvicorn server ◄───────────┘ │
│     Notifier (Java) ◄── alerts                      │
└──────────────────────────────────────────────────────┘
            │ HTTPS / WSS
            ▼
   Binance public market data, news feeds
```

- The engine is pure Python apart from PyYAML. numpy was replaced by small pure-Python statistics
  in the two modules that used it, which removed ~13 MB from the APK. FastAPI/pydantic were replaced
  by Starlette, because pydantic-core has no Android build.
- Android uses the `mobile` config profile (`config/mobile.yaml`): fewer coins, a lighter radar,
  the background backtest in a thread (no multiprocessing on Android) and a pandas-free history loader.
- Settings saved in the app go to `local.yaml` in the app's private storage. Data (SQLite, stats,
  cached history, `engine.log`) lives there too.
- Cleartext HTTP is allowed only to 127.0.0.1 (`network_security_config.xml`). Everything on the
  internet is HTTPS/WSS.

## Build

Requirements: JDK 17+, Android SDK (platform 35, build-tools 35), Python **3.11** on the build machine
(Chaquopy needs a build Python matching the app's Python version), Node 20+.

```bash
cd frontend && npm ci && npm run build && cd ..
cd android
echo "sdk.dir=/path/to/android-sdk" > local.properties      # or set ANDROID_HOME
./gradlew assembleRelease
# → android/app/build/outputs/apk/release/app-release.apk
```

- `SCALPER_BUILD_PYTHON=/path/to/python3.11` selects the build Python if `python3.11` is not on PATH.
- Your own signing key: set `SCALPER_KEYSTORE`, `SCALPER_KEYSTORE_PASSWORD`, `SCALPER_KEY_ALIAS`, `SCALPER_KEY_PASSWORD`.
- Emulator build: add `"x86_64"` to `abiFilters` in `android/app/build.gradle.kts`.
- CI: `.github/workflows/android.yml` builds the APK on every relevant push and uploads it as an artifact
  (Actions tab → latest "Android APK" run → Artifacts).

Test the mobile profile on a desktop without a phone:

```bash
python -m scalper.mobile --dir /tmp/scalper-mobile     # then open http://127.0.0.1:8765/?mobile
```

## Testing status

- Built and signed in CI-like conditions. Lint-vital and R8 passed. 23 MB, arm64-v8a, minSdk 24, targetSdk 35.
- The Android entry point (`scalper.mobile.android_main`) was run from a background thread, as
  the service does, in a Python environment containing only the APK's packages (no numpy, pandas,
  pyarrow, LightGBM, FastAPI or pydantic), with the packaged config and UI. Engine live, background
  backtest done, UI, chart and radar served, clean stop.
- The phone UI was tested in Chromium at Pixel 7 size against live Binance data.
- **Not yet run on a physical device or emulator.** The build machine has no hardware virtualization.
  The Java layer (WebView, foreground service, notifications, restart) has been compiled and linted
  but not executed. If something fails on a device, the engine log (More → Engine log, or
  `adb logcat -s ScalpingEngine python.stderr python.stdout`) shows why.
