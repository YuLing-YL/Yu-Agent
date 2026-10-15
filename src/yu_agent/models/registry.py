"""后端注册表：provider 名 -> 后端工厂。"""
from __future__ import annotations

from typing import Any, Callable

from .base import VLMBackend
from .mock import MockBackend
from .openai_vlm import OpenAICompatBackend


def get_backend(config: Any, provider: str = "openai") -> VLMBackend:
    """根据 provider 构造后端。当前默认走 OpenAI 兼容接口。"""
    if provider == "mock":
        return MockBackend(config)
    # openai / openai-compat / 以及一切 OpenAI 兼容端点统一走这里
    return OpenAICompatBackend(config)
