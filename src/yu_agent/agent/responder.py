"""AI 回复模块：在调用 GUI agent 之前，先判断用户输入是否需要操作屏幕。

职责：
1. 判断用户输入是否需要调用 GUI agent（操作屏幕）
2. 生成一段面向用户的回复文字（说明将要做什么，或直接闲聊回应）

如果用户只是打招呼/闲聊/提问，则只回复文字，不调用 GUI agent。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from ..models.base import VLMRequest

# 意图判断 prompt：让模型判断是否需要 GUI 操作，并给出回复
_INTENT_SYSTEM = """你是 Yu-Agent 的智能调度助手。你会收到用户的一句话，需要判断两件事：

1. 这句话是否需要「操作电脑屏幕」（例如打开软件、点击、搜索、填表、操作界面等）来完成？
2. 用友好、简洁的中文回复用户，说明你接下来要做什么（如果需要操作），或者直接回应（如果只是闲聊/打招呼/提问）。

你必须只输出一个 JSON 对象，不要输出任何其他文字，不要用 markdown 代码块包裹，格式如下：
{"needs_gui": true, "reply": "给用户的回复文字"}

规则：
- 如果用户是打招呼、闲聊、问好、问你是谁、或提一个不需要操作电脑就能回答的问题 → needs_gui 为 false，reply 直接回答。
- 如果用户想让你操作电脑（打开应用、搜索、点击、填表、自动化某个操作等）→ needs_gui 为 true，reply 简短说明你准备怎么做（例如"好的，我来帮你打开浏览器并搜索天气"）。
- reply 要自然、口语化、简短（一般不超过 50 字），不要复述动作细节。
- needs_gui 只能是 true 或 false 两个值。
"""

# 判断是否包含「需要操作屏幕」的动作词（解析失败时的启发式兜底）
_ACTION_HINTS = [
    "打开", "搜索", "点击", "输入", "填", "操作", "下载", "安装",
    "设置", "切换", "保存", "发送", "截屏", "截图", "登录", "帮我",
    "open", "search", "click", "type", "帮我打开",
]


class Responder:
    """意图识别 + 回复。"""

    def __init__(self, backend):
        self.backend = backend

    def respond(self, user_input: str) -> Dict[str, Any]:
        """判断意图并生成回复。返回 {"needs_gui": bool, "reply": str}。"""
        req = VLMRequest(
            system=_INTENT_SYSTEM,
            prompt=f"用户说：{user_input}",
            temperature=0.0,
            max_tokens=256,
            json_mode=False,
        )
        resp = self.backend.chat(req)
        text = resp.text.strip()

        # 解析 JSON
        data = self._parse(text)
        if data is not None:
            needs_gui = bool(data.get("needs_gui", True))
            reply = str(data.get("reply", "")).strip()
            if not reply:
                reply = "好的，我来帮你处理。" if needs_gui else "你好！"
            return {"needs_gui": needs_gui, "reply": reply}

        # 解析失败：把原始文本当作回复，用启发式判断是否需要 GUI
        if not text:
            # 模型完全无输出，保守地当作需要 GUI 并继续执行
            return {"needs_gui": True, "reply": "好的，我来帮你处理。"}
        reply = text[:200].strip()
        needs_gui = self._guess_needs_gui(user_input, reply)
        return {"needs_gui": needs_gui, "reply": reply}

    @staticmethod
    def _guess_needs_gui(user_input: str, reply: str) -> bool:
        """解析失败时的启发式：用户输入或模型回复含动作词 → 需要 GUI。"""
        combined = user_input + reply
        return any(hint in combined for hint in _ACTION_HINTS)

    @staticmethod
    def _parse(text: str) -> Optional[Dict[str, Any]]:
        text = text.strip()
        # 剥离 markdown 代码块标记
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return None
