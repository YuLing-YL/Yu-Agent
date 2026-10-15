"""本地 vLLM / Ollama 后端。

两者都提供 OpenAI 兼容端点，复用 OpenAICompatBackend。
vLLM: http://localhost:8000/v1
Ollama: http://localhost:11434/v1
"""
from __future__ import annotations

from .openai_vlm import OpenAICompatBackend


class LocalVLLMBackend(OpenAICompatBackend):
    name = "local-vllm"

    def __init__(self, config):
        if not config.base_url:
            config.base_url = "http://localhost:8000/v1"
        super().__init__(config)
