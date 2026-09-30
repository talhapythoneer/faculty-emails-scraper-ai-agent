"""SQLite storage: page cache, per-row module results, review overrides, AI usage and AI response cache."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    key TEXT PRIMARY KEY,
    final_url TEXT,
    status INTEGER,
    content_type TEXT,
    body BLOB,
    via TEXT,
    fetched_at REAL
);
CREATE TABLE IF NOT EXISTS results (
    module TEXT NOT NULL,
    unitid TEXT NOT NULL,
    input_key TEXT,
    data TEXT,
    updated_at REAL,
    PRIMARY KEY (module, unitid)
);
CREATE TABLE IF NOT EXISTS overrides (
    module TEXT NOT NULL,
    unitid TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (module, unitid)
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module TEXT,
    unitid TEXT,
    purpose TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    ts REAL
);
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    response TEXT,
    ts REAL
);
"""


class DB:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _one(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _write(self, sql: str, params=()):
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    # --- page cache -------------------------------------------------------
    def get_page(self, key: str) -> dict | None:
        row = self._one("SELECT final_url, status, content_type, body, via FROM pages WHERE key=?", (key,))
        if not row:
            return None
        final_url, status, ctype, body, via = row
        html = zlib.decompress(body).decode("utf-8", "replace") if body else ""
        return {"final_url": final_url, "status": status, "content_type": ctype or "", "html": html, "via": via}

    def put_page(self, key: str, final_url: str, status: int, content_type: str, html: str, via: str) -> None:
        body = zlib.compress((html or "").encode("utf-8", "replace"))
        self._write("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?)",
                    (key, final_url, status, content_type, body, via, time.time()))

    def count_pages(self, via: str) -> int:
        row = self._one("SELECT COUNT(*) FROM pages WHERE via=?", (via,))
        return int(row[0] or 0) if row else 0

    # --- module results -----------------------------------------------------
    def get_result(self, module: str, unitid: str) -> dict | None:
        row = self._one("SELECT input_key, data FROM results WHERE module=? AND unitid=?", (module, unitid))
        if not row:
            return None
        return {"input_key": row[0] or "", "data": json.loads(row[1]) if row[1] else {}}

    def put_result(self, module: str, unitid: str, input_key: str, data: dict) -> None:
        self._write("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?)",
                    (module, unitid, input_key, json.dumps(data, ensure_ascii=False), time.time()))

    def all_results(self, module: str) -> dict[str, dict]:
        rows = self._all("SELECT unitid, data FROM results WHERE module=?", (module,))
        return {u: json.loads(d) if d else {} for u, d in rows}

    # --- manual overrides (mirrored from the review spreadsheets) -----------
    def set_override(self, module: str, unitid: str, value: str) -> None:
        self._write("INSERT OR REPLACE INTO overrides VALUES (?,?,?)", (module, unitid, value))

    def get_overrides(self, module: str) -> dict[str, str]:
        rows = self._all("SELECT unitid, value FROM overrides WHERE module=?", (module,))
        return {u: v for u, v in rows if v}

    # --- AI usage + cache ---------------------------------------------------
    def log_llm(self, module: str, unitid: str, purpose: str, model: str, input_tokens: int, output_tokens: int):
        self._write("INSERT INTO llm_usage (module, unitid, purpose, model, input_tokens, output_tokens, ts) "
                    "VALUES (?,?,?,?,?,?,?)", (module, unitid, purpose, model, input_tokens, output_tokens, time.time()))

    def llm_usage_summary(self) -> list[tuple]:
        return self._all("SELECT model, purpose, COUNT(*), SUM(input_tokens), SUM(output_tokens), "
                         "COUNT(DISTINCT unitid) FROM llm_usage GROUP BY model, purpose ORDER BY model, purpose")

    def llm_institution_count(self) -> int:
        row = self._one("SELECT COUNT(DISTINCT unitid) FROM llm_usage")
        return int(row[0] or 0) if row else 0

    def get_llm_cache(self, key: str) -> str | None:
        row = self._one("SELECT response FROM llm_cache WHERE key=?", (key,))
        return row[0] if row else None

    def put_llm_cache(self, key: str, response: str) -> None:
        self._write("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?)", (key, response, time.time()))
