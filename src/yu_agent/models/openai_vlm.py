"""OpenAI 兼容接口后端（覆盖 OpenAI / Gemini / Qwen / DeepSeek / Kimi / 本地 vLLM 等）。

用户只需填 base_url + api_key + model，即可接入几乎所有主流 VLM。
"""
from __future__ import annotations

import time
from typing import Any, List

from ..common.errors import ModelAuthError, ModelError
from .base import TokenUsage, VLMRequest, VLMResponse


class OpenAICompatBackend:
    """基于 requests 实现的 OpenAI 兼容 Chat Completions 客户端。

    不依赖 openai SDK，避免版本/依赖冲突，直接调 REST API 更可控。
    """

    name = "openai-compat"

    def __init__(self, config: Any):
        self.config = config
        self.base_url = (config.base_url or "").rstrip("/")
        self.api_key = config.api_key
        self.model = config.model
        self.timeout = getattr(config, "timeout_s", 60.0)

    def chat(self, req: VLMRequest) -> VLMResponse:
        if not self.api_key:
            raise ModelAuthError(
                "未配置 API Key。请在「设置」中填写 api_key（或设置环境变量 API_KEY）。"
            )

        import requests

        messages: List[dict] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})

        content: Any
        if req.images:
            # 多模态：文本 + 图片（data URL）
            parts: List[dict] = [{"type": "text", "text": req.prompt}]
            for img in req.images:
                parts.append({"type": "image_url", "image_url": {"url": img}})
            content = parts
        else:
            content = req.prompt
        messages.append({"role": "user", "content": content})

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}/chat/completions"
        t0 = time.time()
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
        except requests.exceptions.Timeout as e:
            raise ModelError(f"模型调用超时（{self.timeout}s）：{e}") from e
        except requests.exceptions.ConnectionError as e:
            raise ModelError(f"无法连接到模型服务 {self.base_url}：{e}") from e

        if resp.status_code == 401 or resp.status_code == 403:
            raise ModelAuthError(
                f"模型鉴权失败（HTTP {resp.status_code}）。请检查 API Key 是否正确。"
            )
        if resp.status_code != 200:
            raise ModelError(
                f"模型返回错误 HTTP {resp.status_code}：{resp.text[:500]}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise ModelError(f"模型返回非 JSON：{resp.text[:300]}") from e

        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise ModelError(f"模型返回格式异常：{str(data)[:500]}") from e

        usage_raw = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
        )
        return VLMResponse(
            text=text,
            usage=usage,
            raw=data,
            model=data.get("model", self.model),
            latency_s=time.time() - t0,
        )
