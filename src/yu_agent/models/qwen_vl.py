"""通义千问 VL（DashScope 兼容模式）。

DashScope 提供 OpenAI 兼容端点，因此直接复用 OpenAICompatBackend。
保留此文件作为显式入口，便于后续加千问特有的功能。
"""
from __future__ import annotations

from .openai_vlm import OpenAICompatBackend


class QwenVLBackend(OpenAICompatBackend):
    name = "qwen-vl"

    def __init__(self, config):
        # 未指定 base_url 时默认指向 DashScope 兼容端点
        if not config.base_url or "dashscope" not in config.base_url:
            config.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        super().__init__(config)
