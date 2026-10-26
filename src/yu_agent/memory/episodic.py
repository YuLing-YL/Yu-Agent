"""情节记忆：一次任务的完整执行轨迹（长期、可回放）。"""
from __future__ import annotations

from typing import List

from .base import MemoryEntry
from .store import MemoryStore


class EpisodicMemory:
    """记录每次任务发生了什么（时间线）。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def record(self, task: str, content: str, app: str = "", success: bool = True,
               dedupe: bool = False) -> int:
        """记一条「这次任务发生了什么」。

        dedupe=True：完全相同的内容不再插入新行，只把时间戳刷成现在。
        同一件任务反复跑（评测、重试）时，否则会存下几十条一模一样的记录，
        召回时全是重复噪声。
        """
        if dedupe:
            hit = self.store.touch("episodic", content)
            if hit:
                return hit
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
