"""News, events and sentiment (spec §9): free sources, dedup, coin tagging, event typing, sentiment,
impact scoring, macro and coin-specific vetoes."""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher

import feedparser
import httpx

log = logging.getLogger(__name__)

# Common names for tagging beyond the bare ticker (tickers come from the live universe).
COIN_NAMES = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "BNB": ["binance coin", "bnb chain"],
    "XRP": ["ripple", "xrp"], "DOGE": ["dogecoin"], "ADA": ["cardano"], "AVAX": ["avalanche"],
    "TRX": ["tron"], "DOT": ["polkadot"], "LINK": ["chainlink"], "MATIC": ["polygon"], "POL": ["polygon"],
    "LTC": ["litecoin"], "SHIB": ["shiba inu"], "TON": ["toncoin"], "UNI": ["uniswap"], "ATOM": ["cosmos"],
    "NEAR": ["near protocol"], "APT": ["aptos"], "ARB": ["arbitrum"], "OP": ["optimism"], "SUI": ["sui network", "sui"],
    "PEPE": ["pepe"], "FIL": ["filecoin"], "INJ": ["injective"], "TIA": ["celestia"], "SEI": ["sei network"],
    "WIF": ["dogwifhat"], "RNDR": ["render"], "RENDER": ["render network"], "FET": ["fetch.ai"], "AAVE": ["aave"],
    "ETC": ["ethereum classic"], "BCH": ["bitcoin cash"], "XLM": ["stellar"], "HBAR": ["hedera"], "ENA": ["ethena"],
}
# Tickers that are ordinary words: require $TICKER, the coin name or uppercase-with-context.
AMBIGUOUS = {"ONE", "ALL", "FOR", "GAS", "HOT", "KEY", "LINK", "NEAR", "OP", "SUN", "TRUMP", "AI", "ID", "ACE",
             "BAT", "CAT", "COW", "DOG", "MOVE", "PEOPLE", "SAND", "SUPER", "TURBO", "WIN", "ZRO", "BOME", "SUI",
             "ME", "BLUR", "FUN", "GOAT", "PENGU", "S", "USUAL", "W", "HOOK", "MASK", "DENT", "CITY", "NOT", "PUMP"}

EVENT_RULES: list[tuple[str, float, list[str]]] = [
    ("delisting", 1.0, ["delist", "will remove", "cease trading", "monitoring tag"]),
    ("hack", 1.0, ["hack", "exploit", "drained", "stolen", "breach", "attacker", "rug pull", "rugpull"]),
    ("listing", 0.9, ["will list", "lists ", "listing", "launchpool", "launchpad", "new trading pair"]),
    ("regulation", 0.7, ["sec ", "lawsuit", "regulat", "ban ", "sanction", "court", "cftc", "doj"]),
    ("etf", 0.8, ["etf"]),
    ("unlock", 0.6, ["unlock", "vesting"]),
    ("macro", 0.7, ["fed ", "fomc", "cpi", "inflation", "rate cut", "rate hike", "powell", "jobs report", "nfp", "tariff"]),
    ("outage", 0.6, ["outage", "halted", "suspend", "maintenance", "downtime"]),
    ("partnership", 0.4, ["partner", "integrat", "collaborat", "adopt"]),
    ("rumour", 0.3, ["rumor", "rumour", "reportedly", "speculat"]),
]

CRYPTO_LEXICON = {
    "bullish": 2.5, "bearish": -2.5, "rally": 2.0, "surge": 2.2, "soar": 2.3, "pump": 1.5, "moon": 1.5,
    "ath": 2.0, "breakout": 1.5, "plunge": -2.5, "crash": -3.0, "dump": -2.0, "hack": -3.0, "exploit": -3.0,
    "delist": -2.5, "liquidated": -1.5, "liquidations": -1.0, "outflows": -1.0, "inflows": 1.0, "approval": 1.5,
    "approved": 1.8, "rejects": -1.8, "rejected": -1.8, "lawsuit": -1.5, "ban": -2.0, "adoption": 1.5,
    "listing": 1.2, "lists": 1.2, "unlock": -0.8, "selloff": -2.2, "sell-off": -2.2, "slump": -2.0,
}


@dataclass
class NewsItem:
    id: str
    ts: int
    source: str
    title: str
    url: str
    summary: str = ""
    credibility: float = 0.7
    symbols: list[str] = field(default_factory=list)
    event_type: str = "general"
    sentiment: float = 0.0
    sentiment_conf: float = 0.0
    impact: float = 0.0

    def to_json(self) -> dict:
        return {"id": self.id, "ts": self.ts, "source": self.source, "title": self.title, "url": self.url,
                "symbols": self.symbols, "event_type": self.event_type, "sentiment": round(self.sentiment, 3),
                "impact": round(self.impact, 1)}


class Sentiment:
    def __init__(self, transformer_model: str = ""):
        self.pipe = None
        if transformer_model:
            try:
                from transformers import pipeline
                self.pipe = pipeline("sentiment-analysis", model=transformer_model)
                log.info("loaded sentiment model %s", transformer_model)
            except Exception as e:  # noqa: BLE001
                log.warning("transformer sentiment unavailable (%s); using VADER", e)
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        self.vader = SentimentIntensityAnalyzer()
        self.vader.lexicon.update(CRYPTO_LEXICON)

    def score(self, text: str) -> tuple[float, float]:
        if self.pipe is not None:
            try:
                r = self.pipe(text[:512])[0]
                lab = r["label"].lower()
                s = r["score"] if "pos" in lab else (-r["score"] if "neg" in lab else 0.0)
                return s, r["score"]
            except Exception:  # noqa: BLE001
                pass
        c = self.vader.polarity_scores(text)["compound"]
        return c, abs(c)


class NewsService:
    def __init__(self, cfg: dict, on_item=None):
        self.cfg = cfg
        self.items: list[NewsItem] = []
        self.seen: set[str] = set()
        self.events: list[dict] = []           # economic calendar
        self.fear_greed: dict | None = None
        self.bases: set[str] = set()
        self.sentiment = Sentiment(cfg.get("transformer_model", ""))
        self.on_item = on_item
        self.health: dict[str, dict] = {}
        self._title_index: list[str] = []
        self.type_weights = {name: w for name, w, _ in EVENT_RULES}

    # ---------------------------------------------------------------- tagging & scoring
    def set_universe(self, bases: set[str]) -> None:
        self.bases = set(bases)

    def tag(self, text: str) -> list[str]:
        found = set()
        low = text.lower()
        for base, names in COIN_NAMES.items():
            if any(re.search(rf"\b{re.escape(n)}\b", low) for n in names):
                found.add(base)
        for m in re.finditer(r"\$([A-Z0-9]{2,10})\b", text):
            found.add(m.group(1))
        for m in re.finditer(r"\b([A-Z0-9]{2,10})\b", text):
            tok = m.group(1)
            if tok in self.bases and tok not in AMBIGUOUS and not tok.isdigit():
                found.add(tok)
        return sorted(b for b in found if not self.bases or b in self.bases or b in COIN_NAMES)

    @staticmethod
    def classify(text: str) -> str:
        low = f" {text.lower()} "
        for name, _, kws in EVENT_RULES:
            if any(k in low for k in kws):
                return name
        return "general"

    def impact(self, item: NewsItem, novelty: float) -> float:
        tw = self.type_weights.get(item.event_type, 0.3)
        mag = 0.4 + 0.6 * abs(item.sentiment)
        return round(100 * item.credibility * tw * mag * novelty, 1)

    def _novelty(self, title: str) -> float:
        t = title.lower()
        best = max((SequenceMatcher(None, t, o).ratio() for o in self._title_index[-300:]), default=0.0)
        return 1.0 - best if best > 0.6 else 1.0

    def ingest(self, source: str, title: str, url: str, ts: int, summary: str = "",
               credibility: float = 0.7) -> NewsItem | None:
        key = hashlib.sha1((url or title).split("?")[0].encode()).hexdigest()[:16]
        if key in self.seen or not title:
            return None
        nov = self._novelty(title)
        if nov < 0.15:                         # near-duplicate headline from another outlet
            self.seen.add(key)
            return None
        self.seen.add(key)
        self._title_index.append(title.lower())
        text = f"{title}. {summary}"
        item = NewsItem(key, ts, source, title.strip(), url, summary[:500], credibility)
        item.symbols = self.tag(text)
        item.event_type = self.classify(text)
        item.sentiment, item.sentiment_conf = self.sentiment.score(title)
        item.impact = self.impact(item, nov)
        self.items.append(item)
        self.items.sort(key=lambda x: x.ts)
        self.items = self.items[-1000:]
        if self.on_item:
            self.on_item(item)
        return item

    # ---------------------------------------------------------------- queries used by the engine
    def recent(self, symbol_base: str | None = None, limit: int = 100) -> list[dict]:
        its = [i for i in reversed(self.items) if symbol_base is None or symbol_base in i.symbols]
        return [i.to_json() for i in its[:limit]]

    def coin_sentiment(self, symbol: str, ts: int, hours: int = 6) -> float | None:
        base = _base(symbol)
        its = [i for i in self.items if base in i.symbols and ts - i.ts < hours * 3_600_000 and i.ts <= ts]
        if not its:
            return None
        w = sum(i.impact for i in its) or 1
        return sum(i.sentiment * i.impact for i in its) / w

    def coin_veto(self, symbol: str, side: str, ts: int) -> str | None:
        base = _base(symbol)
        for i in reversed(self.items):
            if ts - i.ts > 24 * 3_600_000:
                break
            if base not in i.symbols or i.ts > ts:
                continue
            if i.event_type == "delisting" and side == "LONG":
                return f"delisting/monitoring news: {i.title[:80]}"
            if i.event_type == "hack" and side == "LONG" and ts - i.ts < 6 * 3_600_000:
                return f"hack/exploit news: {i.title[:80]}"
        return None

    def catalyst(self, symbol: str, ts: int, minutes: int = 30) -> str | None:
        base = _base(symbol)
        for i in reversed(self.items):
            if ts - i.ts > minutes * 60_000:
                break
            if base in i.symbols and i.ts <= ts and i.impact >= 20:
                return f"{i.event_type}: {i.title[:90]}"
        return None

    def macro_block(self, ts: int) -> dict | None:
        before = self.cfg.get("macro_block_before_min", 15) * 60_000
        after = self.cfg.get("macro_block_after_min", 30) * 60_000
        for e in self.events:
            if e.get("impact") == "High" and e.get("country") in ("USD", "ALL") and e["ts"] - before <= ts <= e["ts"] + after:
                return e
        return None

    def upcoming_events(self, hours: int = 48) -> list[dict]:
        now = int(time.time() * 1000)
        return [e for e in self.events if now - 3_600_000 <= e["ts"] <= now + hours * 3_600_000]

    # ---------------------------------------------------------------- fetchers
    async def poll_forever(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.cfg.get("poll_seconds", 120))

    async def poll_once(self) -> None:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (scalping-terminal; +news reader)"}) as cli:
            tasks = [self._rss(cli, f) for f in self.cfg.get("rss", [])]
            if self.cfg.get("cryptopanic_token"):
                tasks.append(self._cryptopanic(cli))
            if self.cfg.get("calendar_url"):
                tasks.append(self._calendar(cli))
            if self.cfg.get("fear_greed", True):
                tasks.append(self._fng(cli))
            if self.cfg.get("binance_announcements", True):
                tasks.append(self._binance(cli))
            await asyncio.gather(*tasks, return_exceptions=True)

    def _ok(self, name: str, n: int) -> None:
        self.health[name] = {"ok": True, "items": n, "ts": int(time.time() * 1000)}

    def _fail(self, name: str, err) -> None:
        self.health[name] = {"ok": False, "error": str(err)[:120], "ts": int(time.time() * 1000)}

    async def _rss(self, cli: httpx.AsyncClient, feed: dict) -> None:
        name = feed["name"]
        try:
            r = await cli.get(feed["url"])
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            n = 0
            for e in parsed.entries[:40]:
                st = e.get("published_parsed") or e.get("updated_parsed")
                ts = int(calendar.timegm(st) * 1000) if st else int(time.time() * 1000)
                summ = re.sub("<[^>]+>", " ", e.get("summary", ""))
                if self.ingest(name, e.get("title", ""), e.get("link", ""), ts, summ, feed.get("credibility", 0.7)):
                    n += 1
            self._ok(name, n)
        except Exception as ex:  # noqa: BLE001
            self._fail(name, ex)

    async def _cryptopanic(self, cli: httpx.AsyncClient) -> None:
        try:
            r = await cli.get("https://cryptopanic.com/api/developer/v2/posts/",
                              params={"auth_token": self.cfg["cryptopanic_token"], "public": "true"})
            r.raise_for_status()
            n = 0
            for p in r.json().get("results", []):
                ts = int(datetime.fromisoformat(p["published_at"].replace("Z", "+00:00")).timestamp() * 1000)
                if self.ingest("CryptoPanic", p.get("title", ""), p.get("url") or p.get("original_url", ""), ts, "", 0.6):
                    n += 1
            self._ok("CryptoPanic", n)
        except Exception as ex:  # noqa: BLE001
            self._fail("CryptoPanic", ex)

    async def _calendar(self, cli: httpx.AsyncClient) -> None:
        try:
            r = await cli.get(self.cfg["calendar_url"])
            r.raise_for_status()
            evs = []
            for e in r.json():
                try:
                    ts = int(datetime.fromisoformat(e["date"]).astimezone(timezone.utc).timestamp() * 1000)
                except (KeyError, ValueError):
                    continue
                evs.append({"ts": ts, "title": e.get("title"), "country": e.get("country"), "impact": e.get("impact"),
                            "forecast": e.get("forecast"), "previous": e.get("previous"), "actual": e.get("actual")})
            self.events = sorted(evs, key=lambda x: x["ts"])
            self._ok("Economic calendar", len(evs))
        except Exception as ex:  # noqa: BLE001
            self._fail("Economic calendar", ex)

    async def _fng(self, cli: httpx.AsyncClient) -> None:
        try:
            r = await cli.get("https://api.alternative.me/fng/", params={"limit": 2})
            r.raise_for_status()
            d = r.json()["data"]
            self.fear_greed = {"value": int(d[0]["value"]), "label": d[0]["value_classification"],
                               "previous": int(d[1]["value"]) if len(d) > 1 else None}
            self._ok("Fear & Greed", 1)
        except Exception as ex:  # noqa: BLE001
            self._fail("Fear & Greed", ex)

    async def _binance(self, cli: httpx.AsyncClient) -> None:
        """Binance announcements (unofficial public CMS endpoint; may be region-restricted)."""
        n = 0
        try:
            for catalog in (48, 161):          # 48 = new listings, 161 = delistings
                r = await cli.get("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
                                  params={"type": 1, "catalogId": catalog, "pageNo": 1, "pageSize": 10})
                r.raise_for_status()
                for cat in r.json().get("data", {}).get("catalogs", []):
                    for a in cat.get("articles", []):
                        url = f"https://www.binance.com/en/support/announcement/{a.get('code', '')}"
                        if self.ingest("Binance", a.get("title", ""), url, int(a.get("releaseDate", time.time() * 1000)), "", 1.0):
                            n += 1
            self._ok("Binance announcements", n)
        except Exception as ex:  # noqa: BLE001
            self._fail("Binance announcements", ex)


def _base(symbol: str) -> str:
    for q in ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH"):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)]
    return symbol
