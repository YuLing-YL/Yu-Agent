"""记忆存储：SQLite 持久化。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

from ..common.errors import MemoryError
from ..common.paths import memory_dir
from .base import MemoryEntry


class MemoryStore:
    """SQLite 记忆库。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else memory_dir() / "yu_agent.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                app TEXT DEFAULT '',
                task TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at REAL,
                success INTEGER DEFAULT 1
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kind ON memories(kind)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_app ON memories(app)"
        )
        self._conn.commit()

    def add(self, entry: MemoryEntry) -> int:
        if entry.created_at == 0.0:
            entry.created_at = time.time()
        cur = self._conn.execute(
            "INSERT INTO memories (kind, content, app, task, metadata, created_at, success) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.kind,
                entry.content,
                entry.app,
                entry.task,
                json.dumps(entry.metadata, ensure_ascii=False),
                entry.created_at,
                entry.metadata.get("success", 1),
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore

    def search(
        self, query: str, kind: Optional[str] = None, app: Optional[str] = None,
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """关键词检索（LIKE 匹配，GUI 领域够用）。"""
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if app:
            sql += " AND app = ?"
            params.append(app)
        if query:
            sql += " AND (content LIKE ? OR task LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def recent(self, kind: Optional[str] = None, limit: int = 20) -> List[MemoryEntry]:
        sql = "SELECT * FROM memories"
        params: list = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def exists(self, kind: str, content: str) -> bool:
        """同样内容的记忆是否已经存在（去重用，避免同一句经验存十遍）。"""
        row = self._conn.execute(
            "SELECT 1 FROM memories WHERE kind = ? AND content = ? LIMIT 1",
            (kind, content),
        ).fetchone()
        return row is not None

    def touch(self, kind: str, content: str) -> int:
        """同内容记录的时间戳刷成"现在"，返回其 id（去重时用）。"""
        row = self._conn.execute(
            "SELECT id FROM memories WHERE kind = ? AND content = ? LIMIT 1",
            (kind, content),
        ).fetchone()
        if row is None:
            return 0
        self._conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?", (time.time(), row["id"])
        )
        self._conn.commit()
        return int(row["id"])

    def delete_like(self, patterns: List[str]) -> int:
        """按内容模式批量删除，返回删除条数（清理旧格式记忆用）。"""
        n = 0
        for pat in patterns:
            cur = self._conn.execute(
                "DELETE FROM memories WHERE content LIKE ?", (f"%{pat}%",)
            )
            n += cur.rowcount or 0
        self._conn.commit()
        return n

    def count_like(self, patterns: List[str]) -> int:
        n = 0
        for pat in patterns:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE content LIKE ?", (f"%{pat}%",)
            ).fetchone()
            n += int(row["c"] if row else 0)
        return n

    def delete_kind(self, kind: str) -> None:
        self._conn.execute("DELETE FROM memories WHERE kind = ?", (kind,))
        self._conn.commit()

    def clear(self) -> None:
        self._conn.execute("DELETE FROM memories")
        self._conn.commit()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            id=row["id"],
            kind=row["kind"],
            content=row["content"],
            app=row["app"],
            task=row["task"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=row["created_at"],
        )

    def close(self) -> None:
        self._conn.close()
