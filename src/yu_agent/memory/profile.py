"""用户画像记忆：长期记住用户偏好、常用 App、习惯等。"""
from __future__ import annotations

from typing import List

from .base import MemoryEntry
from .store import MemoryStore


class ProfileMemory:
    """记住用户偏好（如「常用 Chrome」「喜欢深色模式」），跨会话保留。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def set_preference(self, key: str, value: str) -> int:
        entry = MemoryEntry(kind="profile", content=f"{key}: {value}", metadata={"key": key})
        return self.store.add(entry)

    def get_preferences(self) -> List[MemoryEntry]:
        return self.store.recent(kind="profile", limit=50)

    def describe(self) -> str:
        prefs = self.get_preferences()
        if not prefs:
            return ""
        return "\n".join(f"- {p.content}" for p in prefs)
