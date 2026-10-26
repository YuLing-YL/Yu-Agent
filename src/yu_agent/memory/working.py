"""工作记忆（短时记忆）：当前任务进行中的上下文。

它要回答的就两个问题：
1. **我上一步干了什么、有没有生效**（不能记成 "click(513,127)" —— 模型看不懂，
   也回忆不起自己点的是哪个元素）；
2. **这一步该干什么**（由子任务计划提供，见 agent/loop.py 的 _build_subtask_hint）。

所以这里存的是「人话版」的一步：
    步骤 6  点了「搜索框」(WorkBuddy)   → 屏幕有变化
    步骤 5  输入「今天天气」             → 无变化（已重试 1 次）

只保留最近 N 步，任务结束即丢弃（跨任务的东西进长期记忆，见 distillation.py）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# 一步的「效果」标签（用于告诉模型上一步到底成没成）
EFFECT_CHANGED = "changed"
EFFECT_NO_CHANGE = "no_change"
EFFECT_FAILED = "failed"


@dataclass
class StepRecord:
    step: int
    observation_summary: str  # 操作**之前**界面是什么样（哪个应用 / 多少可点元素）
    action: str  # 这一步做了什么（人话：点了「搜索框」/ 输入「天气」/ 按 enter）
    result: str  # 执行反馈
    effect: str = ""  # changed / no_change / failed
    screenshot_b64: Optional[str] = None  # 完整截图（很占 token，默认不用）
    phash: Optional[int] = None


class WorkingMemory:
    """当前任务的工作记忆（短时）。"""

    def __init__(self, max_recent_steps: int = 6, max_recent_images: int = 2):
        self.max_recent_steps = max_recent_steps
        self.max_recent_images = max_recent_images
        self.task: str = ""
        self.subtasks: List[str] = []
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
        # 只保留最近 N 步（更早的对"下一步该干嘛"没有帮助，只会占 token）
        if len(self.steps) > self.max_recent_steps:
            self.steps = self.steps[-self.max_recent_steps :]

    # -- 渲染 ---------------------------------------------------------- #
    def _effect_text(self, effect: str, en: bool) -> str:
        if effect == EFFECT_NO_CHANGE:
            return "screen did NOT change" if en else "屏幕没变化"
        if effect == EFFECT_FAILED:
            return "FAILED" if en else "执行失败"
        if effect == EFFECT_CHANGED:
            return "screen changed" if en else "屏幕有变化"
        return ""

    def context_for_prompt(self, language: str = "", include_plan: bool = True) -> str:
        """生成注入 prompt 的短时上下文。

        language: 提示词语言（"en" 时用英文标签，任务内容本身保持原样）
        include_plan: 是否把子任务计划也写进来。
            默认 True 兼容旧调用；agent 主循环传 False —— 子任务由
            `_build_subtask_hint` 单独强调，两处都写会重复占 token。
        """
        en = str(language or "").strip().lower().startswith("en")
        parts: List[str] = []
        if self.goal and not self.subtasks:
            # 有子任务计划时，目标由计划本身说清楚，不用再重复一遍
            parts.append(f"Goal: {self.goal}" if en else f"任务目标：{self.goal}")
        if include_plan and self.subtasks:
            parts.append("Subtask plan:" if en else "子任务计划：")
            for i, st in enumerate(self.subtasks):
                if en:
                    mark = "-> current" if i == self.current_subtask else "  "
                    done = " (done)" if i < self.current_subtask else ""
                else:
                    mark = "→ 当前" if i == self.current_subtask else "  "
                    done = "✅" if i < self.current_subtask else ""
                parts.append(f"  {mark} [{i + 1}] {st}{done}")

        recent = self.steps[-self.max_recent_steps :]
        if recent:
            # 倒序：越近越靠上，模型一眼就能看到"上一步"
            parts.append(
                "What you already did (newest first):" if en
                else "你已经做过的（最近的在上）："
            )
            for s in reversed(recent):
                bits: List[str] = []
                if s.action:
                    bits.append(s.action)
                if s.observation_summary:
                    bits.append(f"on {s.observation_summary}" if en
                                else f"（{s.observation_summary}）")
                eff = self._effect_text(s.effect, en)
                if eff:
                    bits.append(f"→ {eff}")
                elif s.result:
                    bits.append(f"→ {s.result[:60]}")
                line = " ".join(bits) if en else " ".join(bits)
                parts.append(f"  step {s.step}: {line}" if en else f"  步骤{s.step}：{line}")
            # 一句提醒，避免模型重复做同一件事
            last = recent[-1]
            if last.effect == EFFECT_NO_CHANGE:
                parts.append(
                    "⚠️ The last step had no visible effect — do something DIFFERENT."
                    if en else
                    "⚠️ 上一步屏幕上没有任何变化 —— 换一种做法，不要重复它。"
                )
        return "\n".join(parts)

    def last_step(self) -> Optional[StepRecord]:
        return self.steps[-1] if self.steps else None

    def recent_screenshots(self) -> List[str]:
        """返回最近截图（最多 max_recent_images 张）的 base64。"""
        imgs: List[str] = []
        for s in reversed(self.steps):
            if s.screenshot_b64:
                imgs.append(s.screenshot_b64)
                if len(imgs) >= self.max_recent_images:
                    break
        return list(reversed(imgs))
