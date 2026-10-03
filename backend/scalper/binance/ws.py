"""Combined-stream WebSocket manager: many streams over few connections, dynamic subscribe,
reconnect with backoff + jitter, stale-connection detection, forced reconnect before 24h (spec §4.1)."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
from typing import Awaitable, Callable

import websockets

log = logging.getLogger(__name__)

Handler = Callable[[str, dict], Awaitable[None] | None]


def _ssl_context() -> ssl.SSLContext:
    """Verify TLS with certifi's CA bundle when available (Android's Python has no system store)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


class _Conn:
    def __init__(self, mgr: "StreamManager", idx: int):
        self.mgr = mgr
        self.idx = idx
        self.streams: set[str] = set()
        self.ws = None
        self.task: asyncio.Task | None = None
        self.msg_id = 0
        self.last_msg = 0.0
        self.connected_at = 0.0
        self.pending: asyncio.Queue = asyncio.Queue()
        self.connected = False

    async def run(self) -> None:
        backoff = 1.0
        while not self.mgr.closing:
            if not self.streams:
                await asyncio.sleep(0.5)
                continue
            url = f"{self.mgr.base}/stream?streams=" + "/".join(sorted(self.streams))
            try:
                async with websockets.connect(url, open_timeout=15, ping_interval=None, max_size=2 ** 23,
                                              close_timeout=3, ssl=_SSL if url.startswith("wss") else None) as ws:
                    self.ws, self.connected = ws, True
                    self.connected_at = self.last_msg = time.time()
                    self.mgr.reconnects += 1 if backoff > 1 else 0
                    backoff = 1.0
                    sender = asyncio.create_task(self._sender(ws))
                    watchdog = asyncio.create_task(self._watchdog(ws))
                    try:
                        async for raw in ws:
                            self.last_msg = time.time()
                            self.mgr.messages += 1
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                continue
                            stream, data = msg.get("stream"), msg.get("data")
                            if stream and data is not None:
                                r = self.mgr.handler(stream, data)
                                if asyncio.iscoroutine(r):
                                    await r
                    finally:
                        sender.cancel()
                        watchdog.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("ws conn %s error: %s", self.idx, e)
            self.connected = False
            self.ws = None
            if self.mgr.closing:
                break
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)

    async def _sender(self, ws) -> None:
        """Rate-limited SUBSCRIBE/UNSUBSCRIBE sender (Binance allows ~5 messages/s per connection)."""
        while True:
            method, params = await self.pending.get()
            self.msg_id += 1
            await ws.send(json.dumps({"method": method, "params": params, "id": self.msg_id}))
            await asyncio.sleep(0.25)

    async def _watchdog(self, ws) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.time()
            if now - self.last_msg > self.mgr.stale_after:
                log.warning("ws conn %s stale for %.0fs, reconnecting", self.idx, now - self.last_msg)
                await ws.close()
                return
            if now - self.connected_at > 23 * 3600:
                log.info("ws conn %s: scheduled reconnect before 24h limit", self.idx)
                await ws.close()
                return

    def subscribe(self, streams: list[str]) -> None:
        new = [s for s in streams if s not in self.streams]
        self.streams.update(new)
        if new and self.connected:
            for i in range(0, len(new), 50):
                self.pending.put_nowait(("SUBSCRIBE", new[i:i + 50]))

    def unsubscribe(self, streams: list[str]) -> None:
        old = [s for s in streams if s in self.streams]
        self.streams.difference_update(old)
        if old and self.connected:
            for i in range(0, len(old), 50):
                self.pending.put_nowait(("UNSUBSCRIBE", old[i:i + 50]))


class StreamManager:
    def __init__(self, base: str, handler: Handler, max_streams: int = 200, stale_after: float = 30.0):
        self.base = base.rstrip("/")
        self.handler = handler
        self.max_streams = max_streams
        self.stale_after = stale_after
        self.conns: list[_Conn] = []
        self.closing = False
        self.messages = 0
        self.reconnects = 0

    def _conn_for(self) -> _Conn:
        for c in self.conns:
            if len(c.streams) < self.max_streams:
                return c
        c = _Conn(self, len(self.conns))
        self.conns.append(c)
        c.task = asyncio.create_task(c.run())
        return c

    def subscribe(self, streams: list[str]) -> None:
        have = self.streams()
        todo = [s for s in streams if s not in have]
        while todo:
            c = self._conn_for()
            room = self.max_streams - len(c.streams)
            c.subscribe(todo[:room])
            todo = todo[room:]

    def unsubscribe(self, streams: list[str]) -> None:
        for c in self.conns:
            c.unsubscribe([s for s in streams if s in c.streams])

    def streams(self) -> set[str]:
        out: set[str] = set()
        for c in self.conns:
            out |= c.streams
        return out

    def status(self) -> dict:
        return {"connections": len(self.conns), "connected": sum(1 for c in self.conns if c.connected),
                "streams": sum(len(c.streams) for c in self.conns), "messages": self.messages,
                "reconnects": self.reconnects,
                "max_silence_s": round(max((time.time() - c.last_msg for c in self.conns if c.connected), default=0), 1)}

    async def close(self) -> None:
        self.closing = True
        for c in self.conns:
            if c.ws is not None:
                await c.ws.close()
            if c.task:
                c.task.cancel()
