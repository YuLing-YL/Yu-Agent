"""模型输出解析的回归测试。

覆盖历史上真实踩到过的坑：
1. `{"type":"skill","args":{...}}` 这种**嵌套**结构被 `\\{[^{}]*\\}` 正则切坏，
   解析出一个 type="" 的空动作且**不报错**（模型答对了，agent 却白烧一步）；
2. JSON 前后带解释文字 / ``` 包裹；
3. 回复语言：模型只回英文时，必须变成中文再给用户。
"""
from __future__ import annotations

import pytest

from yu_agent.agent.planner import _parse_planner_output
from yu_agent.agent.responder import Responder, has_chinese
from yu_agent.common.errors import GroundingError
from yu_agent.common.jsonx import extract_all_objects, extract_json_object
from yu_agent.grounding.parser import parse_action
from yu_agent.models.base import VLMResponse


# ── 1. 嵌套 JSON：本项目最常出问题的形状 ─────────────────────────── #
def test_extract_nested_args() -> None:
    text = 'Sure, I will open Safari: {"type":"skill","skill":"open_app","args":{"app_name":"Safari"}}'
    data = extract_json_object(text)
    assert data is not None
    assert data["type"] == "skill"
    assert data["args"] == {"app_name": "Safari"}


def test_parse_action_nested_skill() -> None:
    text = 'Next: {"type": "skill", "skill": "open_app", "args": {"app_name": "Safari"}}'
    action = parse_action(text)
    assert action.type == "skill"
    assert action.skill == "open_app"
    assert action.args["app_name"] == "Safari"


def test_parse_action_nested_position() -> None:
    text = '{"type": "click", "position": {"x": 12, "y": 34}, "reason": "点搜索框"}'
    action = parse_action(text)
    assert (action.x, action.y) == (12, 34)


def test_parse_action_drag_to() -> None:
    text = '{"type":"drag","x":1,"y":2,"to":{"x":30,"y":40}}'
    action = parse_action(text)
    assert (action.x2, action.y2) == (30, 40)


def test_brace_inside_string() -> None:
    text = '{"type":"type","text":"if (x) { y }","reason":"输入代码"}'
    action = parse_action(text)
    assert action.type == "type"
    assert action.text == "if (x) { y }"


def test_code_fence_wrapped() -> None:
    text = '```json\n{"type": "key", "key": "cmd+l"}\n```'
    assert parse_action(text).key == "cmd+l"


def test_empty_type_raises() -> None:
    """只有内层对象时，必须报错而不是返回空动作。"""
    with pytest.raises(GroundingError):
        parse_action('他说 {"app_name": "Safari"} 就这样')


def test_parse_multiple_objects() -> None:
    text = '{"type":"click","mark":1} 然后 {"type":"type","text":"天气"}'
    actions = extract_all_objects(text)
    assert [a["type"] for a in actions] == ["click", "type"]


def test_planner_parses_plan() -> None:
    action = _parse_planner_output('{"type":"plan","subtasks":["打开浏览器","搜索天气"]}')
    assert action.type == "plan"
    assert action.args["subtasks"] == ["打开浏览器", "搜索天气"]


def test_planner_parses_nested_skill() -> None:
    action = _parse_planner_output(
        '好的 {"type":"skill","skill":"open_app","args":{"app_name":"计算器"}}'
    )
    assert action.type == "skill"
    assert action.args["app_name"] == "计算器"


# ── 2. 回复语言：只能输出中文 ─────────────────────────────────────── #
class _StubBackend:
    """按顺序返回预设文本的假后端。"""

    name = "stub"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, req) -> VLMResponse:
        text = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return VLMResponse(text=text)


def test_reply_english_is_translated() -> None:
    backend = _StubBackend([
        '{"needs_gui": false, "reply": "Hello! I am Yu-Agent, how can I help?"}',
        "你好！我是 Yu-Agent，需要我做什么？",
    ])
    out = Responder(backend).respond("hello")
    assert out["needs_gui"] is False
    assert has_chinese(out["reply"])
    assert backend.calls == 2  # 第一次意图，第二次翻译


def test_reply_chinese_no_extra_call() -> None:
    backend = _StubBackend(['{"needs_gui": false, "reply": "你好，我是 Yu-Agent。"}'])
    out = Responder(backend).respond("你好")
    assert has_chinese(out["reply"])
    assert backend.calls == 1  # 已经是中文，不再多花一次调用


def test_reply_translate_failure_falls_back_to_chinese() -> None:
    backend = _StubBackend([
        '{"needs_gui": true, "reply": "Opening Safari now"}',
        "still english",  # 翻译也失败
    ])
    out = Responder(backend).respond("open safari")
    assert has_chinese(out["reply"]), out["reply"]
    assert out["needs_gui"] is True


def test_parse_tolerates_missing_needs_gui() -> None:
    backend = _StubBackend(['{"reply": "你好"}'])
    out = Responder(backend).respond("hi")
    assert out["reply"] == "你好"
