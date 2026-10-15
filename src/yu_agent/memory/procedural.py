"""程序性记忆：可复用的「操作流程模板」（如「打开浏览器→输入网址→回车」）。"""
from __future__ import annotations

from typing import List, Optional

from .base import MemoryEntry
from .store import MemoryStore


class ProceduralMemory:
    """沉淀可复用的操作序列（如何做某事）。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def remember(self, name: str, steps: List[str], app: str = "") -> int:
        content = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))
        entry = MemoryEntry(kind="procedural", content=f"技能[{name}]:\n{content}", app=app)
        return self.store.add(entry)

    def recall(self, query: str, limit: int = 3) -> List[MemoryEntry]:
        return self.store.search(query, kind="procedural", limit=limit)
