"""Agent 运行状态。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentState:
    """一次任务运行的实时状态。"""

    task: str = ""
    step: int = 0
    running: bool = False
    done: bool = False
    success: bool = False
    reason: str = ""  # 结束原因
    auto_stopped: bool = False  # 是否因重复/卡死被系统自动停止
    history: List[Dict[str, Any]] = field(default_factory=list)
    subtasks: List[str] = field(default_factory=list)
    current_subtask: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "step": self.step,
            "running": self.running,
            "done": self.done,
            "success": self.success,
            "reason": self.reason,
            "auto_stopped": self.auto_stopped,
            "subtasks": self.subtasks,
            "current_subtask": self.current_subtask,
        }
