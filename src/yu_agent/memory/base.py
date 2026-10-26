"""记忆模块基础类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class MemoryEntry:
    """一条记忆记录。"""

    id: Optional[int] = None
    kind: str = ""  # episodic | semantic | procedural | profile
    content: str = ""
    app: str = ""
    task: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    score: float = 0.0  # 检索时打分用
