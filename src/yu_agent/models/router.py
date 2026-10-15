"""模型路由：角色 -> 后端。当前简化为单模型（设置页配置的那个）。"""
from __future__ import annotations

from typing import Any

from .base import VLMBackend
from .registry import get_backend


class ModelRouter:
    """把 planner/grounder/reflector 等角色映射到后端。

    目前所有角色共用设置页里配置的那一个模型，后续可扩展大/小模型分工。
    """

    def __init__(self, model_config: Any, provider: str = "openai"):
        self.model_config = model_config
        self.provider = provider
        self._backends: dict[str, VLMBackend] = {}

    def get(self, role: str = "default") -> VLMBackend:
        if role not in self._backends:
            self._backends[role] = get_backend(self.model_config, self.provider)
        return self._backends[role]
