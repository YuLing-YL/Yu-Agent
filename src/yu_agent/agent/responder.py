"""AI 回复模块：在调用 GUI agent 之前，先判断用户输入是否需要操作屏幕。

职责：
1. 判断用户输入是否需要调用 GUI agent（操作屏幕）
2. 生成一段面向用户的回复文字（说明将要做什么，或直接闲聊回应）

如果用户只是打招呼/闲聊/提问，则只回复文字，不调用 GUI agent。

⚠️ 回复语言：**一律简体中文**
------------------------------
提示词默认是英文（`prompts.language: en`，指令遵循更稳），小模型很容易把回复也写成英文，
用户看到的就是一句英文寒暄。这里三层保证：
1. 系统提示里把「reply 必须简体中文」写成硬性要求 + 三个 few-shot；
2. 返回前检测：整句没有任何汉字 → 追加一次极短的「译成中文」调用（只在异常时发生）；
3. 最后兜底：仍拿不到中文就用现成的中文模板，绝不给用户丢一句英文。

⚠️ 这里有一个必须守住的边界
----------------------------
本模块发给模型的请求**不带截图**（纯文本），所以模型对「你能看到屏幕吗」这类问题
只能回答「我看不到」。如果把这种回答当成「不需要操作屏幕」，就会出现：
用户想验证 agent 能不能看到屏幕 → agent 回答一句「我看不到」→ 直接结束，一步都没跑。

因此凡是涉及**屏幕 / 界面 / 看**的输入，一律强制走 GUI：
主循环会截图，模型届时是真的能看到，回答才有意义。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from ..common.jsonx import extract_json_object
from ..common.logging import get_logger
from ..models.base import VLMRequest

log = get_logger("yu_agent.responder")

# 意图判断 prompt：让模型判断是否需要 GUI 操作，并给出回复
_INTENT_SYSTEM = """你是 Yu-Agent 的智能调度助手。你会收到用户的一句话，需要判断两件事：

1. 这句话是否需要「操作电脑屏幕」来完成？
2. 用友好、简洁的**简体中文**回复用户，说明你接下来要做什么（如果需要操作），或者直接回应（如果只是闲聊/打招呼/提问）。

【回复语言：硬性要求】
- reply 字段**必须是简体中文**。即使你用英文思考、即使提示词/用户输入是英文，也要用中文回复。
- 只有专有名词（应用名、文件名、菜单项、命令、链接）保留原文，例如「我来打开 Safari 搜索天气」。
- 禁止用英文整句；不要写成中英混杂的英文句式。

示例（注意 reply 全是中文）：
用户：hello → {"needs_gui": false, "reply": "你好！我是 Yu-Agent，可以帮你操作电脑，需要我做什么？"}
用户：open safari and search for weather → {"needs_gui": true, "reply": "好的，我先打开 Safari，再搜索天气。"}
用户：你是谁 → {"needs_gui": false, "reply": "我是 Yu-Agent，一个能帮你在电脑上点击、输入、执行任务的助手。"}

你必须只输出一个 JSON 对象，不要输出任何其他文字，不要用 markdown 代码块包裹，格式如下：
{"needs_gui": true, "reply": "给用户的回复文字"}

规则：
- 如果用户想让你操作电脑（打开应用、搜索、点击、填表、自动化某个操作等）→ needs_gui 为 true，reply 简短说明你准备怎么做。
- **如果用户问的是屏幕相关的事**（你能看到屏幕吗 / 屏幕上有什么 / 帮我看看界面 / 现在是什么界面 / 截图看一下）→ needs_gui 必须为 true。你此刻没有截图，回答不了；真正的执行器会截屏后回答。**绝对不要**因为"我看不到屏幕"就判 false。
- 只有纯粹的问候、闲聊、常识问答（例如"现在几点""你是谁""1+1 等于几"）→ needs_gui 为 false，reply 直接回答。
- reply 要自然、口语化、简短（一般不超过 50 字），不要复述动作细节。
- needs_gui 只能是 true 或 false 两个值。
"""

# 兜底翻译用：只做一件事，避免小模型又自由发挥
_TRANSLATE_SYSTEM = """把用户给的一句话翻译成简体中文，只输出译文本身：
不要解释、不要引号、不要保留英文原句。专有名词（应用名/文件名/命令）可以保留原文。"""

# 判断是否包含汉字（东亚汉字区，覆盖简体/繁体）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 判断是否包含「需要操作屏幕」的动作词（解析失败时的启发式兜底）
_ACTION_HINTS = [
    "打开", "搜索", "点击", "输入", "填", "操作", "下载", "安装",
    "设置", "切换", "保存", "发送", "截屏", "截图", "登录", "帮我",
    "open", "search", "click", "type", "帮我打开",
]

# 「看屏幕」类词：命中就强制走 GUI。
# 依据：本模块的请求不带图，模型对这些词只能回答"我看不到"，让模型判就是误判。
_SCREEN_HINTS = [
    "屏幕", "桌面", "界面", "窗口", "画面", "截图", "截屏",
    "你能看到", "你能看见", "看得到", "看得见", "看到什么", "看到了什么",
    "看到啥", "看一下", "看看", "当前界面", "现在的界面", "这个页面",
    "screen", "screenshot", "see my", "look at", "what's on",
]

# 模型在纯文本请求下的典型「我做不到」表述：强制执行时要换掉，避免界面自相矛盾
_NEGATIVE_REPLIES = (
    "看不到", "看不见", "无法看到", "不能看到", "没有屏幕", "无法查看",
    "cannot see", "can't see", "unable to see", "no access to your screen",
)

# 中文兜底模板（翻译也失败时用，宁可普通也别丢英文给用户）
_FALLBACK_REPLY_RUN = "好的，我来帮你处理。"
_FALLBACK_REPLY_CHAT = "你好！我是 Yu-Agent，可以帮你操作电脑，需要我做什么？"


def has_chinese(text: str) -> bool:
    """文本里是否含汉字。"""
    return bool(_CJK_RE.search(text or ""))


class Responder:
    """意图识别 + 回复。"""

    def __init__(self, backend):
        self.backend = backend

    def respond(self, user_input: str) -> Dict[str, Any]:
        """判断意图并生成回复。返回 {"needs_gui": bool, "reply": str}。"""
        # 硬规则先算：命中「看屏幕」或「动作」词 → 无论如何都要执行。
        # 模型仍然负责生成那句友好回复，但它的 needs_gui 判断会被覆盖，
        # 因为纯文本请求下模型对"你能看到屏幕吗"只能答"看不到"（2026-10-21 实测踩到）。
        forced = self._must_run_gui(user_input)

        req = VLMRequest(
            system=_INTENT_SYSTEM,
            prompt=f"用户说：{user_input}",
            temperature=0.0,
            max_tokens=256,
            json_mode=False,
        )
        resp = self.backend.chat(req)
        text = (resp.text or "").strip()

        # 解析 JSON
        data = self._parse(text)
        if data is not None:
            model_says = bool(data.get("needs_gui", True))
            reply = str(data.get("reply", "")).strip()
            if not reply:
                reply = _FALLBACK_REPLY_RUN if (model_says or forced) else _FALLBACK_REPLY_CHAT
            needs_gui = model_says or forced
            if forced and not model_says:
                log.warning(
                    "意图识别把「%s」判成了无需操作，已按硬规则纠正为执行"
                    "（模型原话：%s）", user_input, reply[:60],
                )
            # 强制执行的场景下，模型那句「我看不到你的屏幕」会误导用户——
            # 界面上会显示这句话，但实际马上要截屏执行，所以换成不矛盾的说法。
            if forced and any(x in reply.lower() for x in _NEGATIVE_REPLIES):
                reply = "好的，我先截屏看一下当前界面。"
            return {"needs_gui": needs_gui, "reply": self._ensure_chinese(reply, needs_gui)}

        # 解析失败：把原始文本当作回复，用启发式判断是否需要 GUI
        if not text:
            # 模型完全无输出，保守地当作需要 GUI 并继续执行
            return {"needs_gui": True, "reply": _FALLBACK_REPLY_RUN}
        reply = text[:200].strip()
        needs_gui = forced or self._guess_needs_gui(user_input, reply)
        return {"needs_gui": needs_gui, "reply": self._ensure_chinese(reply, needs_gui)}

    # ------------------------------------------------------------------ #
    # 回复语言兜底
    # ------------------------------------------------------------------ #
    def _ensure_chinese(self, reply: str, needs_gui: bool) -> str:
        """确保给用户看的回复是中文。

        只有「整句没有一个汉字」时才多花一次调用去译 —— 正常中文回复零开销。
        """
        if not reply or has_chinese(reply):
            return reply
        translated = self._translate(reply)
        if translated:
            return translated
        # 翻译失败：用现成中文模板，绝不给用户丢英文
        log.warning("回复无法转成中文，已用模板兜底（原文：%s）", reply[:60])
        return _FALLBACK_REPLY_RUN if needs_gui else _FALLBACK_REPLY_CHAT

    def _translate(self, text: str) -> str:
        """把一句英文译成中文；失败返回空串。"""
        try:
            resp = self.backend.chat(
                VLMRequest(
                    system=_TRANSLATE_SYSTEM,
                    prompt=text,
                    temperature=0.0,
                    max_tokens=300,
                )
            )
            out = (resp.text or "").strip().strip('"').strip()
            return out if has_chinese(out) else ""
        except Exception as e:  # 翻译是锦上添花，失败不能影响主流程
            log.warning("回复中文化失败（忽略）：%s", e)
            return ""

    @staticmethod
    def _must_run_gui(user_input: str) -> bool:
        """输入是否**必须**走 GUI（不看模型脸色）。

        两类命中即执行：
        1. 屏幕/界面/看 —— 只有截图之后才答得出来；
        2. 明确动作词（打开/搜索/点击…）—— 用户就是要干活。
        """
        text = (user_input or "").lower()
        if any(h.lower() in text for h in _SCREEN_HINTS):
            return True
        return any(h.lower() in text for h in _ACTION_HINTS)

    @staticmethod
    def _guess_needs_gui(user_input: str, reply: str) -> bool:
        """解析失败时的启发式：用户输入或模型回复含动作词 → 需要 GUI。"""
        combined = user_input + reply
        return any(hint in combined for hint in _ACTION_HINTS + _SCREEN_HINTS)

    @staticmethod
    def _parse(text: str) -> Optional[Dict[str, Any]]:
        """从模型输出里取 JSON（统一走 common/jsonx，支持嵌套与代码块包裹）。"""
        return extract_json_object(text, required_keys=["needs_gui", "reply"])
