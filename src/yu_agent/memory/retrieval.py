"""记忆检索：统一入口，融合各层记忆为可注入 prompt 的上下文。"""
from __future__ import annotations

from typing import List

from .base import MemoryEntry
from .store import MemoryStore


class MemoryRetriever:
    """按当前任务从长期记忆里召回相关内容。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def retrieve(self, task: str, app: str = "", top_k: int = 5) -> List[MemoryEntry]:
        """关键词召回：情节 + 语义 + 程序性 记忆各取若干，按相关性排序。"""
        results: List[MemoryEntry] = []
        # 用任务里的关键词做匹配
        keywords = self._extract_keywords(task)
        for kw in keywords[:5]:
            results.extend(self.store.search(kw, limit=top_k))
        # 去重（按 id）
        seen = set()
        dedup: List[MemoryEntry] = []
        for e in results:
            if e.id not in seen:
                seen.add(e.id)
                dedup.append(e)
        return dedup[:top_k]

    @staticmethod
    def _extract_keywords(task: str) -> List[str]:
        """简单分词：按空格/标点切分，去掉太短的词。"""
        import re

        words = re.split(r"[\s，。！？、；：,\.!?;:]+", task)
        return [w for w in words if len(w) >= 2]

    def context_for_prompt(self, task: str, app: str = "") -> str:
        entries = self.retrieve(task, app)
        if not entries:
            return ""
        lines = ["【相关历史记忆】"]
        for e in entries:
            lines.append(f"- ({e.kind}) {e.content[:200]}")
        return "\n".join(lines)
