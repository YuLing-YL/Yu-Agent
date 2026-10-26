"""程序性记忆：可复用的「操作流程模板」（如「打开浏览器→输入网址→回车」）。"""
from __future__ import annotations

import re
from typing import List, Optional

from .base import MemoryEntry
from .store import MemoryStore

_STEP_LINE = re.compile(r"^\s*\d+[.、)]\s*(.+)$")


def parse_steps(content: str) -> List[str]:
    """把存下来的「1. xxx / 2. xxx」解析回步骤列表。"""
    steps: List[str] = []
    for line in (content or "").splitlines():
        m = _STEP_LINE.match(line)
        if m:
            step = m.group(1).strip()
            if step:
                steps.append(step)
    return steps


class ProceduralMemory:
    """沉淀可复用的操作序列（如何做某事）。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def remember(self, name: str, steps: List[str], app: str = "",
                 dedupe: bool = False) -> int:
        content = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
        text = f"技能[{name}]:\n{content}"
        if dedupe and self.store.exists("procedural", text):
            return 0
        entry = MemoryEntry(kind="procedural", content=text, app=app)
        return self.store.add(entry)

    def recall(self, query: str, limit: int = 3) -> List[MemoryEntry]:
        return self.store.search(query, kind="procedural", limit=limit)

    def best_steps(self, task: str, min_steps: int = 2) -> List[str]:
        """找一条跟当前任务最像的成功流程，返回它的步骤。

        只作**建议**用（作为初始子任务），不自动执行：环境会变，
        上次能走通的路径这次可能第一步就卡住，硬套反而有害。

        不能拿整句任务去 LIKE（中文没有空格，永远匹配不上），
        先用和召回同一套分词拆出关键词再逐个探。
        """
        from .retrieval import MemoryRetriever

        keywords = MemoryRetriever._extract_keywords(task)
        if not keywords:
            return []

        scored: List[tuple] = []
        seen_ids = set()
        for kw in keywords:
            for entry in self.store.search(kw, kind="procedural", limit=5):
                if entry.id in seen_ids:
                    continue
                seen_ids.add(entry.id)
                steps = parse_steps(entry.content)
                if len(steps) < min_steps:
                    continue
                hay = entry.content.lower()
                hits = sum(len(k) for k in keywords if k.lower() in hay)
                scored.append((hits, len(steps), steps))

        if not scored:
            return []
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        # 只在「至少命中 2 个关键词」时才推荐，避免乱套无关流程
        top_hits, _n, steps = scored[0]
        return steps if top_hits >= 2 else []
