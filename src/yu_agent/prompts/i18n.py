"""提示词里运行期文案的多语言选择。

system.md / planner.md 走文件切换（`prompts.language`），
而主循环里动态拼的提示（"你已经点了输入框…"、"子任务计划…"）没法放进模板，
统一用这里的 `t(zh, en)` 选一份。

设计：**只翻译给模型看的文案**；给用户看的 GUI/日志文案保持中文（用户是中文用户）。
"""
from __future__ import annotations


def is_en(language: str = "") -> bool:
    lang = language or _current()
    return str(lang).strip().lower().startswith("en")


def _current() -> str:
    try:
        from .loader import prompt_language

        return prompt_language()
    except Exception:
        return "en"


def t(zh: str, en: str, language: str = "") -> str:
    """按当前提示词语言二选一。"""
    return en if is_en(language) else zh
