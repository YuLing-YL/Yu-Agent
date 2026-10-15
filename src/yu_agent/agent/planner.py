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
        marks: Optional[Dict[int, Any]] = None,
        marks_desc: str = "",
    ) -> Action:
        """根据当前状态生成下一步动作。

        marks: 编号 -> 元素映射（SOM 标记），非空时引导模型输出编号而非像素坐标。
        """
        system = render_prompt(
            "system",
            actions=describe_actions(),
            skills=self.skill_registry.describe_for_prompt() if self.skill_registry else "（无）",
        )

        prompt_parts = []
        prompt_parts.append(
            f"当前屏幕分辨率（逻辑像素）：{screen_size[0]} x {screen_size[1]}"
        )

        if marks:
            # SOM 模式：截图上有编号框，让模型优先输出编号
            prompt_parts.append(
                f"重要：截图上的可点击元素已用红色框标出，并在左上角标注了编号。"
                f"请直接点击对应的编号，输出格式："
                f'{{"type": "click", "mark": 编号, "reason": "..."}}。\n'
                f"元素编号对照表：\n{marks_desc}\n"
                f"如果你要点的目标不在编号列表里（例如需要先打开某个应用、或点击空白处），"
                f"再使用像素坐标 x/y（范围 0~{screen_size[0] - 1} 横向、0~{screen_size[1] - 1} 纵向）。"
            )
        else:
            prompt_parts.append(
                f"重要：你看到的截图与屏幕是 1:1 对应的，没有缩放。"
                f"请直接输出目标元素在截图中的像素坐标："
                f"x 是横坐标（从左到右，范围 0~{screen_size[0] - 1}），"
                f"y 是纵坐标（从上到下，范围 0~{screen_size[1] - 1}）。"
                f"例如点击屏幕正中央，应输出 x={screen_size[0] // 2}, y={screen_size[1] // 2}。"
                f"不要使用 0~1 或 0~1000 的归一化坐标。"
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
