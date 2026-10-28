from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  source TEXT, url TEXT, title TEXT,
  price INTEGER, area REAL, rooms INTEGER, location TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  score INTEGER, analysis TEXT, draft TEXT, listing TEXT,
  tg_message_id INTEGER,
  first_seen REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

_UPDATABLE = {"status", "score", "analysis", "draft", "listing", "tg_message_id"}


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(str(path))
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        self.c.commit()

    # --- listings ---------------------------------------------------------
    def has(self, key: str) -> bool:
        return self.c.execute("SELECT 1 FROM listings WHERE key=?", (key,)).fetchone() is not None

    def insert(self, l: Listing, status: str, analysis: dict | None = None, draft: str | None = None) -> int:
        now = time.time()
        self.c.execute(
            "INSERT OR IGNORE INTO listings (key,source,url,title,price,area,rooms,location,"
            "status,score,analysis,draft,listing,first_seen,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                l.key, l.source, l.url, l.title, l.price, l.area, l.rooms, l.location,
                status,
                (analysis or {}).get("fit_score"),
                json.dumps(analysis, ensure_ascii=False) if analysis else None,
                draft,
                json.dumps(asdict(l), ensure_ascii=False),
                now, now,
            ),
        )
        self.c.commit()
        return self.c.execute("SELECT id FROM listings WHERE key=?", (l.key,)).fetchone()["id"]

    def update(self, row_id: int, **fields) -> None:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"нельзя обновлять поля: {bad}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.c.execute(
            f"UPDATE listings SET {sets}, updated=? WHERE id=?",
            (*fields.values(), time.time(), row_id),
        )
        self.c.commit()

    def delete(self, row_id: int) -> None:
        self.c.execute("DELETE FROM listings WHERE id=?", (row_id,))
        self.c.commit()

    def get(self, row_id: int):
        return self.c.execute("SELECT * FROM listings WHERE id=?", (row_id,)).fetchone()

    def by_message(self, message_id: int):
        return self.c.execute(
            "SELECT * FROM listings WHERE tg_message_id=?", (message_id,)
        ).fetchone()

    def counts(self) -> dict:
        rows = self.c.execute("SELECT status, COUNT(*) n FROM listings GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # --- key/value state --------------------------------------------------
    def kv_get(self, k: str, default: str | None = None) -> str | None:
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def kv_prefix(self, prefix: str) -> list[tuple[str, str]]:
        rows = self.c.execute("SELECT k, v FROM kv WHERE k LIKE ? ESCAPE '\\'",
                              (prefix.replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall()
        return [(r["k"], r["v"]) for r in rows]

    def kv_set(self, k: str, v: str) -> None:
        self.c.execute("INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.c.commit()
