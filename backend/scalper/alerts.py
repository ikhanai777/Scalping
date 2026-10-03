"""Alert delivery: Telegram bot and Discord webhook (spec §10.6). Both are free."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)


class Alerts:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.local_hook = None             # callable(title, body): native notifications (Android app)
        self.sent: list[dict] = []
        self._last: dict[str, float] = {}
        self.cli = httpx.AsyncClient(timeout=10)

    @property
    def enabled(self) -> bool:
        return bool((self.cfg.get("telegram_token") and self.cfg.get("telegram_chat_id")) or self.cfg.get("discord_webhook"))

    def send(self, text: str, key: str | None = None, min_interval: float = 60) -> None:
        """Fire-and-forget; de-duplicates the same key within ``min_interval`` seconds."""
        now = time.time()
        if key and now - self._last.get(key, 0) < min_interval:
            return
        if key:
            self._last[key] = now
        self.sent = (self.sent + [{"ts": int(now * 1000), "text": text}])[-200:]
        if self.local_hook and self.cfg.get("local_notifications", True):
            title, _, body = text.partition("\n")
            try:
                self.local_hook(title[:120], body[:500])
            except Exception as e:  # noqa: BLE001
                log.debug("local notification failed: %s", e)
        if self.enabled:
            asyncio.create_task(self._deliver(text))

    async def _deliver(self, text: str) -> None:
        try:
            if self.cfg.get("telegram_token") and self.cfg.get("telegram_chat_id"):
                await self.cli.post(f"https://api.telegram.org/bot{self.cfg['telegram_token']}/sendMessage",
                                    json={"chat_id": self.cfg["telegram_chat_id"], "text": text,
                                          "disable_web_page_preview": True})
            if self.cfg.get("discord_webhook"):
                await self.cli.post(self.cfg["discord_webhook"], json={"content": text[:1900]})
        except Exception as e:  # noqa: BLE001
            log.warning("alert delivery failed: %s", e)


def fmt_signal(s: dict) -> str:
    p = f"{s['p_win']:.0%}" if s.get("p_win") is not None else "unvalidated"
    tg = " / ".join(f"{t['price']:.6g}" for t in s["targets"])
    return (f"{'🟢' if s['side'] == 'LONG' else '🔴'} {s['symbol']} {s['side']} [{s['strategy'].split('@')[0]} {s['tf']}]\n"
            f"Entry {s['entry']['price']:.6g} · SL {s['stop']:.6g} · TP {tg}\n"
            f"P(win) {p} · EV {s['expected_R'] if s['expected_R'] is not None else '—'}R · confluence {s['confluence']}\n"
            f"{'; '.join(s['reasons'][:4])}")


def fmt_trend(ev: dict, alignment: str) -> str:
    arrow = "📈" if ev["direction"] == "UP" else "📉"
    return f"{arrow} {ev['symbol']} {ev['tf']} trend {ev['state']} {ev['direction']} (score {ev['score']}, alignment {alignment})"


def fmt_pump(ev: dict) -> str:
    e = ev.get("event") or {}
    icon = "🚀" if ev.get("direction") == "PUMP" else "💥"
    return (f"{icon} {ev['symbol']} {ev.get('direction')} {ev['stage']} · move {e.get('move_pct', 0):+.2f}% · "
            f"vol ×{e.get('vol_x', 0)} · {e.get('classification', '')}"
            + (f" · catalyst: {e['catalyst']}" if e.get("catalyst") else "")
            + (f"\nChase limit {e['chase_limit']:.6g}" if e.get("chase_limit") else ""))
