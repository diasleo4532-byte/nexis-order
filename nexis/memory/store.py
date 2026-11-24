from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS facts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
USING fts5(content, content='messages', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class MemoryStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self.fts_enabled = True
        try:
            self._conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            self.fts_enabled = False
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def touch_session(self, session_id: str) -> None:
        stamp = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(session_id, started_at, last_active_at) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_active_at=excluded.last_active_at",
                (session_id, stamp, stamp),
            )
            self._conn.commit()

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at) VALUES(?,?,?,?)",
                (session_id, role, content, _now()),
            )
            self._conn.execute(
                "INSERT INTO sessions(session_id, started_at, last_active_at) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_active_at=excluded.last_active_at",
                (session_id, _now(), _now()),
            )
            self._conn.commit()

    def history(self, session_id: str, turns: int = 20) -> list[dict[str, str]]:
        if turns <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, turns * 2),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, started_at, last_active_at FROM sessions "
                "ORDER BY last_active_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_fact(self, key: str, value: str) -> None:
        key = key.strip().lower()
        if not key:
            raise ValueError("Fact key must not be empty")
        with self._lock:
            self._conn.execute(
                "INSERT INTO facts(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value.strip(), _now()),
            )
            self._conn.commit()

    def facts(self, limit: int | None = None) -> dict[str, str]:
        query = "SELECT key, value FROM facts ORDER BY updated_at DESC"
        params: tuple[Any, ...] = ()
        if limit:
            query += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def forget_fact(self, key: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM facts WHERE key=?", (key.strip().lower(),))
            self._conn.commit()
        return cursor.rowcount > 0

    def search_history(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        query = query.strip()
        if not query:
            return []
        with self._lock:
            if self.fts_enabled:
                try:
                    rows = self._conn.execute(
                        "SELECT m.role, m.content, m.created_at, m.session_id "
                        "FROM messages_fts f JOIN messages m ON m.id=f.rowid "
                        "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
                        (_fts_query(query), limit),
                    ).fetchall()
                    return [dict(row) for row in rows]
                except sqlite3.OperationalError:
                    pass
            rows = self._conn.execute(
                "SELECT role, content, created_at, session_id FROM messages "
                "WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [dict(row) for row in rows]


def _fts_query(text: str) -> str:
    tokens = [token for token in "".join(
        ch if ch.isalnum() or ch.isspace() else " " for ch in text
    ).split() if token]
    if not tokens:
        return '""'
    return " OR ".join(f'"{token}"' for token in tokens[:8])
