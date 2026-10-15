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
        dry_run: bool = False,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        memory: Optional[MemoryManager] = None,
        backend=None,
    ):
        setup_logging()

        settings = get_settings()
        self.cfg = settings

        if model_config is None:
            model_config = settings.get_model("default")

        self.model_config = model_config

        # 构建后端：优先用注入的 backend（测试用），否则按配置构建
        if backend is not None:
            self.backend = backend
        else:
            if not model_config.is_configured:
                raise ConfigError(
                    "尚未配置模型 API。请在「设置」中填写 base_url / api_key / 模型名。"
                )
            self.backend = get_backend(model_config, model_config.provider)

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
                "verify_delay_s": settings.loop.get("verify_delay_s", 0.5),
                "screenshot_diff_threshold": settings.loop.get("screenshot_diff_threshold", 0.002),
                "max_image_side": settings.perception.get("max_image_side", 1568),
                "use_som": settings.perception.get("use_som", True),
                "som_max_marks": settings.loop.get("som_max_marks", 120),
                "som_dock_max_marks": settings.loop.get("som_dock_max_marks", 30),
                "som_max_apps": settings.loop.get("som_max_apps", 6),
                "max_repeated_actions": settings.agent.max_repeated_actions,
                "max_same_action_total": settings.agent.max_same_action_total,
                "max_same_action_total_other": settings.agent.max_same_action_total_other,
                "max_total_clicks": settings.agent.max_total_clicks,
                "max_no_change_steps": settings.agent.max_no_change_steps,
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


def build_agent_from_settings(callback=None, dry_run: bool = False) -> YuAgent:
    """根据当前设置构建 agent（设置页保存后调用）。"""
    reload_settings()
    return YuAgent(dry_run=dry_run, callback=callback)


def build_test_agent(callback=None, dry_run: bool = True) -> YuAgent:
    """构建用于离线测试的 agent（mock 后端 + 内存记忆），不依赖 API 和真实屏幕。

    供测试脚本 / pytest 使用，与生产 GUI 完全解耦。
    """
    from .memory.store import MemoryStore
    from .models.mock import MockBackend

    memory = MemoryManager(store=MemoryStore(db_path=":memory:"))
    backend = MockBackend()
    return YuAgent(backend=backend, memory=memory, dry_run=dry_run, callback=callback)
