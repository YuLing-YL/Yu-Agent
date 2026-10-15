"""工作记忆（短时记忆）：当前任务进行中的上下文。

只保留最近 N 步的完整细节与截图，超出部分转为文本摘要，控制 token 成本。
任务结束后清空，不持久化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StepRecord:
    step: int
    observation_summary: str  # 当前界面文字描述
    action: str  # 执行的动作描述
    result: str  # 动作后的反馈
    screenshot_b64: Optional[str] = None  # 完整截图（很占 token）
    phash: Optional[int] = None


class WorkingMemory:
    """当前任务的工作记忆（短时）。"""

    def __init__(self, max_recent_steps: int = 6, max_recent_images: int = 2):
        self.max_recent_steps = max_recent_steps
        self.max_recent_images = max_recent_images
        self.task: str = ""
        self.subtasks: List[str] = []  # 拆分的子任务
        self.current_subtask: int = -1
        self.steps: List[StepRecord] = []
        self.goal: str = ""
        self.notes: Dict[str, Any] = {}

    def reset(self, task: str = "") -> None:
        self.task = task
        self.subtasks = []
        self.current_subtask = -1
        self.steps = []
        self.goal = task
        self.notes = {}

    def set_subtasks(self, subtasks: List[str]) -> None:
        self.subtasks = subtasks
        self.current_subtask = 0 if subtasks else -1

    def add_step(self, rec: StepRecord) -> None:
        self.steps.append(rec)
        # 只保留最近 N 步的截图
        if len(self.steps) > self.max_recent_steps:
            self.steps = self.steps[-self.max_recent_steps :]

    def context_for_prompt(self) -> str:
        """生成注入 prompt 的短时上下文（文本 + 可选最近截图）。"""
        parts: List[str] = []
        if self.goal:
            parts.append(f"任务目标：{self.goal}")
        if self.subtasks:
            parts.append("子任务计划：")
            for i, st in enumerate(self.subtasks):
                mark = "→ 当前" if i == self.current_subtask else "   "
                done = "✅" if i < self.current_subtask else ""
                parts.append(f"  {mark} [{i+1}] {st} {done}")

        recent = self.steps[-self.max_recent_steps :]
        if recent:
            parts.append("最近步骤：")
            for s in recent:
                parts.append(
                    f"  步骤{s.step}: 界面={s.observation_summary} | 动作={s.action} | 结果={s.result}"
                )
        return "\n".join(parts)

    def recent_screenshots(self) -> List[str]:
        """返回最近截图（最多 max_recent_images 张）的 base64。"""
        imgs: List[str] = []
        for s in reversed(self.steps):
            if s.screenshot_b64:
                imgs.append(s.screenshot_b64)
                if len(imgs) >= self.max_recent_images:
                    break
        return list(reversed(imgs))
