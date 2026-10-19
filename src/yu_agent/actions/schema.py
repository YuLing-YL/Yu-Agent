"""动作 schema：模型可返回的动作类型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Action(BaseModel):
    """模型输出的单个动作。"""

    type: str = Field(..., description="动作类型")
    x: Optional[int] = None
    y: Optional[int] = None
    x2: Optional[int] = None  # drag 的绝对目标点
    y2: Optional[int] = None
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
    "right_click": "在 (x, y) 右键单击（呼出上下文菜单）",
    "double_click": "在 (x, y) 双击（打开文件/文件夹、选中单词）",
    "middle_click": "在 (x, y) 中键单击（常用于后台新标签页打开链接）",
    "triple_click": "在 (x, y) 三击（选中整行/整段文字）",
    "type": "输入文字：把 text 打到「当前已聚焦的输入框」里。**输入文字必须先 click 点一下输入框，"
            "然后下一步用 type**；type 不需要坐标或编号，也不要点第二次输入框。支持中文。",
    "key": "按下按键/组合键，key 形如 'enter'（提交搜索）、'esc'、'tab'、'cmd+l'（浏览器地址栏）、"
           "'cmd+c'、'cmd+v'。修饰键请写 cmd / ctrl / option / shift。",
    "scroll": "滚动，x/y 为位置，text 填 'up'/'down' 或数值",
    "move": "移动鼠标到 (x, y)",
    "drag": "拖拽：从 (x, y) 拖到 (x2, y2)（绝对坐标，推荐）；也可用 args.dx/dy 表示相对位移",
    "wait": "等待 duration 秒",
    "skill": "调用已注册技能，skill 填技能名，args 填参数",
    "subtask_done": "当前子任务已完成，reason 说明；用于推进到下一个子任务",
    "done": "整个任务已完成，reason 说明结果",
}

# 英文版（prompts.language=en 时使用）
SUPPORTED_ACTIONS_EN = {
    "click": "left click at (x, y); or use `mark` to click a numbered element",
    "right_click": "right click at (x, y) (opens a context menu)",
    "double_click": "double click at (x, y) (open a file/folder, select a word)",
    "middle_click": "middle click at (x, y) (open a link in a background tab)",
    "triple_click": "triple click at (x, y) (select a whole line/paragraph)",
    "type": "type `text` into the currently focused field. You MUST click the field first, "
            "then use `type` on the next step; `type` needs no coordinates or mark. "
            "Non-ASCII text (Chinese, emoji) is supported.",
    "key": "press a key or shortcut, e.g. 'enter' (submit), 'esc', 'tab', 'cmd+l' (address bar), "
           "'cmd+c', 'cmd+v'. Modifiers: cmd / ctrl / option / shift.",
    "scroll": "scroll; x/y optional position, text is 'up'/'down' or an amount",
    "move": "move the mouse to (x, y)",
    "drag": "drag from (x, y) to (x2, y2) in absolute coordinates (preferred); "
            "relative args.dx/dy also works",
    "wait": "wait for `duration` seconds",
    "skill": "call a registered skill; put its name in `skill` and arguments in `args`",
    "subtask_done": "current subtask finished; `reason` explains what was done",
    "done": "the whole task is finished; `reason` explains the result",
}


def describe_actions(lang: str = "zh") -> str:
    """生成动作说明文本，供 prompt 使用。"""
    table = SUPPORTED_ACTIONS_EN if (lang or "").lower().startswith("en") else SUPPORTED_ACTIONS
    return "\n".join(f"- {name}: {desc}" for name, desc in table.items())
