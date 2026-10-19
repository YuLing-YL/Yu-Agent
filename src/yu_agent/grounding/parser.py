"""解析模型输出的动作（JSON 或纯文本）。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from ..actions.schema import Action
from ..common.errors import GroundingError


def parse_action(text: str) -> Action:
    """从模型输出解析出单个 Action。

    支持两种格式：
    1. 纯 JSON：{"type": "click", "x": 100, "y": 200, ...}
    2. 文本里内嵌 JSON（模型有时会加解释文字）
    """
    text = text.strip()

    # 尝试直接解析整个文本为 JSON
    try:
        data = json.loads(text)
        return _to_action(data)
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试提取 JSON 对象
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return _to_action(data)
        except (json.JSONDecodeError, ValueError):
            pass

    raise GroundingError(f"无法从模型输出解析动作：{text[:200]}")


def parse_actions(text: str) -> List[Action]:
    """解析可能包含多个动作的输出（JSON 数组或对象列表）。"""
    text = text.strip()
    # 数组
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [_to_action(d) for d in data]
        if isinstance(data, dict):
            return [_to_action(data)]
    except (json.JSONDecodeError, ValueError):
        pass

    # 单个对象
    try:
        return [parse_action(text)]
    except GroundingError:
        pass

    # 提取所有 JSON 对象
    actions = []
    for m in re.finditer(r"\{[^{}]*\}", text, re.DOTALL):
        try:
            data = json.loads(m.group(0))
            actions.append(_to_action(data))
        except (json.JSONDecodeError, ValueError):
            continue
    if actions:
        return actions

    raise GroundingError(f"无法从模型输出解析动作：{text[:200]}")


def _to_action(data: Dict[str, Any]) -> Action:
    """把 dict 转成 Action，做字段归一化。"""
    # 兼容不同字段名
    if "action" in data and "type" not in data:
        data = {**data, "type": data["action"]}
    # 兼容 {x,y} 嵌套在 position 里的情况
    if "position" in data and isinstance(data["position"], dict):
        pos = data["position"]
        data.setdefault("x", pos.get("x"))
        data.setdefault("y", pos.get("y"))
    # 兼容 coordinate
    if "coordinate" in data and isinstance(data["coordinate"], list):
        data.setdefault("x", data["coordinate"][0])
        data.setdefault("y", data["coordinate"][1])

    # drag 的终点：兼容 x2/y2、to_x/to_y、以及 {"to": {"x":..,"y":..}}
    endpoint = data.get("to") if isinstance(data.get("to"), dict) else {}
    x2 = data.get("x2", data.get("to_x", endpoint.get("x")))
    y2 = data.get("y2", data.get("to_y", endpoint.get("y")))

    return Action(
        type=str(data.get("type", data.get("action", ""))).lower(),
        x=_opt_int(data.get("x")),
        y=_opt_int(data.get("y")),
        x2=_opt_int(x2),
        y2=_opt_int(y2),
        mark=_opt_int(data.get("mark", data.get("id", data.get("index")))),
        text=data.get("text"),
        key=data.get("key"),
        duration=float(data.get("duration", 0.1)),
        reason=str(data.get("reason", data.get("thought", ""))),
        skill=data.get("skill"),
        args=data.get("args", data.get("params", {})) or {},
    )


def _opt_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (ValueError, TypeError):
        return None
