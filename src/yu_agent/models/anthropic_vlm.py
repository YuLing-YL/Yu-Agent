"""Anthropic Claude 后端。

Claude 原生 Messages API 与 OpenAI 格式不同；但 Anthropic 也提供 OpenAI 兼容端点
（https://api.anthropic.com/v1/，需传 x-api-key）。此处优先走兼容端点，
保留本文件作为原生格式的扩展位。
"""
from __future__ import annotations

from .openai_vlm import OpenAICompatBackend


class AnthropicBackend(OpenAICompatBackend):
    name = "anthropic"

    def __init__(self, config):
        if not config.base_url:
            config.base_url = "https://api.anthropic.com/v1"
        super().__init__(config)
