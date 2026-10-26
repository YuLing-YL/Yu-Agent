"""把动作与界面翻译成「人话」，供工作记忆与日志使用。

单独成文件的原因：这类文案会随语言、动作类型不断长（现在 12 种动作 × 中英），
留在 loop.py 里既挤占主流程，也没法单独测试。

两条约定：
- 动作写成**模型能复述的样子**：「点击了『搜索框』」，而不是 `click(513,127)`——
  下一步要判断「这个动作是不是做过了」，坐标是还原不出元素的；
- 界面只写**对下一步有用**的信息（前台是谁、多少可点元素），不写元素清单：
  清单会随截图变化，写进历史反而让模型拿旧信息做判断。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

_CLICK_VERBS_ZH = {
    "click": "点击", "double_click": "双击", "right_click": "右键点击",
    "middle_click": "中键点击", "triple_click": "三击",
}
_CLICK_VERBS_EN = {
    "click": "clicked", "double_click": "double-clicked",
    "right_click": "right-clicked", "middle_click": "middle-clicked",
    "triple_click": "triple-clicked",
}


def describe_action(
    action: Any,
    marks_map: Optional[Dict[int, Any]] = None,
    language: str = "",
) -> str:
    """把一步动作翻译成一句话。"""
    en = str(language).lower().startswith("en")
    t = str(getattr(action, "type", "") or "")

    if t in _CLICK_VERBS_ZH:
        el = None
        mark = getattr(action, "mark", None)
        if marks_map and mark is not None:
            el = marks_map.get(mark)
        name = str(getattr(el, "title", "") or "") if el is not None else ""
        if en:
            verb = _CLICK_VERBS_EN.get(t, "clicked")
            if name:
                return f'{verb} "{name}"'
            if mark is not None:
                return f"{verb} an unlabeled element"
            return f"{verb} ({getattr(action, 'x', '?')},{getattr(action, 'y', '?')})"
        verb = _CLICK_VERBS_ZH.get(t, "点击")
        if name:
            return f"{verb}「{name}」"
        if mark is not None:
            return f"{verb}了一个无标签元素"
        return f"{verb} ({getattr(action, 'x', '?')},{getattr(action, 'y', '?')})"

    if t == "type":
        txt = (getattr(action, "text", "") or "")[:24]
        return f'typed "{txt}"' if en else f"输入「{txt}」"
    if t == "key":
        return f"pressed {action.key}" if en else f"按了 {action.key}"
    if t == "scroll":
        body = getattr(action, "text", "") or ""
        return (f"scrolled {body}".strip() if en else f"滚动 {body}".strip())
    if t == "skill":
        return f"called skill {action.skill}" if en else f"调用技能 {action.skill}"
    if t == "drag":
        return "dragged" if en else "拖拽"
    if t == "wait":
        return "waited" if en else "等待"
    if t == "ask":
        return "asked the user" if en else "向用户提问"
    if t == "plan":
        return "replanned" if en else "重新规划"
    if t in ("done", "finish", "subtask_done"):
        return "finished" if en else "结束任务"
    return t


def describe_screen(a11y_stats: Dict[str, Any], en: bool = False) -> str:
    """一句话说清「操作之前屏幕长什么样」。"""
    apps = a11y_stats.get("apps") or []
    if not apps:
        return ""
    first = apps[0] or {}
    name = str(first.get("name") or "")
    total = sum(int(a.get("elements") or 0) for a in apps)
    if not name:
        return ""
    if en:
        if len(apps) > 1:
            return f"{name} +{len(apps) - 1} more app(s), {total} clickable elements"
        return f"{name}, {total} clickable elements"
    if len(apps) > 1:
        return f"{name} 等 {len(apps)} 个应用，{total} 个可点元素"
    return f"{name}，{total} 个可点元素"
