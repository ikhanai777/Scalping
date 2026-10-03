"""Signed Binance order execution for testnet/live mode (spec §11.1–11.2).

EXPERIMENTAL: exercised only against mocks in this repo's tests. Always verify on Binance testnet
(execution.mode: testnet) before enabling live mode. Withdrawal-enabled keys are refused.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import time
import uuid
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

HOSTS = {
    ("spot", "testnet"): "https://testnet.binance.vision",
    ("spot", "live"): "https://api.binance.com",
    ("futures", "testnet"): "https://testnet.binancefuture.com",
    ("futures", "live"): "https://fapi.binance.com",
}


def sign(secret: str, params: dict) -> str:
    return hmac.new(secret.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()


def round_step(x: float, step: float) -> float:
    if not step:
        return x
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.floor(x / step + 1e-9) * step, decimals)


class SymbolFilters:
    def __init__(self, info: dict):
        self.tick = self.step = self.min_notional = 0.0
        for f in info.get("filters", []):
            t = f.get("filterType")
            if t == "PRICE_FILTER":
                self.tick = float(f["tickSize"])
            elif t == "LOT_SIZE":
                self.step = float(f["stepSize"])
            elif t in ("MIN_NOTIONAL", "NOTIONAL"):
                self.min_notional = float(f.get("minNotional") or f.get("notional") or 0)

    def price(self, p: float) -> float:
        return round_step(p, self.tick)

    def qty(self, q: float) -> float:
        return round_step(q, self.step)


class BinanceTrader:
    def __init__(self, market: str, mode: str, key_env: str = "BINANCE_API_KEY",
                 secret_env: str = "BINANCE_API_SECRET", client: httpx.AsyncClient | None = None):
        if mode not in ("testnet", "live"):
            raise ValueError("BinanceTrader is only for testnet/live modes")
        self.market, self.mode = market, mode
        self.key = os.environ.get(key_env, "")
        self.secret = os.environ.get(secret_env, "")
        if not self.key or not self.secret:
            raise RuntimeError(f"set {key_env} and {secret_env} environment variables")
        self.base = HOSTS[(market, mode)]
        self.cli = client or httpx.AsyncClient(base_url=self.base, timeout=10)
        self.offset = 0
        self.filters: dict[str, SymbolFilters] = {}
        self.p = "/api/v3" if market == "spot" else "/fapi/v1"

    async def _req(self, method: str, path: str, params: dict | None = None, signed: bool = True):
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.key}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.offset
            params["recvWindow"] = 5000
            params["signature"] = sign(self.secret, params)
        r = await self.cli.request(method, path, params=params, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"Binance {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    async def start(self) -> None:
        st = await self._req("GET", f"{self.p}/time", signed=False)
        self.offset = st["serverTime"] - int(time.time() * 1000)
        info = await self._req("GET", f"{self.p}/exchangeInfo", signed=False)
        self.filters = {s["symbol"]: SymbolFilters(s) for s in info["symbols"]}
        await self.check_permissions()

    async def check_permissions(self) -> None:
        """Refuse keys that can withdraw (spec §11.1). Testnet has no /sapi endpoints."""
        if self.mode == "testnet":
            return
        params = {"timestamp": int(time.time() * 1000) + self.offset, "recvWindow": 5000}
        params["signature"] = sign(self.secret, params)
        async with httpx.AsyncClient(base_url="https://api.binance.com", timeout=10) as cli:
            r = await cli.get("/sapi/v1/account/apiRestrictions", params=params, headers={"X-MBX-APIKEY": self.key})
        r.raise_for_status()
        rs = r.json()
        if rs.get("enableWithdrawals"):
            raise RuntimeError("API key has withdrawals enabled — refusing to trade. Disable withdrawals.")
        if self.market == "futures" and not rs.get("enableFutures"):
            raise RuntimeError("API key lacks futures permission")
        if self.market == "spot" and not rs.get("enableSpotAndMarginTrading"):
            raise RuntimeError("API key lacks spot trading permission")

    async def bracket(self, symbol: str, side: str, qty: float, stop: float, take_profit: float) -> dict:
        f = self.filters.get(symbol) or SymbolFilters({})
        qty = f.qty(qty)
        entry_side, exit_side = ("BUY", "SELL") if side == "LONG" else ("SELL", "BUY")
        cid = "sc_" + uuid.uuid4().hex[:16]
        if self.market == "spot":
            if side != "LONG":
                raise RuntimeError("spot cannot open shorts")
            entry = await self._req("POST", "/api/v3/order", {"symbol": symbol, "side": "BUY", "type": "MARKET",
                                                              "quantity": qty, "newClientOrderId": cid,
                                                              "newOrderRespType": "FULL"})
            filled = float(entry.get("executedQty", qty))
            oco = await self._req("POST", "/api/v3/orderList/oco", {
                "symbol": symbol, "side": "SELL", "quantity": f.qty(filled),
                "aboveType": "LIMIT_MAKER", "abovePrice": f.price(take_profit),
                "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": f.price(stop),
                "belowPrice": f.price(stop * 0.997), "belowTimeInForce": "GTC",
                "listClientOrderId": cid + "_oco"})
            return {"entry": entry, "exits": oco}
        entry = await self._req("POST", "/fapi/v1/order", {"symbol": symbol, "side": entry_side, "type": "MARKET",
                                                           "quantity": qty, "newClientOrderId": cid})
        sl = await self._req("POST", "/fapi/v1/order", {"symbol": symbol, "side": exit_side, "type": "STOP_MARKET",
                                                        "stopPrice": f.price(stop), "closePosition": "true",
                                                        "workingType": "MARK_PRICE", "newClientOrderId": cid + "_sl"})
        tp = await self._req("POST", "/fapi/v1/order", {"symbol": symbol, "side": exit_side,
                                                        "type": "TAKE_PROFIT_MARKET", "stopPrice": f.price(take_profit),
                                                        "closePosition": "true", "workingType": "MARK_PRICE",
                                                        "newClientOrderId": cid + "_tp"})
        return {"entry": entry, "exits": [sl, tp]}

    async def cancel_all(self, symbol: str) -> None:
        path = "/api/v3/openOrders" if self.market == "spot" else "/fapi/v1/allOpenOrders"
        await self._req("DELETE", path, {"symbol": symbol})

    async def flatten(self) -> list[dict]:
        """Kill switch: cancel everything and close all futures positions at market."""
        out = []
        if self.market == "futures":
            for p in await self._req("GET", "/fapi/v2/positionRisk"):
                amt = float(p["positionAmt"])
                if amt == 0:
                    continue
                await self.cancel_all(p["symbol"])
                out.append(await self._req("POST", "/fapi/v1/order", {
                    "symbol": p["symbol"], "side": "SELL" if amt > 0 else "BUY", "type": "MARKET",
                    "quantity": abs(amt), "reduceOnly": "true"}))
        else:
            for o in await self._req("GET", "/api/v3/openOrders"):
                await self.cancel_all(o["symbol"])
        return out

    async def account(self) -> dict:
        if self.market == "spot":
            a = await self._req("GET", "/api/v3/account")
            return {"balances": [b for b in a["balances"] if float(b["free"]) + float(b["locked"]) > 0]}
        return await self._req("GET", "/fapi/v2/account")
