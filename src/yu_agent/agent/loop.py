"""Agent 主循环：observe → recall → think → ground → act → verify 闭环。

鲁棒性设计：
- 提示词注入防护：外部内容（屏幕/记忆/用户输入）隔离为不可信数据
- 动作校验：执行后对比屏幕变化，无变化则重试/换策略
- 异常容错：单步失败不崩溃，累计失败次数达阈值才终止
- 广告/噪声过滤：OCR 文本过滤广告干扰
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from ..actions.executor import ActionExecutor
from ..actions.schema import Action
from ..common.errors import (
    ActionError,
    GroundingError,
    ModelError,
    SafetyViolation,
)
from ..common.image import image_to_data_url, scale_factor, scale_image
from ..common.logging import get_logger
from ..grounding.resolver import GroundingResolver
from ..grounding.verify import verify_screen_changed
from ..memory import MemoryManager
from ..perception.screenshot import capture_screen, get_screen_size, region_to_pixel
from ..safety.policy import check_action_allowed, detect_injection, sanitize_context
from ..skills.registry import SkillRegistry
from .planner import Planner
from .state import AgentState

log = get_logger("yu_agent.loop")


class AgentLoop:
    """GUI Agent 主循环。"""

    def __init__(
        self,
        backend,
        memory: MemoryManager,
        skill_registry: SkillRegistry,
        config: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.backend = backend
        self.memory = memory
        self.skill_registry = skill_registry
        self.cfg = config or {}
        self.dry_run = dry_run
        self.callback = callback  # 用于 GUI 实时反馈

        self.executor = ActionExecutor()
        self.planner = Planner(backend, skill_registry)
        self.resolver = GroundingResolver()
        self.state = AgentState()

        self.max_steps = self.cfg.get("max_steps", 30)
        self.max_consecutive_failures = self.cfg.get("max_consecutive_failures", 3)
        self.verify_after_action = self.cfg.get("verify_after_action", True)
        self.diff_threshold = self.cfg.get("screenshot_diff_threshold", 0.002)
        self.max_image_side = self.cfg.get("max_image_side", 1568)

    def run(self, task: str) -> AgentState:
        """执行一个任务，直到完成或达到步数上限。"""
        self.state = AgentState(task=task, running=True)
        self.memory.start_task(task)

        self._emit({"type": "start", "task": task})
        log.info("开始任务：%s", task)

        consecutive_failures = 0

        try:
            while self.state.running and self.state.step < self.max_steps:
                self.state.step += 1
                step = self.state.step

                try:
                    # 1. observe：截屏（逻辑像素 1:1）
                    before = capture_screen()
                    screen = get_screen_size()
                    obs_img = scale_image(before, self.max_image_side)
                    # 记录缩放比例，用于把模型返回的坐标还原回逻辑像素
                    sx, sy = scale_factor(before, obs_img)
                    data_url = image_to_data_url(obs_img)

                    # 2. recall：召回记忆上下文（隔离为不可信内容）
                    mem_ctx = sanitize_context(self.memory.recall_context(task))
                    work_ctx = sanitize_context(self.memory.working.context_for_prompt())

                    # 3. think：模型决策
                    action = self.planner.plan(
                        task=task,
                        screenshot_data_url=data_url,
                        memory_context=mem_ctx,
                        working_context=work_ctx,
                        screen_size=(screen.width, screen.height),
                    )

                    # 4. 处理 plan（任务拆分）
                    if action.type == "plan":
                        subtasks = action.args.get("subtasks", [])
                        if subtasks:
                            self.state.subtasks = list(subtasks)
                            self.state.current_subtask = 0
                            self.memory.set_subtasks(self.state.subtasks)
                            self._emit(
                                {"type": "plan", "subtasks": self.state.subtasks}
                            )
                            log.info("任务拆分为 %d 个子任务", len(subtasks))
                            continue  # 不执行，直接进入下一轮
                        else:
                            action.type = "wait"

                    # 5. ground + safety 检查
                    check_action_allowed(action.type)

                    # 5.5 坐标还原：模型基于缩放图返回的坐标 → 逻辑像素坐标
                    if action.x is not None and action.y is not None:
                        action.x, action.y = region_to_pixel(action.x, action.y, sx, sy)

                    # 6. act：执行动作
                    self._emit(
                        {
                            "type": "action",
                            "step": step,
                            "action": action.type,
                            "reason": action.reason,
                            "x": action.x,
                            "y": action.y,
                            "text": action.text,
                        }
                    )
                    if self.dry_run:
                        result_desc = f"[dry-run] 跳过执行：{action.type}"
                    else:
                        result_desc = self._execute(action)

                    # 7. verify：校验屏幕变化
                    screen_changed = True
                    if self.verify_after_action and action.type not in (
                        "done",
                        "finish",
                        "plan",
                        "wait",
                    ):
                        time.sleep(0.3)
                        after = capture_screen()
                        screen_changed = verify_screen_changed(
                            before, after, self.diff_threshold
                        )
                        if not screen_changed:
                            result_desc += "（屏幕无变化，可能未生效）"

                    # 8. 记录记忆 + 历史
                    self.memory.add_step(
                        step=step,
                        observation_summary=f"执行{action.type}",
                        action=f"{action.type}" + (
                            f"({action.x},{action.y})" if action.x is not None else ""
                        ),
                        result=result_desc,
                    )
                    self.state.history.append(
                        {
                            "step": step,
                            "action": action.type,
                            "reason": action.reason,
                            "result": result_desc,
                        }
                    )

                    self._emit(
                        {"type": "result", "step": step, "result": result_desc}
                    )

                    # 9. 判断是否结束
                    if action.is_done:
                        self.state.done = True
                        self.state.success = True
                        self.state.reason = action.reason or "任务完成"
                        self.state.running = False
                        break

                    consecutive_failures = 0  # 成功执行，重置失败计数

                except SafetyViolation as e:
                    log.warning("安全拦截：%s", e)
                    consecutive_failures += 1
                    self._emit({"type": "safety", "message": str(e)})
                    if consecutive_failures >= self.max_consecutive_failures:
                        self.state.reason = f"安全策略多次拦截：{e}"
                        break

                except (ModelError, GroundingError, ActionError) as e:
                    log.warning("第 %d 步失败：%s", step, e)
                    consecutive_failures += 1
                    self._emit({"type": "error", "step": step, "message": str(e)})
                    if consecutive_failures >= self.max_consecutive_failures:
                        self.state.reason = f"连续失败 {consecutive_failures} 次：{e}"
                        break

                except Exception as e:
                    log.exception("第 %d 步发生未预期异常", step)
                    consecutive_failures += 1
                    self._emit({"type": "error", "step": step, "message": str(e)})
                    if consecutive_failures >= self.max_consecutive_failures:
                        self.state.reason = f"未预期异常：{e}"
                        break

        finally:
            self.state.running = False
            # 蒸馏长期记忆
            self.memory.finish_task(task, self.state.history, self.state.success)
            self._emit(
                {
                    "type": "finish",
                    "success": self.state.success,
                    "reason": self.state.reason,
                    "steps": self.state.step,
                }
            )
            log.info("任务结束：success=%s reason=%s", self.state.success, self.state.reason)

        return self.state

    def _execute(self, action: Action) -> str:
        """执行动作（含技能调用），返回结果描述。"""
        if action.type == "skill":
            if not action.skill:
                return "skill 动作缺少技能名"
            if not self.skill_registry.has(action.skill):
                return f"技能未注册：{action.skill}"
            try:
                result = self.skill_registry.call(action.skill, **action.args)
                return str(result)
            except Exception as e:
                raise ActionError(f"技能 {action.skill} 执行失败：{e}") from e
        else:
            self.executor.execute(action)
            return f"已执行 {action.type}" + (
                f"({action.x},{action.y})" if action.x is not None else ""
            )

    def stop(self) -> None:
        """外部请求停止。"""
        self.state.running = False

    def _emit(self, event: Dict[str, Any]) -> None:
        if self.callback:
            try:
                self.callback(event)
            except Exception:
                pass
