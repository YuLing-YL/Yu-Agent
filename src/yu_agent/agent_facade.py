"""Yu-Agent 顶层门面：组装配置、模型、记忆、技能、主循环。

GUI 和 CLI 都通过这个类启动 agent，避免重复组装逻辑。
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Dict, Optional

from .agent import AgentLoop
from .common.errors import ConfigError
from .common.logging import setup_logging
from .memory import MemoryManager
from .models.registry import get_backend
from .skills import build_default_registry
from .settings import ModelConfig, get_settings, reload_settings


class YuAgent:
    """Yu-Agent 门面。"""

    def __init__(
        self,
        model_config: Optional[ModelConfig] = None,
        use_mock: bool = False,
        dry_run: bool = False,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        memory: Optional[MemoryManager] = None,
    ):
        setup_logging()

        settings = get_settings()
        self.cfg = settings

        if model_config is None:
            model_config = settings.get_model("default")

        self.model_config = model_config

        # 构建后端
        if use_mock or not model_config.is_configured:
            provider = "mock"
        else:
            provider = model_config.provider
        self.backend = get_backend(model_config, provider)

        # 记忆
        self.memory = memory or MemoryManager(enabled=settings.memory.get("enabled", True))

        # 技能
        self.skill_registry = build_default_registry()

        # 主循环
        self.loop = AgentLoop(
            backend=self.backend,
            memory=self.memory,
            skill_registry=self.skill_registry,
            config={
                "max_steps": settings.agent.max_steps,
                "max_consecutive_failures": settings.agent.max_consecutive_failures,
                "verify_after_action": settings.loop.get("verify_after_action", True),
                "screenshot_diff_threshold": settings.loop.get("screenshot_diff_threshold", 0.002),
                "max_image_side": settings.perception.get("max_image_side", 1568),
            },
            dry_run=dry_run,
            callback=callback,
        )

    def run(self, task: str):
        """执行任务。"""
        return self.loop.run(task)

    def stop(self) -> None:
        self.loop.stop()

    def close(self) -> None:
        self.memory.close()


def build_agent_from_settings(callback=None, use_mock: bool = False, dry_run: bool = False) -> YuAgent:
    """根据当前设置构建 agent（设置页保存后调用）。"""
    reload_settings()
    return YuAgent(use_mock=use_mock, dry_run=dry_run, callback=callback)
