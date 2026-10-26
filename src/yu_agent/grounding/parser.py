"""解析模型输出的动作（JSON 或纯文本）。"""
from __future__ import annotations

from typing import Any, Dict, List

from ..actions.schema import Action
from ..common.errors import GroundingError
from ..common.jsonx import extract_all_objects, extract_json_object

# 一个"像动作"的对象至少要有这些键之一
_ACTION_KEYS = ["type", "action", "mark", "skill", "key", "x"]


def parse_action(text: str) -> Action:
    """从模型输出解析出单个 Action。

    支持两种格式：
    1. 纯 JSON：{"type": "click", "x": 100, "y": 200, ...}
    2. 文本里内嵌 JSON（模型有时会加解释文字），**允许嵌套**（args/params/position/to）
    """
    data = extract_json_object(text, required_keys=["type", "action"])
    if data is None:
        data = extract_json_object(text, required_keys=_ACTION_KEYS)
    if data is None:
        raise GroundingError(f"无法从模型输出解析动作：{(text or '')[:200]}")

    action = _to_action(data)
    if not action.type:
        # 以前这里会返回一个 type="" 的空动作，主循环拿着它走到执行器才炸，
        # 错误信息（"未知动作类型"）跟真实原因（模型输出没解析对）完全对不上。
        raise GroundingError(f"模型输出里没有动作类型：{(text or '')[:200]}")
    return action


def parse_actions(text: str) -> List[Action]:
    """解析可能包含多个动作的输出（JSON 数组或对象列表）。"""
    out: List[Action] = []
    for data in extract_all_objects(text):
        try:
            action = _to_action(data)
        except (TypeError, ValueError):
            continue
        if action.type:
            out.append(action)
    if out:
        return out
    # 没有任何带 type 的对象：交给 parse_action 给出统一报错（含原文）
    return [parse_action(text)]


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
    _to = data.get("to")
    endpoint: Dict[str, Any] = _to if isinstance(_to, dict) else {}
    x2 = data.get("x2", data.get("to_x", endpoint.get("x")))
    y2 = data.get("y2", data.get("to_y", endpoint.get("y")))

    # type 的「覆盖」语义：模型可能写在顶层（"replace": true），
    # 也可能写在 args 里。统一收进 args，executor 从那里读。
    args = dict(data.get("args", data.get("params", {})) or {})
    for key in ("replace", "clear", "clear_first", "select_all"):
        if key in data and key not in args:
            args[key] = data[key]

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
        args=args,
    )


def _opt_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (ValueError, TypeError):
        return None
