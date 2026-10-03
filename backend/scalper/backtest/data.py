"""Historical data: Binance REST klines (paged) and data.binance.vision bulk dumps, cached as Parquet.

pandas/pyarrow are optional: without them (e.g. inside the Android app) ``load_history`` falls back to
REST klines with a small JSON cache."""
from __future__ import annotations

import io
import json
import logging
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from ..models import TF_MS, Bar

log = logging.getLogger(__name__)
VISION = "https://data.binance.vision/data"
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
        "taker_buy_volume", "taker_buy_quote", "ignore"]


def bars_from_df(df) -> list[Bar]:
    return [Bar(int(r.open_time), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume),
                float(r.quote_volume), int(r.trades), float(r.taker_buy_volume), True)
            for r in df.itertuples(index=False)]


def fetch_klines_raw(symbol: str, interval: str, start_ms: int, end_ms: int,
                     base: str = "https://data-api.binance.vision", market: str = "spot") -> list[list]:
    """Paged REST klines as raw Binance arrays (no pandas)."""
    path = "/api/v3/klines" if market == "spot" else "/fapi/v1/klines"
    limit = 1000 if market == "spot" else 1500
    rows, cur = [], start_ms
    step = TF_MS[interval]
    with httpx.Client(base_url=base, timeout=20) as cli:
        while cur < end_ms:
            for attempt in range(5):
                r = cli.get(path, params={"symbol": symbol, "interval": interval, "startTime": cur,
                                          "endTime": end_ms, "limit": limit})
                if r.status_code in (418, 429):
                    time.sleep(int(r.headers.get("Retry-After", "5")))
                    continue
                r.raise_for_status()
                break
            batch = r.json()
            if not batch:
                break
            rows += batch
            cur = batch[-1][0] + step
            used = int(r.headers.get("x-mbx-used-weight-1m", "0") or 0)
            if used > 4000:
                time.sleep(10)
    return rows


def fetch_klines_rest(symbol: str, interval: str, start_ms: int, end_ms: int,
                      base: str = "https://data-api.binance.vision", market: str = "spot"):
    import pandas as pd
    return _typed(pd.DataFrame(fetch_klines_raw(symbol, interval, start_ms, end_ms, base, market), columns=COLS))


def _typed(df):
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume"):
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype("int64")
    # data.binance.vision switched spot timestamps to microseconds in 2025
    if df["open_time"].iloc[0] > 10 ** 14:
        df["open_time"] = df["open_time"] // 1000
    df["trades"] = df["trades"].astype(int)
    return df[["open_time", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume"]]


def fetch_vision_day(symbol: str, interval: str, day: date, market: str = "spot"):
    import pandas as pd
    seg = "spot" if market == "spot" else "futures/um"
    url = f"{VISION}/{seg}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{day.isoformat()}.zip"
    r = httpx.get(url, timeout=60)
    if r.status_code != 200:
        return None
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None)
    if not str(df.iloc[0, 0]).isdigit():        # some files have a header row
        df = df.iloc[1:]
    df.columns = COLS[: df.shape[1]]
    return _typed(df.astype({"open_time": "int64"}))


def load_history(symbol: str, interval: str, days: int, cache_dir: Path, market: str = "spot",
                 rest_base: str = "https://data-api.binance.vision", end: datetime | None = None) -> list[Bar]:
    """Load ``days`` of history ending now (UTC). Uses cached Parquet per day, data.binance.vision for
    complete past days and REST for today/missing days."""
    try:
        import pandas as pd
        import pyarrow  # noqa: F401  (Parquet cache)
    except ImportError:
        return load_history_light(symbol, interval, days, cache_dir, market, rest_base, end)
    cache = cache_dir / "klines" / market / symbol / interval
    cache.mkdir(parents=True, exist_ok=True)
    end = end or datetime.now(timezone.utc)
    today = end.date()
    frames = []
    for k in range(days, -1, -1):
        day = today - timedelta(days=k)
        f = cache / f"{day.isoformat()}.parquet"
        if f.exists() and day != today:
            frames.append(pd.read_parquet(f))
            continue
        df = None
        if day < today:
            try:
                df = fetch_vision_day(symbol, interval, day, market)
            except Exception:
                df = None
        if df is None or df.empty:
            start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
            stop = min(start + 86_400_000 - 1, int(end.timestamp() * 1000))
            try:
                df = fetch_klines_rest(symbol, interval, start, stop, rest_base, market)
            except httpx.HTTPError as e:      # e.g. region-blocked REST: keep the bulk-archive days
                log.warning("REST klines unavailable for %s %s %s (%s); skipping that day", symbol, interval, day, e)
                df = None
        if df is not None and not df.empty:
            if day != today:
                df.to_parquet(f)
            frames.append(df)
    if not frames:
        return []
    df = pd.concat(frames).drop_duplicates("open_time").sort_values("open_time")
    now_ms = int(end.timestamp() * 1000)
    df = df[df["open_time"] + TF_MS[interval] <= now_ms]          # closed bars only
    return bars_from_df(df)


def load_history_light(symbol: str, interval: str, days: int, cache_dir: Path, market: str = "spot",
                       rest_base: str = "https://data-api.binance.vision", end: datetime | None = None) -> list[Bar]:
    """REST-only loader without pandas, caching complete days as JSON (used on Android)."""
    cache = cache_dir / "klines-json" / market / symbol / interval
    cache.mkdir(parents=True, exist_ok=True)
    end = end or datetime.now(timezone.utc)
    today = end.date()
    now_ms = int(end.timestamp() * 1000)
    rows: dict[int, list] = {}
    for k in range(days, -1, -1):
        day = today - timedelta(days=k)
        f = cache / f"{day.isoformat()}.json"
        if f.exists() and day != today:
            batch = json.loads(f.read_text(encoding="utf-8"))
        else:
            start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
            try:
                batch = fetch_klines_raw(symbol, interval, start, min(start + 86_400_000 - 1, now_ms), rest_base, market)
            except httpx.HTTPError as e:
                log.warning("REST klines unavailable for %s %s %s (%s); skipping that day", symbol, interval, day, e)
                continue
            if day != today and batch:
                f.write_text(json.dumps(batch), encoding="utf-8")
        for r in batch:
            rows[int(r[0])] = r
    out = []
    for t in sorted(rows):
        if t + TF_MS[interval] <= now_ms:
            out.append(Bar.from_kline(rows[t]))
    return out
