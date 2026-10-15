"""响应缓存：开发期避免重复计费，回放测试靠它。"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from .base import VLMRequest, VLMResponse


class ResponseCache:
    """以请求内容哈希为 key 的内存缓存。"""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._store: dict[str, VLMResponse] = {}

    def _key(self, req: VLMRequest) -> str:
        h = hashlib.sha256()
        h.update(req.system.encode())
        h.update(req.prompt.encode())
        for img in req.images:
            h.update(img.encode())
        return h.hexdigest()

    def get(self, req: VLMRequest) -> Optional[VLMResponse]:
        if not self.enabled:
            return None
        return self._store.get(self._key(req))

    def put(self, req: VLMRequest, resp: VLMResponse) -> None:
        if self.enabled:
            self._store[self._key(req)] = resp
