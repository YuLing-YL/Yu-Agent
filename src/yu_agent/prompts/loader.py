"""Prompt 加载：从包内 prompts 目录读取模板并做变量替换。

支持多语言：`config/default.yaml` 的 `prompts.language`
- `en` → 优先加载 `system.en.md`（默认）
- `zh` → 加载 `system.md`
找不到对应语言文件时回退到 `system.md`，保证永远有东西可用。
"""
from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent

# 没配置时的默认语言：英文提示词通常更省 token、指令遵循更稳
DEFAULT_LANGUAGE = "en"


def prompt_language() -> str:
    """当前提示词语言（来自配置，取不到就回退默认值）。"""
    try:
        from ..settings import get_settings

        lang = get_settings().prompts.get("language", DEFAULT_LANGUAGE)
        return str(lang or DEFAULT_LANGUAGE).strip().lower()
    except Exception:
        return DEFAULT_LANGUAGE


def candidate_files(name: str, language: str = "") -> list:
    """按优先级列出要尝试的文件名。"""
    lang = (language or prompt_language()).strip().lower()
    out = []
    if lang and lang != "zh":
        out.append(f"{name}.{lang}.md")
    out.append(f"{name}.md")
    return out


def load_prompt(name: str, language: str = "") -> str:
    """加载 prompt 模板（如 'system'、'planner'），按语言选文件。"""
    for fname in candidate_files(name, language):
        p = _PACKAGE_DIR / fname
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def render_prompt(name: str, language: str = "", **kwargs: str) -> str:
    """加载并做变量替换。模板里用 {var} 占位。"""
    template = load_prompt(name, language)
    for k, v in kwargs.items():
        template = template.replace("{" + k + "}", v)
    return template


def prompt_file(name: str, language: str = "") -> str:
    """实际使用的文件名（排障用）。"""
    for fname in candidate_files(name, language):
        if (_PACKAGE_DIR / fname).exists():
            return fname
    return ""
