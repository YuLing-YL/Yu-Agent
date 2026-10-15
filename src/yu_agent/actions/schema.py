"""动作 schema：模型可返回的动作类型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Action(BaseModel):
    """模型输出的单个动作。"""

    type: str = Field(..., description="动作类型")
    x: Optional[int] = None
    y: Optional[int] = None
    mark: Optional[int] = None  # SOM 标记编号（优先于 x/y 使用）
    text: Optional[str] = None
    key: Optional[str] = None
    duration: float = 0.1
    reason: str = ""
    skill: Optional[str] = None  # 若调用技能，填技能名
    args: Dict[str, Any] = Field(default_factory=dict)  # 技能参数

    @property
    def is_done(self) -> bool:
        return self.type in ("done", "finish", "finished")


# 支持的动作类型（用于 prompt 约束与校验）
SUPPORTED_ACTIONS = {
    "click": "在 (x, y) 左键单击；若截图有编号框，可用 mark 指定编号",
    "double_click": "在 (x, y) 双击",
    "right_click": "在 (x, y) 右键",
    "type": "输入文字：把 text 打到「当前已聚焦的输入框」里。**输入文字必须先 click 点一下输入框，"
            "然后下一步用 type**；type 不需要坐标或编号，也不要点第二次输入框。支持中文。",
    "key": "按下按键/组合键，key 形如 'enter'（提交搜索）、'esc'、'tab'、'cmd+l'（浏览器地址栏）、"
           "'cmd+c'、'cmd+v'。修饰键请写 cmd / ctrl / option / shift。",
    "scroll": "滚动，x/y 为位置，text 填 'up'/'down' 或数值",
    "move": "移动鼠标到 (x, y)",
    "drag": "从 (x, y) 拖到 args.dx/dy",
    "wait": "等待 duration 秒",
    "skill": "调用已注册技能，skill 填技能名，args 填参数",
    "subtask_done": "当前子任务已完成，reason 说明；用于推进到下一个子任务",
    "done": "整个任务已完成，reason 说明结果",
}


def describe_actions() -> str:
    """生成动作说明文本，供 prompt 使用。"""
    lines = []
    for name, desc in SUPPORTED_ACTIONS.items():
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)
