"""Prompt 加载：从包内 prompts 目录读取模板并做变量替换。"""
from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """加载 prompt 模板（如 'system'、'planner'）。"""
    p = _PACKAGE_DIR / f"{name}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def render_prompt(name: str, **kwargs: str) -> str:
    """加载并做变量替换。模板里用 {var} 占位。"""
    template = load_prompt(name)
    for k, v in kwargs.items():
        template = template.replace("{" + k + "}", v)
    return template
