"""
Persistent query history (a separate metadata database).

History is stored in its own SQLite file (``data/querybridge_meta.db``), never
in the target database, which stays strictly read-only. Each entry records
the question, the SQL that ran, the outcome, and the cost and quality
signals (configuration, confidence, complexity, corrections, LLM calls and
tokens).

Credentials are never stored: entries carry only the *masked* database
label. Result rows are not stored either; revisiting an entry re-validates
and re-runs its SQL, so the user sees current data.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from utils.helpers import truncate_text, utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    database_label    TEXT    NOT NULL,
    question          TEXT    NOT NULL,
    sql               TEXT,
    status            TEXT    NOT NULL,
    intent            TEXT,
    row_count         INTEGER,
    execution_time_ms REAL,
    config_name       TEXT,
    confidence        REAL,
    complexity        TEXT,
    corrections       INTEGER DEFAULT 0,
    llm_calls         INTEGER DEFAULT 0,
    input_tokens      INTEGER DEFAULT 0,
    output_tokens     INTEGER DEFAULT 0,
    estimated_cost    REAL,
    message           TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_created ON query_history (created_at);
"""
_FIELDS = ("created_at", "database_label", "question", "sql", "status", "intent", "row_count",
           "execution_time_ms", "config_name", "confidence", "complexity", "corrections", "llm_calls",
           "input_tokens", "output_tokens", "estimated_cost", "message")


class HistoryEntry(BaseModel):
    id: int | None = None
    created_at: str = ""
    database_label: str
    question: str
    sql: str | None = None
    status: str
    intent: str | None = None
    row_count: int | None = None
    execution_time_ms: float | None = None
    config_name: str | None = None
    confidence: float | None = None
    complexity: str | None = None
    corrections: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float | None = None
    message: str | None = None


class HistoryStore:
    def __init__(self, path: str | Path, *, max_entries: int = 1000) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, entry: HistoryEntry) -> int:
        data = entry.model_dump()
        data["created_at"] = data["created_at"] or utc_now_iso()
        data["question"] = truncate_text(data["question"], 2000)
        data["message"] = truncate_text(data["message"], 1000) if data.get("message") else None
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO query_history ({', '.join(_FIELDS)}) VALUES ({', '.join('?' for _ in _FIELDS)})",
                [data[f] for f in _FIELDS],
            )
            conn.execute(
                "DELETE FROM query_history WHERE id NOT IN "
                "(SELECT id FROM query_history ORDER BY id DESC LIMIT ?)", (self.max_entries,))
            return int(cursor.lastrowid)

    def list(self, *, limit: int = 50, status: str | None = None, search: str | None = None) -> list[HistoryEntry]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if search:
            clauses.append("(question LIKE ? OR sql LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM query_history {where} ORDER BY id DESC LIMIT ?",
                                (*params, limit)).fetchall()
        return [HistoryEntry(**dict(row)) for row in rows]

    def get(self, entry_id: int) -> HistoryEntry | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM query_history WHERE id = ?", (entry_id,)).fetchone()
        return HistoryEntry(**dict(row)) if row else None

    def delete(self, entry_id: int) -> bool:
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM query_history WHERE id = ?", (entry_id,)).rowcount > 0

    def clear(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM query_history").rowcount

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM query_history").fetchone()[0]
            by_status = dict(conn.execute("SELECT status, COUNT(*) FROM query_history GROUP BY status").fetchall())
            avg_time = conn.execute("SELECT AVG(execution_time_ms) FROM query_history "
                                    "WHERE execution_time_ms IS NOT NULL").fetchone()[0]
            cost = conn.execute("SELECT SUM(estimated_cost) FROM query_history").fetchone()[0]
        return {"total": total, "by_status": by_status,
                "average_execution_ms": round(avg_time, 2) if avg_time is not None else None,
                "estimated_cost_usd": round(cost, 4) if cost else 0.0}
