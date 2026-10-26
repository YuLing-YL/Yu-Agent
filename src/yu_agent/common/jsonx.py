"""从模型输出里抠 JSON —— 全项目唯一实现。

为什么要单独一个模块：这套逻辑原来在四个地方各写一遍
（`grounding/parser.py`、`agent/planner.py`、`agent/responder.py`、`memory/distillation.py`），
其中两处用的是 `\\{[^{}]*\\}` 这种正则，**匹配不了嵌套对象**：

    "Sure, I'll open Safari: {"type":"skill","args":{"app_name":"Safari"}}"
      → 正则先命中内层 {"app_name":"Safari"} → 解析出 type="" 的动作，且不报错

带 args / params / position / to 的动作（skill、drag、坐标嵌套）全是这种形状，
于是"模型明明答对了，agent 却拿了个空动作、白烧一步"。

正确做法只有一种：按**大括号配对**切，配对时跳过字符串里的括号和转义。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Optional

# ```json ... ``` 包裹（模型很爱加）
_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$", re.MULTILINE)


def strip_code_fence(text: str) -> str:
    """去掉 markdown 代码块围栏，以及首尾空白。"""
    return _FENCE_RE.sub("", text or "").strip()


def _match_brace(text: str, start: int) -> Optional[int]:
    """从 text[start] == '{' 起找到与它配对的 '}'，返回其下标。

    跳过字符串字面量（双引号/单引号）里的括号，并处理反斜杠转义——
    否则 `{"text": "a } b"}` 会在中间的 '}' 处提前结束。
    """
    depth = 0
    quote = ""
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _object_spans(text: str) -> Iterator[tuple]:
    """依次产出所有「顶层」JSON 对象片段（跳过嵌套在别的对象里的）。"""
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        end = _match_brace(text, i)
        if end is None:
            i += 1
            continue
        yield i, end
        i = end + 1


def iter_json_objects(text: str) -> Iterator[Dict[str, Any]]:
    """产出文本里所有可解析的 JSON 对象（含嵌套结构）。"""
    src = strip_code_fence(text)
    if not src:
        return
    # 整段就是 JSON：先按整体解析（最快，也支持嵌套）
    try:
        data = json.loads(src)
        if isinstance(data, dict):
            yield data
            return
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item
            return
    except (json.JSONDecodeError, ValueError):
        pass
    # 文本里夹杂解释：按大括号配对逐个尝试
    for start, end in _object_spans(src):
        try:
            data = json.loads(src[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            yield data


def extract_json_object(
    text: str,
    required_keys: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """取第一个 JSON 对象；required_keys 给了就要求它包含其中**任意**一个键。

    加 required_keys 是为了避开"解释文字里附带的小对象"：
    例如模型先写 `{"note":"..."}` 再写真正的动作，只要要求 `type` 就能选对。
    """
    for data in iter_json_objects(text):
        if not required_keys:
            return data
        if any(k in data for k in required_keys):
            return data
    return None


def extract_all_objects(text: str) -> List[Dict[str, Any]]:
    """取文本里所有 JSON 对象（多动作输出时用）。"""
    return list(iter_json_objects(text))
