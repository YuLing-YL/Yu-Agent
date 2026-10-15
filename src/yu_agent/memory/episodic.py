"""情节记忆：一次任务的完整执行轨迹（长期、可回放）。"""
from __future__ import annotations

from typing import List, Optional

from .base import MemoryEntry
from .store import MemoryStore


class EpisodicMemory:
    """记录每次任务发生了什么（时间线）。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def record(self, task: str, content: str, app: str = "", success: bool = True) -> int:
        entry = MemoryEntry(
            kind="episodic",
            content=content,
            task=task,
            app=app,
            metadata={"success": 1 if success else 0},
        )
        return self.store.add(entry)

    def recall(self, task_hint: str, limit: int = 5) -> List[MemoryEntry]:
        return self.store.search(task_hint, kind="episodic", limit=limit)
