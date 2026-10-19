"""规划者：根据观察 + 记忆 + 上下文，决定下一步动作。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..actions.schema import Action, describe_actions
from ..common.errors import GroundingError, ModelError
from ..grounding.parser import _to_action
from ..models.base import VLMRequest
from ..prompts.loader import prompt_language, render_prompt
from ..skills.registry import SkillRegistry


class Planner:
    """调用模型，输出下一步动作。"""

    def __init__(self, backend, skill_registry: Optional[SkillRegistry] = None):
        self.backend = backend
        self.skill_registry = skill_registry
        # 最近一次模型的原始输出（供排障日志使用）
        self.last_raw_text: str = ""

    def plan(
        self,
        task: str,
        screenshot_data_url: str,
        memory_context: str = "",
        working_context: str = "",
        screen_size: tuple = (0, 0),
        marks: Optional[Dict[int, Any]] = None,
        marks_desc: str = "",
        extra_hint: str = "",
        language: str = "",
    ) -> Action:
        """根据当前状态生成下一步动作。

        marks: 编号 -> 元素映射（SOM 标记），非空时引导模型输出编号而非像素坐标。
        extra_hint: 运行期进度提示（最近动作 / 重复警告），放在提示词末尾以强化约束。
        """
        lang = language or prompt_language()
        en = lang.lower().startswith("en")
        system = render_prompt(
            "system",
            language=lang,
            actions=describe_actions(lang),
            skills=self.skill_registry.describe_for_prompt() if self.skill_registry else "（无）",
        )

        prompt_parts = []
        if en:
            prompt_parts.append(
                f"Screen size (logical pixels): {screen_size[0]} x {screen_size[1]}"
            )
        else:
            prompt_parts.append(
                f"当前屏幕分辨率（逻辑像素）：{screen_size[0]} x {screen_size[1]}"
            )

        if marks:
            # SOM 模式：截图上有编号框，**必须**用编号，避免模型自己估坐标估错
            has_dock = any(
                getattr(el, "role", "") in ("程序坞图标",) or getattr(el, "kind", "") == "dock"
                for el in marks.values()
            )
            if en:
                dock_note = (
                    "\nNote: entries labelled '程序坞图标' (dock icon) are app icons in the dock at the "
                    "bottom of the screen. Click one ONLY to open/switch to that app. If the target app "
                    "is already open on screen, never click its dock icon."
                ) if has_dock else ""
                prompt_parts.append(
                    "Important: clickable elements in the screenshot are outlined with **red boxes** "
                    "carrying a number in the top-left corner.\n"
                    "**If the element you want is in the list below, you MUST use `mark`, never pixel "
                    "coordinates** — your own coordinate estimates are always off.\n"
                    'Format: {"type": "click", "mark": <number>, "reason": "..."}\n'
                    f"Element list:\n{marks_desc}\n"
                    "Use pixel x/y (x: 0~%d, y: 0~%d) ONLY when the target is genuinely not in the "
                    "list (e.g. clicking empty space)." % (screen_size[0] - 1, screen_size[1] - 1)
                    + dock_note
                )
            else:
                dock_note = (
                    "\n注意：编号表里带「程序坞图标」的是屏幕最底部程序坞里的应用图标，"
                    "**只在你需要打开某个应用时**才点它。"
                    "如果目标应用已经打开并在屏幕上，绝对不要再点程序坞图标。"
                ) if has_dock else ""
                prompt_parts.append(
                    f"重要：截图上的可点击元素已用红色框标出，并在框的左上角标注了编号。\n"
                    f"**只要你要点的目标在下面的编号表里，就必须用编号，禁止用像素坐标**——"
                    f"你自己估算的坐标一定不准。输出格式："
                    f'{{"type": "click", "mark": 编号, "reason": "..."}}。\n'
                    f"元素编号对照表：\n{marks_desc}\n"
                    f"只有当目标**确实不在编号表里**（例如点击空白处、拖动滚动条）时，"
                    f"才退而使用像素坐标 x/y（范围 0~{screen_size[0] - 1} 横向、"
                    f"0~{screen_size[1] - 1} 纵向）。"
                    + dock_note
                )
        else:
            if en:
                prompt_parts.append(
                    "Important: the screenshot maps 1:1 to the screen, no scaling. "
                    "Output the pixel coordinates of the target in the screenshot: "
                    f"x horizontal (0~{screen_size[0] - 1}), y vertical (0~{screen_size[1] - 1}). "
                    f"For example the screen center is x={screen_size[0] // 2}, y={screen_size[1] // 2}. "
                    "Never use 0~1 or 0~1000 normalized coordinates."
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
        prompt_parts.append(f"Task: {task}" if en else f"用户任务：{task}")
        if extra_hint:
            # 放在任务之后、收尾指令之前，让模型最后看到「别重复」的强约束
            prompt_parts.append(extra_hint)
        prompt_parts.append(
            "Look at the screenshot and output the JSON of the next action."
            if en else "请观察当前屏幕截图，输出下一步动作的 JSON。"
        )

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
        self.last_raw_text = resp.text or ""
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
