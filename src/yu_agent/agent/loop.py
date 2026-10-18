"""Agent 主循环：observe → recall → think → ground → act → verify 闭环。

鲁棒性设计：
- 提示词注入防护：外部内容（屏幕/记忆/用户输入）隔离为不可信数据
- 动作校验：执行后对比屏幕变化，无变化则重试/换策略
- 异常容错：单步失败不崩溃，累计失败次数达阈值才终止
- 广告/噪声过滤：OCR 文本过滤广告干扰
- 终止保障（多层，防止「一直点停不下来」）：
  1. 用户停止后立即生效：模型返回后、执行动作前都会再检查一次，绝不「点了停止还继续点」
  2. 连续相同动作上限（max_repeated_actions）
  3. 同一目标累计操作上限（max_same_action_total）——可拦住 A→B→A→B 式绕圈
  4. 整个任务点击总预算（max_total_clicks）
  5. 屏幕连续无变化上限（max_no_change_steps）
  6. 单任务步数上限（max_steps）
  7. 重复拆分任务上限（max_plan_rejects）——拦住「拆分↔观察」死循环
  并把「最近动作 + 重复警告」回灌给模型，让它自己意识到该收手。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..actions.desktop import CleanReport, clean_desktop
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
from ..common.paths import new_run_dir
from ..grounding.resolver import GroundingResolver
from ..grounding.verify import verify_screen_changed
from ..memory import MemoryManager
from ..perception import a11y as a11y_mod
from ..perception.screenshot import capture_screen, get_screen_size, region_to_pixel
from ..perception.som import build_som_mapping, describe_marks
from ..safety.policy import check_action_allowed, detect_injection, sanitize_context
from ..skills.registry import SkillRegistry
from .planner import Planner
from .state import AgentState

log = get_logger("yu_agent.loop")

# 会被判定为「点击」的动作类型（防重复的重点对象）
_CLICK_TYPES = {"click", "double_click", "right_click"}
# 坐标量化网格（逻辑像素）：把相邻几像素的点击视为「同一个目标」
_COORD_GRID = 20
# 「可以输入文字」的角色（点了它们之后，下一步应该是 type 而不是再点一次）
_INPUT_ROLES = {
    "AXTextField", "AXTextArea", "AXSearchField", "AXSecureTextField", "AXComboBox",
    "输入框", "文本域", "搜索框", "密码框", "下拉框",
}
# 角色识别不出来时，用标题/描述里的关键词兜底
_INPUT_KEYWORDS = ("搜索", "输入", "地址", "网址", "search", "url", "输入框", "查找")


def _quantize(v: Optional[int]) -> Optional[int]:
    """把坐标量化到网格，消除模型输出的几像素抖动。"""
    if v is None:
        return None
    return int(round(float(v) / _COORD_GRID))


def _action_target_sig(action: Action) -> Tuple[Any, ...]:
    """把动作归一化成「目标标识」，用于判定是否在重复操作同一个地方。"""
    t = action.type
    if t in _CLICK_TYPES:
        # 坐标没解析出来（例如只有 mark）时，退回用 mark 区分不同目标
        if action.x is None or action.y is None:
            return ("click", "mark", action.mark)
        return ("click", _quantize(action.x), _quantize(action.y))
    if t == "key":
        return ("key", (action.key or "").strip().lower())
    if t == "scroll":
        return ("scroll", (action.text or "").strip().lower())
    if t == "type":
        return ("type", action.text or "")
    if t == "skill":
        return ("skill", action.skill or "")
    return (t,)


def _is_input_like(el: Any) -> bool:
    """判断这个元素是不是「可以输入文字」的控件。"""
    try:
        role = str(getattr(el, "role", "") or "")
        title = str(getattr(el, "title", "") or "")
        desc = str(getattr(el, "description", "") or "")
    except Exception:
        return False
    if role in _INPUT_ROLES:
        return True
    text = f"{title} {desc}"
    return any(k in text for k in _INPUT_KEYWORDS)


def _describe_target(action: Action) -> str:
    """给人类读的目标描述（写进日志 / 回灌给模型）。"""
    if action.x is not None and action.y is not None:
        return f"({action.x},{action.y})"
    if action.mark is not None:
        return f"编号{action.mark}"
    if action.key:
        return action.key
    if action.text:
        return repr(action.text)[:20]
    return ""


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

        self.max_steps = self.cfg.get("max_steps", 20)
        self.max_consecutive_failures = self.cfg.get("max_consecutive_failures", 3)
        self.verify_after_action = self.cfg.get("verify_after_action", True)
        self.verify_delay_s = self.cfg.get("verify_delay_s", 0.5)
        self.diff_threshold = self.cfg.get("screenshot_diff_threshold", 0.002)
        self.max_image_side = self.cfg.get("max_image_side", 1568)
        self.use_som = self.cfg.get("use_som", True)
        # SOM 标记预算：总数 / 程序坞上限 / 最多枚举几个应用
        self.som_max_marks = self.cfg.get("som_max_marks", 120)
        self.som_dock_max = self.cfg.get("som_dock_max_marks", 30)
        self.som_max_apps = self.cfg.get("som_max_apps", 6)
        # ── 防重复 / 防卡死（避免「一直点击停不下来」）──
        # 连续多少次完全相同的动作 → 停
        self.max_repeats = self.cfg.get("max_repeated_actions", 3)
        # 同一个目标累计被操作多少次 → 停（点击类，最严格）
        self.max_same_target = self.cfg.get("max_same_action_total", 3)
        # 同一目标累计上限（按键/滚动/输入等，放宽一些，因为确有合法的连续操作）
        self.max_same_other = self.cfg.get("max_same_action_total_other", 8)
        # 整个任务的点击总预算 → 停
        self.max_total_clicks = self.cfg.get("max_total_clicks", 12)
        # 屏幕连续无变化多少次 → 停
        self.max_stuck = self.cfg.get("max_no_change_steps", 3)
        # 模型反复「拆分子任务」却不执行任何实际操作 → 停（防「拆分↔观察」死循环）
        self.max_plan_rejects = self.cfg.get("max_plan_rejects", 2)
        # 「已点击但还没输入文字」的输入框描述（由 _build_input_hint 回灌给模型）
        self._pending_input: Optional[str] = None
        # ── 运行前清场：把其它应用隐藏掉，减少 SOM 标记干扰 ──
        # 默认关闭：只有通过配置（GUI/CLI）显式开启才会动用户的窗口，
        # 避免单元测试或库调用时意外把别人的桌面收掉。
        self.reset_desktop = bool(self.cfg.get("reset_desktop", False))
        self.reset_desktop_keep = list(self.cfg.get("reset_desktop_keep") or [])
        self._clean_report: Optional[CleanReport] = None
        if self.reset_desktop:
            # 清场模式：只标记「当前最前面的应用 + 程序坞」，背景应用的按钮一个都不标。
            # 这是不依赖任何权限的兜底——即使系统不允许隐藏窗口，屏幕再乱也不会干扰识别。
            self.som_max_apps = 1
        self._user_stopped = False

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(self, task: str) -> AgentState:
        """执行一个任务，直到完成、被用户停止、或触发任一安全阈值。"""
        self.state = AgentState(task=task, running=True)
        self.memory.start_task(task)
        self._user_stopped = False
        self._pending_input = None

        self._emit({"type": "start", "task": task})
        log.info("开始任务：%s", task)

        # 0. 清场：隐藏其它应用，只留桌面 + 程序坞。
        #    屏幕上的窗口越少，SOM 枚举出来的干扰元素就越少，模型也更容易选对目标。
        self._clean_report = None
        if self.reset_desktop and not self.dry_run and not self._user_stopped:
            self._prepare_desktop(task)

        # 本次运行的排障记录目录（截图 + trace.jsonl），用于事后定位点错/找不到元素
        run_dir = None
        try:
            run_dir = new_run_dir()
            log.info("本次运行记录目录：%s", run_dir)
        except Exception as e:
            log.warning("无法创建运行记录目录（%s），跳过追踪落盘", e)

        consecutive_failures = 0
        last_sig: Optional[Tuple[Any, ...]] = None
        repeat_count = 0
        no_change_count = 0
        sig_totals: Dict[Tuple[Any, ...], int] = {}
        click_count = 0
        recent: List[str] = []  # 最近动作摘要，回灌给模型提醒它别重复
        perception_warned = False
        plan_rejects = 0  # 被拒绝的「重复拆分」次数

        try:
            while self.state.running and self.state.step < self.max_steps:
                # 用户已点停止 → 立刻退出，绝不开始新的一步
                if self._user_stopped:
                    break

                self.state.step += 1
                step = self.state.step

                try:
                    # 1. observe：截屏（逻辑像素 1:1）
                    before = capture_screen()
                    screen = get_screen_size()
                    obs_img = scale_image(before, self.max_image_side)
                    # 记录缩放比例，用于把模型返回的坐标还原回逻辑像素
                    sx, sy = scale_factor(before, obs_img)

                    # 1.5 SOM 标记：枚举可交互元素，画编号框，供模型精确指代
                    marks_map = {}
                    marks_desc = ""
                    a11y_stats: Dict[str, Any] = {}
                    if self.use_som:
                        try:
                            # 元素坐标是逻辑像素；若截图被缩放过，画框时要换算到图像像素
                            img_scale = (1.0 / sx) if sx else 1.0
                            marked_img, marks_map, _ = build_som_mapping(
                                obs_img,
                                max_elements=self.som_max_marks,
                                coord_scale=img_scale,
                                dock_max=self.som_dock_max,
                                max_apps=self.som_max_apps,
                            )
                            obs_img = marked_img
                            marks_desc = describe_marks(marks_map)
                            a11y_stats = dict(a11y_mod.last_stats)
                            apps = a11y_stats.get("apps") or []
                            apps_desc = "、".join(
                                f"{a.get('name')}({a.get('elements')})" for a in apps
                            )
                            log.info(
                                "观察：标记 %d 个元素（应用 %s 个：%s；程序坞 %s 个；校准 %s）",
                                len(marks_map),
                                a11y_stats.get("app_count"),
                                apps_desc or "无",
                                a11y_stats.get("dock_elements"),
                                a11y_stats.get("calibration"),
                            )
                            self._emit(
                                {
                                    "type": "debug",
                                    "step": step,
                                    "message": (
                                        f"识别到 {len(marks_map)} 个可点元素"
                                        f"（应用 {apps_desc or '无'}"
                                        f"；程序坞 {a11y_stats.get('dock_elements')} 个）"
                                    ),
                                }
                            )
                        except Exception as e:
                            log.warning("SOM 标记失败，回退到像素坐标：%s", e)
                            marks_map = {}

                    data_url = image_to_data_url(obs_img)

                    # 2. recall：召回记忆上下文（隔离为不可信内容）
                    mem_ctx = sanitize_context(self.memory.recall_context(task))
                    work_ctx = sanitize_context(self.memory.working.context_for_prompt())

                    # 2.5 提示词组装（顺序 = 重要度递增，模型最后看到的最强）：
                    #     子任务计划 → 最近动作 → 输入提醒 → 感知异常
                    hint_parts = [
                        self._build_prepare_hint(step),
                        self._build_subtask_hint(),
                        self._build_progress_hint(
                            recent, repeat_count, no_change_count
                        ),
                        self._build_input_hint(),
                    ]
                    warn = self._build_perception_warning(a11y_stats, marks_map)
                    if warn:
                        hint_parts.append(warn)
                        if not perception_warned:  # 只在首次提醒用户，避免刷屏
                            perception_warned = True
                            self._emit({"type": "notice", "step": step, "message": warn})
                    progress_hint = "\n\n".join(p for p in hint_parts if p)

                    # 3. think：模型决策
                    if self._user_stopped:
                        break
                    action = self.planner.plan(
                        task=task,
                        screenshot_data_url=data_url,
                        memory_context=mem_ctx,
                        working_context=work_ctx,
                        screen_size=(screen.width, screen.height),
                        marks=marks_map,
                        marks_desc=marks_desc,
                        extra_hint=progress_hint,
                    )

                    # 用户可能在模型思考期间点了停止 → 丢弃本步动作，不要执行
                    if self._user_stopped:
                        log.info("用户已停止，丢弃本步动作：%s", action.type)
                        break

                    # 4. 处理 plan（任务拆分）
                    #    ⚠️ 整个任务只接受一次拆分。曾经的 bug：plan 分支直接 continue，
                    #    既不计数也不进卡死检测，于是模型只要每轮都输出 plan，
                    #    就会无限循环在「拆分 → 观察 → 拆分 → 观察」，直到步数用尽。
                    if action.type == "plan":
                        subtasks = [
                            str(s).strip()
                            for s in (action.args.get("subtasks") or [])
                            if str(s).strip()
                        ]

                        # 4a. 首次拆分：接受（此后不再允许拆分）
                        if subtasks and not self.state.subtasks:
                            self.state.subtasks = subtasks
                            self.state.current_subtask = 0
                            self.memory.set_subtasks(subtasks)
                            self._emit({"type": "plan", "subtasks": subtasks})
                            log.info(
                                "任务拆分为 %d 个子任务：%s",
                                len(subtasks), " / ".join(subtasks),
                            )
                            self._trace_plan(
                                run_dir=run_dir, step=step, obs_img=obs_img,
                                marks_map=marks_map, marks_desc=marks_desc,
                                a11y_stats=a11y_stats, action=action,
                                raw_text=self.planner.last_raw_text,
                                result_desc=f"已拆分为 {len(subtasks)} 个子任务",
                            )
                            continue  # 不执行动作，下一轮开始执行第一个子任务

                        # 4b. 不该再拆分的两种情况 → 忽略 + 计数 + 回灌纠正提示
                        plan_rejects += 1
                        if subtasks:
                            reason = (
                                f"⚠️ 任务已经拆分过了，忽略这次重复拆分"
                                f"（第 {plan_rejects} 次）"
                            )
                            tip = "现在必须直接执行当前子任务，输出 click / type / key 等具体动作"
                        else:
                            reason = "⚠️ plan 动作没有给出 subtasks，已忽略"
                            tip = "确实需要拆分时，subtasks 必须是非空的字符串列表"
                        log.warning("%s：subtasks=%s", reason, subtasks)
                        self._emit(
                            {"type": "notice", "step": step,
                             "message": f"{reason}。{tip}。"}
                        )
                        self._emit(
                            {"type": "result", "step": step, "result": reason}
                        )
                        recent.append(f"步骤{step}: 重复拆分被系统忽略（{reason}）")
                        del recent[:-6]
                        self._trace_plan(
                            run_dir=run_dir, step=step, obs_img=obs_img,
                            marks_map=marks_map, marks_desc=marks_desc,
                            a11y_stats=a11y_stats, action=action,
                            raw_text=self.planner.last_raw_text,
                            result_desc=reason,
                        )
                        if plan_rejects >= self.max_plan_rejects:
                            self._auto_stop(
                                step,
                                "模型连续把任务反复拆分成子任务、却没有执行任何实际操作，"
                                "已自动停止以免陷入死循环。请把任务描述得更具体一些，"
                                "或换用能力更强的模型。",
                                success=False,
                            )
                            break
                        continue

                    # 4.5 子任务完成：推进到下一个；全部完成则结束任务
                    if action.type == "subtask_done":
                        total = len(self.state.subtasks)
                        idx = self.state.current_subtask
                        if total == 0 or idx >= total - 1:
                            # 没有子任务，或已是最后一个 → 整体任务完成
                            self.state.done = True
                            self.state.success = True
                            self.state.reason = action.reason or "全部子任务已完成"
                            self.state.running = False
                            self._emit(
                                {
                                    "type": "result",
                                    "step": step,
                                    "result": f"✅ {self.state.reason}",
                                }
                            )
                            break
                        # 推进到下一个子任务
                        self.state.current_subtask += 1
                        self.memory.working.current_subtask = self.state.current_subtask
                        name = self.state.subtasks[self.state.current_subtask]
                        self._emit(
                            {
                                "type": "subtask",
                                "index": self.state.current_subtask,
                                "total": total,
                                "name": name,
                            }
                        )
                        log.info("推进到子任务 %d/%d：%s", self.state.current_subtask + 1, total, name)
                        continue

                    # 5. ground + safety 检查
                    check_action_allowed(action.type)

                    # 5.5 坐标解析：优先用 SOM 编号查表，否则用像素坐标还原
                    raw_x, raw_y = action.x, action.y
                    clicked_el = None  # 本步点击的是哪个已标记元素（用于判断是否点了输入框）
                    if action.mark is not None and action.mark in marks_map:
                        el = marks_map[action.mark]
                        clicked_el = el
                        # 元素坐标本身就是逻辑像素（与点击同一坐标系），不要乘缩放比
                        action.x = int(round(el.cx))
                        action.y = int(round(el.cy))
                        log.info(
                            "SOM 定位：#%d -> %s (%d,%d)",
                            action.mark, el.label(), action.x, action.y,
                        )
                    elif action.x is not None and action.y is not None:
                        action.x, action.y = region_to_pixel(action.x, action.y, sx, sy)
                        if sx != 1.0 or sy != 1.0:
                            log.info(
                                "坐标还原：(原始 %s,%s) * (%s,%s) → (%s,%s)",
                                raw_x, raw_y, sx, sy, action.x, action.y,
                            )
                        # 模型没用编号、直接给了像素坐标：VLM 估坐标常有几十像素偏差，
                        # 若落在某个已标记元素附近，就吸附到该元素中心，避免"差一点"点偏。
                        snap = (
                            self._snap_to_mark(action.x, action.y, marks_map)
                            if action.type in _CLICK_TYPES
                            else None
                        )
                        if snap is not None:
                            sx_, sy_, num, el = snap
                            clicked_el = el
                            log.info(
                                "坐标吸附：模型给的 (%d,%d) → #%d %s 中心 (%d,%d)",
                                action.x, action.y, num, el.label(), sx_, sy_,
                            )
                            action.x, action.y = sx_, sy_
                        elif marks_map:
                            log.warning(
                                "模型未使用编号、直接给了坐标 (%d,%d)，且附近没有匹配的标记元素，"
                                "可能点偏；标记图见本次 runs 目录。",
                                action.x, action.y,
                            )

                    # 执行前最后一次确认用户是否已停止
                    if self._user_stopped:
                        log.info("用户已停止，本步动作不再执行。")
                        break

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

                    # 6.5 输入流程看护：
                    #     点了输入框却没接着输入文字，是本地模型最常见的失败模式——
                    #     它会反复点同一个输入框、直到重复检测把它当成"任务完成"。
                    #     这里记下来，下一轮用一条强提示把模型推回 type 动作。
                    if (
                        action.type in _CLICK_TYPES
                        and clicked_el is not None
                        and _is_input_like(clicked_el)
                    ):
                        self._pending_input = clicked_el.label()
                        result_desc += "（输入框已聚焦：下一步必须用 type 输入文字）"
                        log.info(
                            "点击了输入框「%s」：下一轮必须输出 type 输入文字",
                            self._pending_input,
                        )
                    elif action.type == "type":
                        self._pending_input = None

                    # 7. verify：校验屏幕变化
                    screen_changed = True
                    if self.verify_after_action and action.type not in (
                        "done",
                        "finish",
                        "plan",
                        "wait",
                        "subtask_done",
                    ):
                        time.sleep(self.verify_delay_s)
                        after = capture_screen()
                        screen_changed = verify_screen_changed(
                            before, after, self.diff_threshold
                        )
                        if not screen_changed:
                            result_desc += "（屏幕无变化，可能未生效）"
                            no_change_count += 1
                        else:
                            no_change_count = 0

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

                    # 8.5 排障追踪：把这一步「模型看到什么 / 说了什么 / 点了哪」
                    #     落盘到 runs/<时间戳>/，方便事后定位点错位置的原因
                    self._save_trace(
                        run_dir=run_dir,
                        step=step,
                        obs_img=obs_img,
                        marks_count=len(marks_map),
                        marks_desc=marks_desc,
                        a11y_stats=a11y_stats,
                        action=action,
                        raw_x=raw_x,
                        raw_y=raw_y,
                        raw_text=self.planner.last_raw_text,
                        result_desc=result_desc,
                    )

                    # 9. 判断是否结束（模型自己说完成）
                    if action.is_done:
                        self.state.done = True
                        self.state.success = True
                        self.state.reason = action.reason or "任务完成"
                        self.state.running = False
                        break

                    # 10. 目标统计 + 多层防重复 / 防卡死
                    sig = _action_target_sig(action)
                    sig_totals[sig] = sig_totals.get(sig, 0) + 1
                    if sig == last_sig:
                        repeat_count += 1
                    else:
                        last_sig = sig
                        repeat_count = 1

                    recent.append(
                        f"步骤{step}: {action.type} {_describe_target(action)} → "
                        f"{result_desc[:40]}"
                    )
                    del recent[:-6]  # 只保留最近 6 条

                    if action.type in _CLICK_TYPES:
                        click_count += 1

                    stop = self._check_stuck(
                        action=action,
                        sig=sig,
                        sig_totals=sig_totals,
                        repeat_count=repeat_count,
                        no_change_count=no_change_count,
                        click_count=click_count,
                    )
                    if stop:
                        reason, as_success = stop
                        self._auto_stop(step, reason, success=as_success)
                        break

                    consecutive_failures = 0  # 成功执行，重置失败计数
                    # 有「真正推进」的动作 → 清空无效拆分计数
                    # （计的是「连续无进展的拆分」，偶发一次空 plan 不会累积误停）
                    if action.type != "wait":
                        plan_rejects = 0

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
            # 用户主动停止且未成功完成时，给出明确原因
            if self._user_stopped and not self.state.success:
                self.state.reason = self.state.reason or "任务已被用户停止"
            # 蒸馏长期记忆
            self.memory.finish_task(task, self.state.history, self.state.success)
            self._emit(
                {
                    "type": "finish",
                    "success": self.state.success,
                    "reason": self.state.reason,
                    "steps": self.state.step,
                    "auto_stopped": self.state.auto_stopped,
                    "user_stopped": self._user_stopped,
                }
            )
            log.info("任务结束：success=%s reason=%s", self.state.success, self.state.reason)

        return self.state

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _check_stuck(
        self,
        *,
        action: Action,
        sig: Tuple[Any, ...],
        sig_totals: Dict[Tuple[Any, ...], int],
        repeat_count: int,
        no_change_count: int,
        click_count: int,
    ) -> Optional[Tuple[str, bool]]:
        """多层卡死检测。

        返回 None = 继续执行；返回 (原因, 是否算作成功) = 应停止。

        「重复做同一件事」是否算成功，**要看屏幕有没有反应**：
        - 屏幕每次都变了 → 动作是有效的，模型只是没收手，大概率任务已完成 → success=True
        - 屏幕始终没变化 → 这些操作根本没生效（例如反复点输入框却始终没输出 type），
          这种情况**不能**报"已完成"，否则界面会显示 ✅ 而任务其实没做完 → success=False
        """
        target = _describe_target(action)
        no_effect = self.verify_after_action and no_change_count >= 1

        # (a) 连续完全相同的动作
        #     wait 是"等加载"，本身不点击，交给下面更宽松的累计上限处理
        if action.type != "wait" and repeat_count >= self.max_repeats:
            if no_effect:
                return (
                    f"连续 {repeat_count} 次重复同一个动作（{action.type}"
                    f"{(' ' + target) if target else ''}），且屏幕一直没有变化——"
                    f"说明这个动作并没有生效，任务**很可能没有完成**。已自动停止。"
                    f"如果这一步本来是要输入文字，请检查是否用了 type 动作。",
                    False,
                )
            return (
                f"连续 {repeat_count} 次执行了完全相同的动作（{action.type}"
                f"{(' ' + target) if target else ''}），已自动停止。"
                f"如果目标已经达成，这就是正常的结束；否则请换一种方式重试。",
                True,
            )

        # (b) 同一个目标累计操作次数过多（拦住 A→B→A→B 式绕圈）
        total = sig_totals.get(sig, 0)
        if action.type in _CLICK_TYPES:
            if total >= self.max_same_target:
                if no_effect:
                    return (
                        f"同一个位置/目标已被重复点击 {total} 次，且屏幕一直没有变化——"
                        f"判定该操作无效，任务**很可能没有完成**，已自动停止。",
                        False,
                    )
                return (
                    f"同一个位置/目标已被重复操作 {total} 次"
                    f"（最近一次：{action.type} {target}），"
                    f"判定该操作无效或任务已完成，已自动停止。",
                    True,
                )
        elif action.type in ("key", "scroll", "type", "skill", "wait") and total >= self.max_same_other:
            return (
                f"同一类动作（{action.type}）已重复 {total} 次仍无明显推进，已自动停止。",
                True,
            )

        # (c) 整个任务的点击总预算（更像是"乱点"，不算成功）
        if action.type in _CLICK_TYPES and click_count >= self.max_total_clicks:
            return (
                f"本次任务已累计点击 {click_count} 次，达到安全上限，已自动停止。",
                False,
            )

        # (d) 屏幕连续无变化 → 操作没生效/界面无响应。
        #     这里**不能**报成功：没有任何证据说明任务完成了。
        if self.verify_after_action and no_change_count >= self.max_stuck:
            return (
                f"屏幕连续 {no_change_count} 次没有任何变化，说明这些操作都没有生效"
                f"（界面无响应，或者动作方式不对）。已自动停止，"
                f"但无法确认任务已完成，请检查界面当前状态。",
                False,
            )

        return None

    def _auto_stop(self, step: int, reason: str, success: bool = True) -> None:
        """自动停止（防重复 / 防卡死 / 触顶）。"""
        self.state.done = True
        self.state.success = success
        self.state.auto_stopped = True
        self.state.reason = reason
        self.state.running = False
        self._emit({"type": "notice", "step": step, "message": reason})
        log.info("自动停止（success=%s）：%s", success, reason)

    def _build_progress_hint(
        self,
        recent: List[str],
        repeat_count: int,
        no_change_count: int,
    ) -> str:
        """把「最近做过什么 / 是否在重复」回灌给模型，提醒它及时收手。"""
        parts: List[str] = []
        if recent:
            parts.append(
                "【系统记录：本任务最近的实际操作（请勿重复做同样的事）】\n"
                + "\n".join(recent)
            )
        if repeat_count >= 2:
            parts.append(
                f"【系统警告】你已经连续 {repeat_count} 次执行完全相同的动作，"
                "这几乎一定说明任务已经完成、或者这个动作完全无效。"
                "请立即停止重复它：如果目标已达成，直接输出 "
                '{"type": "done", "reason": "已完成"}；'
                "如果还没达成，必须换一种完全不同的方式。"
            )
        if no_change_count >= 1:
            parts.append(
                f"【系统提示】上一步操作后屏幕没有任何变化（已连续 {no_change_count} 次），"
                "说明这个操作没有生效，或任务其实已经完成了。请不要再重复它。"
            )
        return "\n\n".join(parts)

    def _snap_to_mark(
        self,
        x: int,
        y: int,
        marks_map: Dict[int, Any],
        radius: float = 45.0,
    ) -> Optional[Tuple[int, int, int, Any]]:
        """把模型给的像素坐标吸附到最近的已标记元素中心。

        VLM 自己估坐标经常差几十像素（表现为「点在目标左边一点」）。
        若它给的点落在某个标记元素附近，就认为它想点的是那个元素。
        返回 (x, y, 编号, 元素) 或 None。
        """
        if not marks_map:
            return None
        best = None
        best_d = None
        for num, el in marks_map.items():
            ex, ey = el.cx, el.cy
            d = ((ex - x) ** 2 + (ey - y) ** 2) ** 0.5
            if d <= radius and (best_d is None or d < best_d):
                best_d = d
                best = (int(round(ex)), int(round(ey)), num, el)
        return best

    def _build_subtask_hint(self) -> str:
        """计划已确定时，明确把「当前子任务」摆到模型面前。

        这一段是防止「拆分↔观察」死循环的关键：只要上下文里出现计划，
        就告诉模型「不要再拆分，去执行」，并把当前子任务原样复述一遍。
        """
        subs = self.state.subtasks
        if not subs:
            return ""
        idx = max(0, min(self.state.current_subtask, len(subs) - 1))
        lines = ["【子任务计划（系统已确认，禁止再次拆分）】"]
        for i, s in enumerate(subs):
            if i < idx:
                mark = "✅ 已完成"
            elif i == idx:
                mark = "→ 当前执行"
            else:
                mark = "待执行"
            lines.append(f"  [{i + 1}/{len(subs)}] {mark}：{s}")
        lines.append(
            f"现在请只输出「完成当前子任务」所需的**一个具体动作**"
            f"（click / type / key / scroll / skill 等）。"
            f"**禁止再输出 plan**（会被系统直接忽略并记违规）；"
            f"只有当前子任务确实完成了，才输出 subtask_done 推进到下一个。"
        )
        return "\n".join(lines)

    def _trace_plan(
        self,
        *,
        run_dir,
        step: int,
        obs_img,
        marks_map: Dict[int, Any],
        marks_desc: str,
        a11y_stats: Dict[str, Any],
        action: Action,
        raw_text: str,
        result_desc: str,
    ) -> None:
        """plan 步骤的排障记录（没有坐标，单独包一层省去重复参数）。"""
        self._save_trace(
            run_dir=run_dir,
            step=step,
            obs_img=obs_img,
            marks_count=len(marks_map or {}),
            marks_desc=marks_desc,
            a11y_stats=a11y_stats or {},
            action=action,
            raw_x=None,
            raw_y=None,
            raw_text=raw_text,
            result_desc=result_desc,
        )

    def _prepare_desktop(self, task: str) -> None:
        """任务开始前隐藏其它应用（⌘H 语义，不退出、不关窗）。"""
        try:
            report = clean_desktop(
                task=task,
                keep=self.reset_desktop_keep,
                exclude_pids=[os.getpid()],
            )
        except Exception as e:  # 清场失败不影响主流程
            log.warning("清空桌面失败（忽略，继续执行）：%s", e)
            return
        self._clean_report = report
        msg = report.summary()
        self._emit({"type": "prepare", "message": msg, "hidden": report.hidden})
        if report.hidden:
            log.info("已清空桌面：%s", msg)
        else:
            log.info("清场：%s", msg)

    def _build_prepare_hint(self, step: int) -> str:
        """第一步告诉模型「清场模式已生效」，让它从程序坞打开目标应用。"""
        r = self._clean_report
        if step > 1 or r is None:
            return ""
        lines = ["【清场模式】本轮只标记「当前最前面的应用 + 程序坞」，背景应用的按钮不会被标记。"]
        if r.hidden:
            kept = f"（保留：{'、'.join(r.kept[:4])}）" if r.kept else ""
            lines.append(
                f"任务开始前已隐藏其它 {len(r.hidden)} 个应用{kept}，"
                f"它们只是被隐藏、没有关闭也没有丢状态，点程序坞图标就能原样恢复。"
            )
        elif r.error:
            lines.append(f"（隐藏其它应用失败：{r.error}）")
        else:
            lines.append("（系统不允许隐藏其它应用的窗口，所以没有动它们。）")
        lines.append(
            "如果你要操作的应用**现在不在前台**，请先点程序坞里它的图标把它调到前台，"
            "下一步它的界面控件才会被标记出来。不要凭记忆去点别的窗口。"
        )
        return "\n".join(lines)

    def _build_input_hint(self) -> str:
        """「点了输入框但还没输入文字」时，把模型推回 type 动作。

        这是本地模型最典型的失败模式：点了搜索框 → 又点一次搜索框 → 再点一次，
        然后被重复检测当成"任务已完成"。输入框只需要点一次，之后就该是 type。
        """
        if not self._pending_input:
            return ""
        return (
            f"【系统提示】你上一步已经点击了输入框「{self._pending_input}」，"
            f"但**现在还没有输入任何文字**。输入框只需点一次，它已经聚焦了。\n"
            f'下一步**必须**输出：{{"type": "type", "text": "你要输入的内容"}}，'
            f"把文字打进去（`type` 不需要坐标或编号，直接输到当前焦点）。\n"
            f'输入完成后，如果需要提交，再输出 {{"type": "key", "key": "enter"}}。\n'
            f"**绝对不要再次点击这个输入框**——那不会输入任何东西。"
        )

    def _build_perception_warning(
        self, a11y_stats: Dict[str, Any], marks_map: Dict[int, Any]
    ) -> str:
        """当感知明显不正常时，给模型（和用户）一条明确的提醒。"""
        if not self.use_som:
            return ""
        app_count = a11y_stats.get("app_elements") or 0
        if a11y_stats.get("apps"):
            # 多应用枚举：所有应用内元素的总数
            app_count = sum(int(a.get("elements") or 0) for a in a11y_stats["apps"])
        dock_count = a11y_stats.get("dock_elements") or 0

        if marks_map and not app_count and dock_count:
            return (
                "【系统提示】当前只识别到了屏幕底部的「程序坞图标」，"
                "没能识别到任何窗口内的按钮/输入框。"
                "这通常是因为目标应用没有授予「辅助功能」权限（系统设置 → 隐私与安全性 → 辅助功能）。"
                "请不要去点程序坞图标；如果目标应用已经打开，改用键盘操作"
                "（例如浏览器用 cmd+l 聚焦地址栏再输入）。"
            )
        if not marks_map:
            return (
                "【系统提示】当前没有识别到任何可标记元素，请使用像素坐标，"
                "并且操作后注意观察屏幕是否真的变化了。"
            )
        return ""

    def _save_trace(
        self,
        *,
        run_dir,
        step: int,
        obs_img,
        marks_count: int,
        marks_desc: str,
        a11y_stats: Dict[str, Any],
        action: Action,
        raw_x: Optional[int],
        raw_y: Optional[int],
        raw_text: str,
        result_desc: str,
    ) -> None:
        """把单步的观察/决策/坐标落盘，供事后排障。"""
        if run_dir is None:
            return
        try:
            img_path = run_dir / f"step_{step:02d}.png"
            obs_img.save(img_path)
            record = {
                "step": step,
                "a11y": a11y_stats,
                "marks_total": marks_count,
                "marks_desc": marks_desc,
                "model_raw": raw_text,
                "action_type": action.type,
                "action_mark": action.mark,
                "action_raw_xy": [raw_x, raw_y],
                "action_final_xy": [action.x, action.y],
                "action_text": action.text,
                "action_key": action.key,
                "reason": action.reason,
                "result": result_desc,
                "screenshot": img_path.name,
            }
            with (run_dir / "trace.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.debug("写追踪记录失败（忽略）：%s", e)

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
        """外部请求停止（用户点击停止 / 关闭窗口）。"""
        self._user_stopped = True
        self.state.running = False

    def _emit(self, event: Dict[str, Any]) -> None:
        if self.callback:
            try:
                self.callback(event)
            except Exception:
                pass
