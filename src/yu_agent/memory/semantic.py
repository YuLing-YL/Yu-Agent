"""语义记忆：跨任务的通用知识（如「某 App 的设置入口在右下角」）。"""
from __future__ import annotations

from typing import List, Optional

from .base import MemoryEntry
from .store import MemoryStore


class SemanticMemory:
    """沉淀「事实/位置/操作规律」等长期知识，跨任务复用。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def remember(self, content: str, app: str = "", **meta) -> int:
        entry = MemoryEntry(kind="semantic", content=content, app=app, metadata=meta)
        return self.store.add(entry)

    def recall(self, query: str, app: Optional[str] = None, limit: int = 5) -> List[MemoryEntry]:
        return self.store.search(query, kind="semantic", app=app, limit=limit)
