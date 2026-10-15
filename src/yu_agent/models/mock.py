"""脚本化假后端：离线测试 / 无 API Key 时演示用。

根据 prompt 里的指令返回预设动作，让整条闭环可离线跑通。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import TokenUsage, VLMRequest, VLMResponse


class MockBackend:
    """返回一个可解析的 JSON 动作。用于端到端演示与单元测试。"""

    name = "mock"

    def __init__(self, config: Any = None):
        self.config = config
        self._calls = 0

    def chat(self, req: VLMRequest) -> VLMResponse:
        self._calls += 1
        # 如果 prompt 里显式约定动作（测试用），优先返回
        m = re.search(r"ACTION:\s*(\w+)(?:\s+(\d+)\s+(\d+))?", req.prompt)
        if m:
            action = {
                "type": m.group(1),
                "x": int(m.group(2)) if m.group(2) else None,
                "y": int(m.group(3)) if m.group(3) else None,
                "reason": "mock 后端按指令返回",
            }
        else:
            # 脚本化演示流程：第 1 步拆分，中间执行动作，接近上限时结束
            if self._calls == 1:
                action = {
                    "type": "plan",
                    "subtasks": ["定位目标应用", "执行操作", "确认结果"],
                    "reason": "任务拆分为 3 个子任务（mock 演示）",
                }
            elif self._calls <= 3:
                action = {
                    "type": "click",
                    "x": 400,
                    "y": 300,
                    "reason": "mock 演示动作",
                }
            else:
                action = {"type": "done", "reason": "任务完成（mock 演示）"}

        text = json.dumps(action, ensure_ascii=False)
        return VLMResponse(
            text=text,
            usage=TokenUsage(),
            model="mock",
            latency_s=0.001,
        )
