"""记忆蒸馏：任务结束后把轨迹结晶为长期记忆。"""
from __future__ import annotations

from typing import Any, Dict, List

from .episodic import EpisodicMemory
from .procedural import ProceduralMemory
from .semantic import SemanticMemory


class MemoryDistiller:
    """把一次任务的 steps 轨迹蒸馏成可复用记忆。

    简单策略：
    - 任务整体成功 → 情节记忆记录成功案例
    - 连续重复的动作序列 → 程序性记忆模板
    - 关键结论 → 语义记忆
    """

    def __init__(self, episodic: EpisodicMemory, semantic: SemanticMemory, procedural: ProceduralMemory):
        self.episodic = episodic
        self.semantic = semantic
        self.procedural = procedural

    def distill(self, task: str, steps: List[Dict[str, Any]], success: bool) -> None:
        # 情节记忆
        summary = "; ".join(f"{s.get('action', '')}" for s in steps)
        self.episodic.record(task, f"任务『{task}』共 {len(steps)} 步，结果={'成功' if success else '失败'}。轨迹: {summary[:500]}", success=success)

        # 语义记忆：记录成功结论
        if success and steps:
            self.semantic.remember(f"完成任务『{task}』的方法已沉淀，共 {len(steps)} 步。")
