"""规划者：根据观察 + 记忆 + 上下文，决定下一步动作。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..actions.schema import Action, describe_actions
from ..common.errors import GroundingError, ModelError
from ..grounding.parser import _to_action
from ..models.base import VLMRequest
from ..prompts.loader import render_prompt
from ..skills.registry import SkillRegistry


class Planner:
    """调用模型，输出下一步动作。"""

    def __init__(self, backend, skill_registry: Optional[SkillRegistry] = None):
        self.backend = backend
        self.skill_registry = skill_registry

    def plan(
        self,
        task: str,
        screenshot_data_url: str,
        memory_context: str = "",
        working_context: str = "",
        screen_size: tuple = (0, 0),
    ) -> Action:
        """根据当前状态生成下一步动作。"""
        system = render_prompt(
            "system",
            actions=describe_actions(),
            skills=self.skill_registry.describe_for_prompt() if self.skill_registry else "（无）",
        )

        prompt_parts = []
        prompt_parts.append(
            f"当前屏幕分辨率（逻辑像素）：{screen_size[0]} x {screen_size[1]}"
        )
        prompt_parts.append(
            "注意：你看到的截图可能经过等比缩放。输出坐标时请按照上述逻辑像素分辨率估算"
            "（即坐标范围 0~{screen_size[0]-1} 横向、0~{screen_size[1]-1} 纵向），"
            "不要使用 0~1 或 0~1000 的归一化坐标。"
        )
        if memory_context:
            prompt_parts.append(memory_context)
        if working_context:
            prompt_parts.append(working_context)
        prompt_parts.append(f"用户任务：{task}")
        prompt_parts.append("请观察当前屏幕截图，输出下一步动作的 JSON。")

        prompt = "\n\n".join(prompt_parts)

        req = VLMRequest(
            system=system,
            prompt=prompt,
            images=[screenshot_data_url],
            temperature=0.0,
            max_tokens=1024,
            json_mode=False,
        )

        resp = self.backend.chat(req)
        if not resp.text:
            raise ModelError("模型返回了空内容")

        try:
            return _parse_planner_output(resp.text)
        except GroundingError as e:
            raise ModelError(f"解析模型输出失败：{e}。原始输出：{resp.text[:300]}") from e


def _parse_planner_output(text: str) -> Action:
    """解析规划者输出，支持 plan 类型（带 subtasks）。"""
    text = text.strip()
    # 提取 JSON
    data: Dict[str, Any]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试提取内嵌 JSON
        import re

        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
        else:
            raise GroundingError(f"无法解析：{text[:200]}")

    # plan 动作：含 subtasks 列表
    if data.get("type") == "plan" or "subtasks" in data:
        action = Action(type="plan", reason=str(data.get("reason", "")))
        action.args["subtasks"] = data.get("subtasks", [])
        return action

    return _to_action(data)
