"""VLM 后端协议与数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Protocol


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class VLMRequest:
    """一次模型请求：system + 文本 + 可选多张截图。"""

    system: str = ""
    prompt: str = ""
    images: List[str] = field(default_factory=list)  # data URL 列表
    temperature: float = 0.0
    max_tokens: int = 2048
    json_mode: bool = False


@dataclass
class VLMResponse:
    text: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: Any = None
    model: str = ""
    latency_s: float = 0.0


class VLMBackend(Protocol):
    """VLM 后端接口。"""

    name: str

    def chat(self, req: VLMRequest) -> VLMResponse: ...


def make_backend(config: Any) -> VLMBackend:
    """根据 ModelConfig 构造后端（工厂函数，避免循环导入）。"""
    from .openai_vlm import OpenAICompatBackend

    return OpenAICompatBackend(config)
