"""语义记忆：跨任务的通用知识（如「某 App 的设置入口在右下角」）。"""
from __future__ import annotations

from typing import List, Optional

from .base import MemoryEntry
from .store import MemoryStore


class SemanticMemory:
    """沉淀「事实/位置/操作规律」等长期知识，跨任务复用。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def remember(self, content: str, app: str = "", dedupe: bool = False, **meta) -> int:
        """存一条通用知识。

        dedupe=True 时，完全相同的句子只存一次 —— 蒸馏每次都跑，
        没有这层去重，同一句「先点搜索框再输入」会塞满整个记忆库，
        召回时反而把真正有用的挤掉。返回已存在那条的 id。
        """
        if dedupe:
            existing = self.store.search(content, kind="semantic", limit=1)
            if existing and existing[0].content.strip() == content.strip():
                return int(existing[0].id or 0)
            if self.store.exists("semantic", content):
                return 0
        entry = MemoryEntry(kind="semantic", content=content, app=app, metadata=meta)
        return self.store.add(entry)

    def recall(self, query: str, app: Optional[str] = None, limit: int = 5) -> List[MemoryEntry]:
        return self.store.search(query, kind="semantic", app=app, limit=limit)
