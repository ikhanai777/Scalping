"""Async Binance REST client for public market data, with a request-weight governor (spec §4.2)."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)


class WeightGovernor:
    def __init__(self, limit_per_min: int = 6000, safety: float = 0.7):
        self.limit = limit_per_min * safety
        self.used = 0
        self.minute = int(time.time() // 60)
        self.banned_until = 0.0

    def observe(self, headers: httpx.Headers) -> None:
        for k, v in headers.items():
            if k.lower().startswith("x-mbx-used-weight-1m"):
                try:
                    self.used = int(v)
                    self.minute = int(time.time() // 60)
                except ValueError:
                    pass

    async def acquire(self, weight: int) -> None:
        while True:
            now = time.time()
            if now < self.banned_until:
                await asyncio.sleep(self.banned_until - now)
                continue
            m = int(now // 60)
            if m != self.minute:
                self.minute, self.used = m, 0
            if self.used + weight <= self.limit:
                self.used += weight
                return
            await asyncio.sleep(60 - now % 60 + 0.2)


class BinanceREST:
    def __init__(self, base: str, futures: bool = False, weight_limit: int = 6000, safety: float = 0.7,
                 client: httpx.AsyncClient | None = None):
        self.base = base.rstrip("/")
        self.futures = futures
        self.gov = WeightGovernor(2400 if futures else weight_limit, safety)
        self.cli = client or httpx.AsyncClient(base_url=self.base, timeout=15)
        self.sem = asyncio.Semaphore(8)
        self.p = "/fapi/v1" if futures else "/api/v3"

    async def close(self) -> None:
        await self.cli.aclose()

    async def get(self, path: str, params: dict | None = None, weight: int = 1):
        await self.gov.acquire(weight)
        async with self.sem:
            for attempt in range(4):
                try:
                    r = await self.cli.get(path, params=params)
                except httpx.HTTPError as e:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(2 ** attempt)
                    continue
                self.gov.observe(r.headers)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", "10"))
                    log.warning("rate limited (429), backing off %ss", wait)
                    await asyncio.sleep(wait)
                    continue
                if r.status_code == 418:
                    wait = int(r.headers.get("Retry-After", "120"))
                    self.gov.banned_until = time.time() + wait
                    log.error("IP banned (418) for %ss — stopping requests", wait)
                    raise RuntimeError(f"Binance IP ban for {wait}s")
                r.raise_for_status()
                return r.json()
        raise RuntimeError(f"GET {path} failed")

    # ---- market data ---------------------------------------------------------------------
    async def server_time(self) -> int:
        return (await self.get(f"{self.p}/time"))["serverTime"]

    async def exchange_info(self) -> dict:
        return await self.get(f"{self.p}/exchangeInfo", weight=20 if not self.futures else 1)

    async def tickers_24h(self) -> list[dict]:
        return await self.get(f"{self.p}/ticker/24hr", weight=80 if not self.futures else 40)

    async def klines(self, symbol: str, interval: str, limit: int = 500, start: int | None = None,
                     end: int | None = None) -> list[list]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start:
            params["startTime"] = start
        if end:
            params["endTime"] = end
        w = 2 if not self.futures else (1 if limit < 100 else 2 if limit < 500 else 5)
        return await self.get(f"{self.p}/klines", params, weight=w)

    async def depth(self, symbol: str, limit: int = 1000) -> dict:
        w = (5 if limit <= 100 else 25 if limit <= 500 else 50) if not self.futures else (5 if limit <= 100 else 10 if limit <= 500 else 20)
        return await self.get(f"{self.p}/depth", {"symbol": symbol, "limit": limit}, weight=w)

    async def agg_trades(self, symbol: str, limit: int = 500, from_id: int | None = None) -> list[dict]:
        params = {"symbol": symbol, "limit": limit}
        if from_id is not None:
            params["fromId"] = from_id
        return await self.get(f"{self.p}/aggTrades", params, weight=4 if not self.futures else 20)

    # ---- futures-only --------------------------------------------------------------------
    async def premium_index(self) -> list[dict]:
        return await self.get("/fapi/v1/premiumIndex", weight=10)

    async def open_interest(self, symbol: str) -> dict:
        return await self.get("/fapi/v1/openInterest", {"symbol": symbol}, weight=1)

    async def open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 30) -> list[dict]:
        return await self.get("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit})

    async def long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 1) -> list[dict]:
        return await self.get("/futures/data/globalLongShortAccountRatio",
                              {"symbol": symbol, "period": period, "limit": limit})

    async def taker_ratio(self, symbol: str, period: str = "5m", limit: int = 1) -> list[dict]:
        return await self.get("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": period, "limit": limit})
