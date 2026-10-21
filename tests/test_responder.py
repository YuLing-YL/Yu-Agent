"""意图识别（responder）回归测试。

背景：2026-10-21 用户实测踩到——
输入「你能看到我的屏幕吗」时，AgentWorker 的意图识别判定 needs_gui=false，
直接回了一句「我看不到你的屏幕哦」就结束，一步都没执行。
根因：responder 发的是**纯文本请求**（不带截图），模型对这类问题只能回答"看不到"；
把这个回答当成「不需要操作」就完全错了。

修复原则：涉及屏幕/界面/看的输入，一律强制走 GUI（主循环会截图，模型届时真的能看到）。
本文件把这个约定钉死，防止以后有人"优化"掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from yu_agent.agent.responder import Responder  # noqa: E402
from yu_agent.models.base import VLMResponse  # noqa: E402


class FakeBackend:
    """固定返回一段文本的假后端，不碰真实模型。"""

    name = "fake"

    def __init__(self, text: str):
        self.text = text
        self.calls = 0
        self.last_prompt = ""

    def chat(self, req):
        self.calls += 1
        self.last_prompt = req.prompt
        return VLMResponse(text=self.text)


def respond(user_input: str, model_text: str):
    backend = FakeBackend(model_text)
    out = Responder(backend).respond(user_input)
    return out, backend


def test_screen_question_forced_true() -> None:
    """模型说"我看不到屏幕"，也必须纠正为执行。"""
    out, _ = respond(
        "你能看到我的屏幕吗",
        '{"needs_gui": false, "reply": "我看不到你的屏幕哦，我是通过文字和你交流的。"}',
    )
    assert out["needs_gui"] is True, out
    assert "看不到" not in out["reply"], f"回复会误导用户：{out['reply']}"
    assert "截屏" in out["reply"] or "看一下" in out["reply"], out["reply"]
    print(f"✓ 屏幕类问题强制执行，回复已纠正：{out['reply']}")


def test_screen_keywords_variants() -> None:
    """各种'看屏幕'的说法都要命中硬规则。"""
    cases = [
        "现在屏幕上有什么",
        "帮我看看现在是什么界面",
        "看一下桌面",
        "截个图给我看看",
        "what's on my screen",
    ]
    for text in cases:
        out, _ = respond(text, '{"needs_gui": false, "reply": "我不知道"}')
        assert out["needs_gui"] is True, f"{text} 应强制执行，实际 {out}"
    print(f"✓ {len(cases)} 种'看屏幕'说法都强制执行")


def test_pure_chat_still_skipped() -> None:
    """纯闲聊不该被执行（保留原本的对话能力）。"""
    for text, model in [
        ("你好", '{"needs_gui": false, "reply": "你好呀！"}'),
        ("现在几点", '{"needs_gui": false, "reply": "现在是十点。"}'),
        ("你是谁", '{"needs_gui": false, "reply": "我是 Yu-Agent。"}'),
    ]:
        out, _ = respond(text, model)
        assert out["needs_gui"] is False, f"{text} 不该执行，实际 {out}"
    print("✓ 纯闲聊仍然只回复、不执行")


def test_action_words_forced_true() -> None:
    """带动作词的输入，即使模型判 false 也要执行。"""
    for text in ["打开浏览器", "搜索 python tutorial", "帮我新建文件夹"]:
        out, _ = respond(text, '{"needs_gui": false, "reply": "好的"}')
        assert out["needs_gui"] is True, f"{text} 应强制执行，实际 {out}"
    print("✓ 动作词命中时强制执行")


def test_parse_failure_uses_hints() -> None:
    """模型没按 JSON 输出时，启发式兜底不能把动作任务判成闲聊。"""
    out, _ = respond("打开访达看看", "好的，我这就去做。")
    assert out["needs_gui"] is True, out

    out2, _ = respond("今天天气怎么样", "今天多云。")
    assert out2["needs_gui"] is False, out2
    print("✓ 解析失败时启发式判断正确")


def test_empty_output_conservative() -> None:
    """模型完全无输出 → 保守地执行。"""
    out, _ = respond("某个任务", "")
    assert out["needs_gui"] is True, out
    print("✓ 模型无输出时保守执行")


def test_intent_prompt_tells_model_not_to_say_cannot_see() -> None:
    """意图识别的 system prompt 里必须写明"屏幕类问题要判 true"。"""
    from yu_agent.agent.responder import _INTENT_SYSTEM

    assert "屏幕" in _INTENT_SYSTEM
    assert "true" in _INTENT_SYSTEM
    print("✓ 意图提示词已包含屏幕类问题的规则")


def test_look_only_section_in_prompts() -> None:
    """系统提示词要教会模型：看看类任务用 done 回答，不要点。"""
    from yu_agent.prompts.loader import load_prompt

    en = load_prompt("system", "en")
    zh = load_prompt("system", "zh")
    assert "Look-only questions" in en, "英文提示词缺少 look-only 章节"
    assert "只是「看看」的问题" in zh, "中文提示词缺少看看类章节"
    for text in (en, zh):
        assert '"type": "done"' in text
    print("✓ 中英文提示词都包含'只回答不点击'的规则")


if __name__ == "__main__":
    test_screen_question_forced_true()
    test_screen_keywords_variants()
    test_pure_chat_still_skipped()
    test_action_words_forced_true()
    test_parse_failure_uses_hints()
    test_empty_output_conservative()
    test_intent_prompt_tells_model_not_to_say_cannot_see()
    test_look_only_section_in_prompts()
    print("\nALL PASS")
