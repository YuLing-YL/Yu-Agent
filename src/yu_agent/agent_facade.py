"""Yu-Agent 顶层门面：组装配置、模型、记忆、技能、主循环。

GUI 和 CLI 都通过这个类启动 agent，避免重复组装逻辑。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .agent import AgentLoop
from .common.errors import ConfigError
from .common.logging import setup_logging
from .common.paths import project_root
from .memory import MemoryManager
from .memory.store import MemoryStore
from .models.registry import get_backend
from .perception import a11y as a11y_mod
from .settings import ModelConfig, Settings, get_settings, reload_settings
from .skills import build_default_registry


def _resolve_path(p: str) -> Path:
    """配置里的路径：相对路径按项目根解析，绝对路径原样。"""
    path = Path(p).expanduser()
    return path if path.is_absolute() else (project_root() / path)


def _loop_config(settings: Settings) -> Dict[str, Any]:
    """把 Settings 摊平成 AgentLoop 认识的 dict。

    只在这里做一次映射：loop 不知道配置长什么样，配置改了也不用动 loop。
    """
    agent, loop = settings.agent, settings.loop
    return {
        # 步数 / 超时 / 失败容忍
        "max_steps": agent.max_steps,
        "step_timeout_s": agent.step_timeout_s,
        "task_timeout_s": agent.task_timeout_s,
        "max_consecutive_failures": agent.max_consecutive_failures,
        "stop_on_grounding_error": agent.stop_on_grounding_error,
        # 校验
        "verify_after_action": loop.verify_after_action,
        "verify_delay_s": loop.verify_delay_s,
        "screenshot_diff_threshold": loop.screenshot_diff_threshold,
        # 图像
        "max_image_side": settings.perception.max_image_side,
        "use_som": settings.perception.use_som,
        # SOM / OCR
        "som_mode": agent.som_mode,
        "som_max_marks": agent.som_max_marks,
        "som_dock_max_marks": loop.som_dock_max_marks,
        "som_max_apps": loop.som_max_apps,
        "som_frontmost_only": agent.som_frontmost_only,
        "som_content_check": agent.som_content_check,
        "som_ocr": loop.som_ocr,
        "som_ocr_max": loop.som_ocr_max,
        "som_ocr_min_conf": loop.som_ocr_min_conf,
        "som_ocr_skip_ax_elements": loop.som_ocr_skip_ax_elements,
        # 运行记录
        "save_trace": loop.save_trace,
        "runs_keep": loop.runs_keep,
        "redact_secrets": settings.safety.redact_secrets_in_traces,
        # 防卡死
        "max_repeated_actions": agent.max_repeated_actions,
        "max_same_action_total": agent.max_same_action_total,
        "max_same_action_total_other": agent.max_same_action_total_other,
        "max_total_clicks": agent.max_total_clicks,
        "max_no_change_steps": agent.max_no_change_steps,
        "max_plan_rejects": agent.max_plan_rejects,
        "type_interval_s": agent.type_interval_s,
        # 结果复核 / 重规划 / 成本
        "verify_goal": loop.verify_goal,
        "max_goal_rejects": loop.max_goal_rejects,
        "max_replans": loop.max_replans,
        "ocr_anchor": loop.ocr_anchor,
        "require_approval_for_risky": loop.require_approval_for_risky,
        "model_pricing": dict(loop.model_pricing or {}),
        # 安全
        "forbidden_actions": list(settings.safety.forbidden_action_types or []),
        "deny_shell": settings.safety.deny_shell_by_default,
        # 定位
        "text_match_threshold": settings.grounding.text_match_threshold,
        "prefer_a11y_over_pixels": settings.grounding.prefer_a11y_over_pixels,
        "coords_mode": settings.grounding.coords_mode,
    }


class YuAgent:
    """Yu-Agent 门面。"""

    def __init__(
        self,
        model_config: Optional[ModelConfig] = None,
        dry_run: bool = False,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        memory: Optional[MemoryManager] = None,
        backend=None,
        control=None,
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

        # 记忆：路径来自 memory.sqlite_path（相对项目根解析）
        if memory is not None:
            self.memory = memory
        else:
            store = MemoryStore(db_path=str(_resolve_path(settings.memory.sqlite_path)))
            self.memory = MemoryManager(store=store, enabled=settings.memory.enabled)

        # 技能
        self.skill_registry = build_default_registry()

        # 感知上限（config 里写明、但以前没人读的两个旋钮）
        a11y_mod.configure(
            max_depth=settings.perception.a11y_max_depth,
            max_elements=settings.perception.a11y_max_elements,
        )

        # 主循环
        self.loop = AgentLoop(
            backend=self.backend,
            memory=self.memory,
            skill_registry=self.skill_registry,
            config=_loop_config(settings),
            dry_run=dry_run,
            callback=callback,
            control=control,
        )

    def run(self, task: str):
        """执行任务。"""
        return self.loop.run(task)

    def stop(self) -> None:
        self.loop.stop()

    def pause(self) -> None:
        self.loop.pause()

    def resume(self) -> None:
        self.loop.resume()

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
    from .models.mock import MockBackend

    memory = MemoryManager(store=MemoryStore(db_path=":memory:"))
    backend = MockBackend()
    return YuAgent(backend=backend, memory=memory, dry_run=dry_run, callback=callback)
