"""Persistence: SQLite for app records (signals, trends, move events, news, paper trades) and
an optional Parquet recorder for raw trades/book snapshots of focused symbols (spec §4.3, §13)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

TABLES = ("signals", "trends", "move_events", "news", "paper_trades")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            for t in TABLES:
                self.db.execute(f"CREATE TABLE IF NOT EXISTS {t} (id TEXT PRIMARY KEY, ts INTEGER, symbol TEXT, body TEXT)")
                self.db.execute(f"CREATE INDEX IF NOT EXISTS {t}_ts ON {t}(ts)")
            self.db.commit()

    def put(self, table: str, id_: str, ts: int, symbol: str | None, body: dict) -> None:
        with self.lock:
            self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?)", (id_, ts, symbol, json.dumps(body)))
            self.db.commit()

    def recent(self, table: str, limit: int = 200, symbol: str | None = None, since: int | None = None) -> list[dict]:
        q, args = f"SELECT body FROM {table}", []
        conds = []
        if symbol:
            conds.append("symbol = ?")
            args.append(symbol)
        if since:
            conds.append("ts >= ?")
            args.append(since)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(q, args)]


class ParquetRecorder:
    """Buffers rows per (kind, symbol) and flushes hourly-partitioned Parquet files."""

    def __init__(self, root: Path, flush_rows: int = 5000):
        self.root = root
        self.flush_rows = flush_rows
        self.buf: dict[tuple[str, str], list[dict]] = {}

    def add(self, kind: str, symbol: str, row: dict) -> None:
        b = self.buf.setdefault((kind, symbol), [])
        b.append(row)
        if len(b) >= self.flush_rows:
            self.flush(kind, symbol)

    def flush(self, kind: str | None = None, symbol: str | None = None) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq
        for (k, s), rows in list(self.buf.items()):
            if (kind and k != kind) or (symbol and s != symbol) or not rows:
                continue
            hour = time.strftime("%Y-%m-%d/%H", time.gmtime(rows[0].get("ts", time.time() * 1000) / 1000))
            d = self.root / k / s / hour
            d.mkdir(parents=True, exist_ok=True)
            if k == "book":
                rows = [{**r, "bids": json.dumps(r["bids"]), "asks": json.dumps(r["asks"])} for r in rows]
            pq.write_table(pa.Table.from_pylist(rows), d / f"{int(time.time() * 1000)}.parquet")
            self.buf[(k, s)] = []
