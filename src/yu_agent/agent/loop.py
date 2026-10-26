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
  6. 单任务步数上限（max_steps）/ 单步超时 / 任务总超时
  7. 重复拆分任务上限（max_plan_rejects）——拦住「拆分↔观察」死循环
  并把「最近动作 + 重复警告」回灌给模型，让它自己意识到该收手。

⚠️ 结构约定（2026-10 重构，别再退回一个大函数）
------------------------------------------------
`run()` 只负责**调度**：循环、暂停/停止、异常计数、收尾。每个阶段是独立方法：
    _observe  截图 + 枚举 + 标注 + 注入扫描      → Frame
    _decide   组装提示词 + 调模型               → Action
    _handle_* 控制类动作（ask / plan / subtask_done）
    _ground   编号/坐标 → 精确点击点（含吸附/OCR 锚定）
    _risk_gate 危险动作审批
    _act      执行
    _verify_change 截图差分（顺带把这一帧留给下一步复用）
每一步的计数器都在 `StepCounters` 里，不再散落成十几个局部变量。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..actions.executor import ActionExecutor
from ..actions.schema import Action
from ..common.errors import (
    ActionError,
    GroundingError,
    ModelError,
    SafetyViolation,
)
from ..common.image import image_to_data_url, is_blank, scale_factor, scale_image
from ..common.logging import get_logger
from ..common.paths import new_run_dir, prune_runs
from ..common.redact import redact_secrets
from ..grounding.resolver import GroundingResolver
from ..grounding.verify import verify_screen_changed
from ..memory import MemoryManager
from ..perception import a11y as a11y_mod
from ..perception.screenshot import capture_screen, get_screen_size, region_to_pixel
from ..perception.som import MarkStabilizer, build_som_mapping, describe_marks
from ..prompts.loader import prompt_language
from ..safety.policy import check_action_allowed, sanitize_context
from ..skills.registry import SkillRegistry
from .anchor import ocr_anchor
from .ask_flow import handle_ask
from .control import RunControl
from .describe import describe_action, describe_screen
from .finish_gate import FinishGate
from .guard import ActionGuard, summarize_texts
from .planner import Planner
from .state import AgentState
from .usage import UsageMeter
from .verify import GoalVerifier, Verdict

log = get_logger("yu_agent.loop")

# 会被判定为「点击」的动作类型（防重复的重点对象）
_CLICK_TYPES = {"click", "double_click", "right_click", "middle_click", "triple_click"}
# 坐标量化网格（逻辑像素）：把相邻几像素的点击视为「同一个目标」
_COORD_GRID = 20
# 「可以输入文字」的角色（点了它们之后，下一步应该是 type 而不是再点一次）
_INPUT_ROLES = {
    "AXTextField", "AXTextArea", "AXSearchField", "AXSecureTextField", "AXComboBox",
    "输入框", "文本域", "搜索框", "密码框", "下拉框",
}
# 角色识别不出来时，用标题/描述里的关键词兜底
_INPUT_KEYWORDS = ("搜索", "输入", "地址", "网址", "search", "url", "输入框", "查找")

# 「单行」输入角色：在这类框里输入新内容，语义几乎总是「替换旧词」
# （搜索框、浏览器地址栏、表单单行输入）。多行文本域不在其中 ——
# 往文档里写正文时全选覆盖会把用户已有内容删掉。
_SINGLE_LINE_INPUT_ROLES = {
    "AXTextField", "AXSearchField", "AXSecureTextField", "AXComboBox",
}

# 这些动作类型不参与「屏幕有没有变化」的校验（它们本身不改界面，或改动是延迟的）
_NO_VERIFY_TYPES = {"done", "finish", "plan", "wait", "subtask_done"}


def _quantize(v: Optional[int]) -> Optional[int]:
    """把坐标量化到网格，消除模型输出的几像素抖动。"""
    if v is None:
        return None
    return int(round(float(v) / _COORD_GRID))


def _dist_to_rect(x: float, y: float, el: Any) -> float:
    """点到元素矩形的距离（点在矩形内部算 0）。"""
    dx = max(el.x - x, 0.0, x - (el.x + el.w))
    dy = max(el.y - y, 0.0, y - (el.y + el.h))
    return (dx * dx + dy * dy) ** 0.5


def _anchor_by_text(
    action: Action,
    marks_map: Dict[int, Any],
    x: int,
    y: int,
    max_dist: float = 220.0,
) -> Optional[Tuple[int, int, int, Any]]:
    """模型没用编号、只给了坐标时，用它的 reason 文字反查元素表。

    这是「给原图让模型自己算坐标」那条路的兜底：模型对像素的估计误差能到
    上百像素（尤其是缩略图上的小目标），但它对「目标叫什么」通常说得很准。
    只要 reason 里唯一命中某个元素、且它给的点也落在该元素附近，
    就用元素的精确坐标替代模型的估计——比硬点模型给的像素稳得多。

    返回 (x, y, 编号, 元素) 或 None。有歧义时返回 None（宁可不改）。
    """
    reason = str(getattr(action, "reason", "") or "")
    if not reason or not marks_map:
        return None
    cands: List[Tuple[int, float, int, Any]] = []
    for num, el in marks_map.items():
        for text in (
            getattr(el, "title", "") or "",
            getattr(el, "description", "") or "",
        ):
            if text and _name_matches(str(text), reason):
                d = _dist_to_rect(x, y, el)
                if d <= max_dist:
                    cands.append((len(str(text).strip()), d, num, el))
                break
    if not cands:
        return None
    # 名字最长（最具体）的优先；长度相同再看谁离模型给的点更近
    cands.sort(key=lambda c: (-c[0], c[1]))
    if len(cands) > 1 and cands[0][0] == cands[1][0]:
        return None
    _, _, num, el = cands[0]
    return int(round(el.cx)), int(round(el.cy)), num, el


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
    """判断这个元素是不是「可以输入文字」的控件。

    注意排除「列表里的文本字段」：Finder/资源管理器/Electron 列表的行标签
    在 AX 里就是 AXTextField，但点它只是选中那一行，不是聚焦输入框。
    误判的后果很严重——下一轮会强推模型去 type，把文字打进文件列表。
    """
    try:
        role = str(getattr(el, "role", "") or "")
        title = str(getattr(el, "title", "") or "")
        desc = str(getattr(el, "description", "") or "")
        in_list = bool(getattr(el, "in_list", False))
    except Exception:
        return False
    if in_list:
        return False
    if role in _INPUT_ROLES:
        return True
    text = f"{title} {desc}"
    return any(k in text for k in _INPUT_KEYWORDS)


def _name_matches(name: str, text: str) -> bool:
    """名字是否出现在文本里（对短名字做长度保护，避免 "C" 匹配一切）。"""
    if not name or not text:
        return False
    n = name.strip().lower()
    t = text.lower()
    if len(n) < 3:
        # 2 个汉字算有效（"下载"），单个字母/数字不算
        cjk = sum(1 for c in n if ord(c) > 127)
        if cjk < 2:
            return False
    return n in t


def _verify_mark_semantics(action: Action, marks_map: Dict[int, Any]) -> Optional[int]:
    """检查「模型说要点的东西」和「它给的编号」是否是同一个元素。

    本地模型非常容易犯这个错：它嘴上说 "click yuling"，手里输出的却还是
    上一轮的编号（那个编号现在指向 iCloud云盘）。SOM 编号如果跨帧漂移，
    这种错误会直接把操作点到完全无关的位置。

    返回纠正后的编号；不需要/无法纠正时返回 None。

    只在「候选唯一」时纠正——模型一句 reason 里提到多个元素时不敢乱动。
    """
    if action.mark is None or action.mark not in marks_map:
        return None
    el = marks_map[action.mark]
    reason = str(action.reason or "")
    if not reason:
        return None
    current = str(getattr(el, "title", "") or "")
    # 当前编号的名字本来就出现在 reason 里 → 没选错
    if _name_matches(current, reason):
        return None

    # 找编号表里「名字出现在 reason 中」的候选，取最长的（最具体）
    cands: List[Tuple[int, int]] = []
    for num, other in marks_map.items():
        if num == action.mark:
            continue
        t = str(getattr(other, "title", "") or "")
        if _name_matches(t, reason):
            cands.append((len(t.strip()), num))
    if not cands:
        return None
    cands.sort(reverse=True)
    top_len = cands[0][0]
    if sum(1 for n, _ in cands if n == top_len) > 1:
        return None  # 有歧义，宁可不改
    # 第二名同样被提到、长度也接近 → 模型可能想点另一个，别自作聪明
    if len(cands) > 1 and cands[1][0] * 1.5 > top_len:
        return None
    return cands[0][1]


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


class _Flow(Enum):
    """单步处理结果的流向。"""

    NEXT = "next"   # 本步到此结束，进入下一步
    STOP = "stop"   # 结束整个任务


@dataclass
class StepCounters:
    """跨步骤的计数器（以前是 run() 里的十几个局部变量）。"""

    consecutive_failures: int = 0
    last_sig: Optional[Tuple[Any, ...]] = None
    repeat_count: int = 0
    no_change_count: int = 0
    sig_totals: Dict[Tuple[Any, ...], int] = field(default_factory=dict)
    click_count: int = 0
    recent: List[str] = field(default_factory=list)
    perception_warned: bool = False
    plan_rejects: int = 0

    def remember(self, line: str) -> None:
        """记一条最近的动作用于回灌给模型（只留最近 6 条）。"""
        self.recent.append(line)
        self.recent[:] = self.recent[-6:]


@dataclass
class Frame:
    """一帧观察结果：模型看到的图 + 元素表 + 诊断信息。"""

    image: Any                      # 送给模型的图（可能已画编号框）
    before: Any                     # 原始截图（未缩放，用于差分/OCR 锚定）
    sx: float
    sy: float
    marks: Dict[int, Any]
    marks_desc: str
    stats: Dict[str, Any]
    screen: Any = None              # 逻辑像素屏幕尺寸（prompt 里要告诉模型）
    data_url: str = ""


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
        control: Optional[RunControl] = None,
    ):
        self.backend = backend
        self.memory = memory
        self.skill_registry = skill_registry
        self.cfg = config or {}
        self.dry_run = dry_run
        self.callback = callback  # 用于 GUI 实时反馈
        # 人机交互通道：暂停/继续、问用户、危险动作审批（GUI/CLI 各自注入）
        self.control = control or RunControl()
        self._guard = ActionGuard(
            self.control,
            require_approval=bool(self.cfg.get("require_approval_for_risky", True)),
        )

        # type_interval_s：纯 ASCII 逐字输入的间隔（中文走剪贴板，不受影响）
        self.executor = ActionExecutor(
            type_interval=float(self.cfg.get("type_interval_s", 0.02))
        )
        self.planner = Planner(backend, skill_registry)
        self.resolver = GroundingResolver(coords_mode=str(self.cfg.get("coords_mode", "pixel")))
        self.state = AgentState()

        # ── 步数 / 超时 ──
        self.max_steps = self.cfg.get("max_steps", 20)
        self.step_timeout_s = float(self.cfg.get("step_timeout_s", 60) or 0)
        self.task_timeout_s = float(self.cfg.get("task_timeout_s", 900) or 0)
        self.max_consecutive_failures = self.cfg.get("max_consecutive_failures", 3)
        self.stop_on_grounding_error = bool(self.cfg.get("stop_on_grounding_error", True))
        # ── 校验 ──
        self.verify_after_action = self.cfg.get("verify_after_action", True)
        self.verify_delay_s = self.cfg.get("verify_delay_s", 0.5)
        self.diff_threshold = self.cfg.get("screenshot_diff_threshold", 0.002)
        self.max_image_side = self.cfg.get("max_image_side", 1568)
        self.use_som = self.cfg.get("use_som", True)
        # ── 安全 ──
        self.forbidden_actions = list(self.cfg.get("forbidden_actions") or [])
        self.deny_shell = bool(self.cfg.get("deny_shell", True))
        # SOM 标记预算：总数 / 程序坞上限 / 最多枚举几个应用
        self.som_max_marks = self.cfg.get("som_max_marks", 120)
        self.som_dock_max = self.cfg.get("som_dock_max_marks", 30)
        self.som_max_apps = self.cfg.get("som_max_apps", 6)
        # ── 防重复 / 防卡死（避免「一直点击停不下来」）──
        self.max_repeats = self.cfg.get("max_repeated_actions", 3)
        self.max_same_target = self.cfg.get("max_same_action_total", 3)
        self.max_same_other = self.cfg.get("max_same_action_total_other", 8)
        self.max_total_clicks = self.cfg.get("max_total_clicks", 12)
        self.max_stuck = self.cfg.get("max_no_change_steps", 3)
        self.max_plan_rejects = self.cfg.get("max_plan_rejects", 2)
        # 「已点击但还没输入文字」的输入框描述（由 _build_input_hint 回灌给模型）
        self._pending_input: Optional[str] = None
        self._pending_input_role: str = ""
        # 标注模式：marks / coords / off
        self.som_mode = str(self.cfg.get("som_mode") or "marks").strip().lower()
        if self.som_mode not in ("marks", "coords", "off"):
            self.som_mode = "marks"
        self.som_frontmost_only = bool(self.cfg.get("som_frontmost_only", True))
        if self.som_frontmost_only:
            self.som_max_apps = 1
        self.som_content_check = bool(self.cfg.get("som_content_check", True))
        # ── OCR 补标注（蓝色框）──
        self.som_ocr = bool(self.cfg.get("som_ocr", True))
        self.som_ocr_max = int(self.cfg.get("som_ocr_max", 30))
        self.som_ocr_min_conf = float(self.cfg.get("som_ocr_min_conf", 0.30))
        # AX 元素已经够多时跳过 OCR（原生应用不必每帧花 0.3s）
        self.som_ocr_skip_ax_elements = int(self.cfg.get("som_ocr_skip_ax_elements", 0) or 0)
        # 运行记录
        self.save_trace = bool(self.cfg.get("save_trace", True))
        self.runs_keep = int(self.cfg.get("runs_keep", 0) or 0)
        self.redact_secrets = bool(self.cfg.get("redact_secrets", True))
        self._run_dir: Optional[Path] = None
        self._next_frame = None       # 上一步 verify 拍到的画面，下一步直接复用
        self._deadline: Optional[float] = None
        # 编号跨帧稳定器：同一个元素在不同帧拿到同一个编号，模型跨帧指代才可靠
        self._mark_stab = MarkStabilizer()
        self._skip_menubar = True
        # ── 单步调试 ──
        self.step_mode = bool(self.cfg.get("step_mode", False))
        self._step_gate = threading.Event()
        self._step_gate.set()
        self.prompt_lang = str(self.cfg.get("prompt_language") or "").strip() or prompt_language()
        self._user_stopped = False
        # ── 结果自校验 ──
        self.verify_goal = bool(self.cfg.get("verify_goal", True))
        self.max_goal_rejects = int(self.cfg.get("max_goal_rejects", 1))
        self._verifier = GoalVerifier(backend, self.prompt_lang)
        self.ocr_anchor_enabled = bool(self.cfg.get("ocr_anchor", True))
        self.text_match_threshold = float(self.cfg.get("text_match_threshold", 0.62) or 0.62)
        self.prefer_a11y_over_pixels = bool(self.cfg.get("prefer_a11y_over_pixels", True))
        self._gate: Optional[FinishGate] = None
        pricing = (self.cfg.get("model_pricing") or {}) if isinstance(self.cfg, dict) else {}
        self.usage = UsageMeter(
            price_in=pricing.get("input_per_1k"),
            price_out=pricing.get("output_per_1k"),
        )
        self._answer_hint = ""
        self._injection_hint = ""
        self.max_replans = int(self.cfg.get("max_replans", 1))
        self._replans = 0
        self._plan_from_memory = False
        self._distill_thread: Optional[threading.Thread] = None
        self._blank_warned = False

    def _t(self, zh: str, en: str) -> str:
        """运行期提示按提示词语言二选一。"""
        return en if self.prompt_lang.lower().startswith("en") else zh

    # ------------------------------------------------------------------ #
    # 主循环（只做调度）
    # ------------------------------------------------------------------ #
    def run(self, task: str) -> AgentState:
        """执行一个任务，直到完成、被用户停止、或触发任一安全阈值。"""
        self._begin_run(task)
        c = StepCounters()
        frame: Optional[Frame] = None
        try:
            while self.state.running and self.state.step < self.max_steps:
                # 暂停：停在这一步之前；用户点「停止」时立刻退出等待
                self.control.wait_while_paused(lambda: self._user_stopped)
                if self._user_stopped:
                    break
                if self._task_timed_out():
                    break

                self.state.step += 1
                step = self.state.step
                started = time.monotonic()
                try:
                    frame = self._observe(step, reuse=frame)
                    action = self._decide(task, frame, c)

                    # 用户可能在模型思考期间点了停止 → 丢弃本步动作，不要执行
                    if self._user_stopped:
                        log.info("用户已停止，丢弃本步动作：%s", action.type)
                        break

                    flow = self._handle_ask(action, step, frame, c)
                    if flow is _Flow.NEXT:
                        continue
                    flow = self._handle_plan(action, step, frame, c)
                    if flow is _Flow.NEXT:
                        continue
                    if flow is _Flow.STOP:
                        break
                    flow = self._handle_subtask_done(action, task, frame, step, c)
                    if flow is _Flow.NEXT:
                        continue
                    if flow is _Flow.STOP:
                        break

                    # 5. ground + safety 检查
                    check_action_allowed(action.type, self.forbidden_actions, self.deny_shell)
                    clicked_el, raw_x, raw_y = self._ground(action, frame, step)
                    if not self._risk_gate(action, clicked_el, step, frame, c):
                        continue
                    self._prepare_input(action)

                    # 6. act
                    result_desc = self._act(action)
                    result_desc += self._watch_input(action, clicked_el)

                    # 7. verify
                    screen_changed, after = self._verify_change(frame, action)
                    if not screen_changed:
                        result_desc += "（屏幕无变化，可能未生效）"
                        c.no_change_count += 1
                    else:
                        c.no_change_count = 0
                    if after is not None:
                        self._next_frame = after  # 下一步直接复用这一帧，省一次全屏截屏

                    # 8. 记录
                    self._record_step(action, frame, step, result_desc, screen_changed)
                    self._emit({"type": "result", "step": step, "result": result_desc})
                    self._trace_step(
                        frame=frame, step=step, action=action,
                        raw_x=raw_x, raw_y=raw_y, result_desc=result_desc,
                    )

                    # 9. 收尾判定（done / 卡死 / 单步）
                    flow = self._post_action(action, task, frame, step, c, result_desc)
                    if flow is _Flow.STOP:
                        break
                    if flow is _Flow.NEXT:
                        continue

                    # 10. 单步耗时（超时算一次失败，连续超时则停）
                    elapsed = time.monotonic() - started
                    if self.step_timeout_s and elapsed > self.step_timeout_s:
                        c.consecutive_failures += 1
                        log.warning(
                            "第 %d 步耗时 %.1fs，超过单步上限 %.0fs",
                            step, elapsed, self.step_timeout_s,
                        )
                        if c.consecutive_failures >= self.max_consecutive_failures:
                            self._auto_stop(
                                step,
                                f"连续 {c.consecutive_failures} 步耗时超过 "
                                f"{self.step_timeout_s:.0f}s，已自动停止。",
                                success=False,
                            )
                            break

                except SafetyViolation as e:
                    log.warning("安全拦截：%s", e)
                    c.consecutive_failures += 1
                    self._emit({"type": "safety", "message": str(e)})
                    if c.consecutive_failures >= self.max_consecutive_failures:
                        self.state.reason = f"安全策略多次拦截：{e}"
                        break

                except GroundingError as e:
                    log.warning("第 %d 步定位失败：%s", step, e)
                    c.consecutive_failures += 1
                    self._emit({"type": "error", "step": step, "message": str(e)})
                    # 定位失败直接终止是可配置的（stop_on_grounding_error）
                    if self.stop_on_grounding_error:
                        self.state.reason = f"定位失败：{e}"
                        break
                    if c.consecutive_failures >= self.max_consecutive_failures:
                        self.state.reason = f"连续失败 {c.consecutive_failures} 次：{e}"
                        break

                except (ModelError, ActionError) as e:
                    log.warning("第 %d 步失败：%s", step, e)
                    c.consecutive_failures += 1
                    self._emit({"type": "error", "step": step, "message": str(e)})
                    if c.consecutive_failures >= self.max_consecutive_failures:
                        self.state.reason = f"连续失败 {c.consecutive_failures} 次：{e}"
                        break

                except Exception as e:
                    log.exception("第 %d 步发生未预期异常", step)
                    c.consecutive_failures += 1
                    self._emit({"type": "error", "step": step, "message": str(e)})
                    if c.consecutive_failures >= self.max_consecutive_failures:
                        self.state.reason = f"未预期异常：{e}"
                        break

        finally:
            self._end_run(task)

        return self.state

    # ------------------------------------------------------------------ #
    # 任务生命周期
    # ------------------------------------------------------------------ #
    def _begin_run(self, task: str) -> None:
        """重置每个任务的状态（不能跨任务沿用）。"""
        self.state = AgentState(task=task, running=True)
        # 收尾闸门按任务重建：它持有 rejects 计数与打回提示，不能跨任务沿用
        self._gate = FinishGate(
            check_fn=self._check_goal,
            state=self.state,
            emit=self._emit,
            translate=self._t,
            max_rejects=self.max_goal_rejects,
            dry_run=self.dry_run,
            logger=log,
        )
        self.memory.start_task(task)
        # 上次做成同类任务时的步骤 → 作为初始计划（不是硬编码脚本，可随时偏离）
        self._plan_from_memory = False
        self._load_remembered_plan(task)
        self._user_stopped = False
        self._pending_input = None
        self._pending_input_role = ""
        self._answer_hint = ""
        self._injection_hint = ""
        self._replans = 0
        self.usage = UsageMeter(
            price_in=self.usage.price_in, price_out=self.usage.price_out
        )
        # 每任务重来一套编号（同一任务内编号保持稳定，跨任务不需要延续）
        self._mark_stab.reset()
        self._next_frame = None
        self._blank_warned = False
        low = task.lower()
        self._skip_menubar = not any(
            k in low for k in ("菜单", "menu", "导出", "export", "另存为", "偏好设置", "preference")
        )
        if self.task_timeout_s > 0:
            self._deadline = time.monotonic() + self.task_timeout_s
        else:
            self._deadline = None

        self._emit({"type": "start", "task": task})
        log.info("开始任务：%s", task)

        # 本次运行的排障记录目录（截图 + trace.jsonl）
        self._run_dir = None
        if self.save_trace:
            try:
                self._run_dir = new_run_dir()
                log.info("本次运行记录目录：%s", self._run_dir)
                if self.runs_keep:
                    removed = prune_runs(self.runs_keep)
                    if removed:
                        log.info("已清理 %d 个过期运行目录（保留最近 %d 次）", removed, self.runs_keep)
            except Exception as e:
                log.warning("无法创建运行记录目录（%s），跳过追踪落盘", e)
        else:
            log.info("已关闭运行记录（loop.save_trace=false），不落盘截图")

    def _end_run(self, task: str) -> None:
        """收尾：状态、长期记忆（后台线程）、统计事件。"""
        self.state.running = False
        if self._user_stopped and not self.state.success:
            self.state.reason = self.state.reason or "任务已被用户停止"

        # 记忆蒸馏会多花一次模型调用，绝不能挡在「任务完成」事件前面：
        # 放到后台线程，界面立刻显示结果，蒸馏完再补一条 memory 事件。
        self._start_distillation(task)

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
        stats = self.usage.summary()
        self.state.usage = stats
        log.info("本次用量：%s", self.usage.one_line())
        self._emit({"type": "stats", **stats, "line": self.usage.one_line()})

    def _start_distillation(self, task: str) -> None:
        """把本次轨迹蒸馏进长期记忆（后台线程，失败不影响任务结果）。"""
        if self.dry_run:
            return
        steps = list(self.state.history)
        success = self.state.success

        def _work() -> None:
            try:
                summarized = self.memory.finish_task(
                    task,
                    steps,
                    success,
                    summarize=self._llm_summarize,
                    language=self.prompt_lang,
                )
            except Exception as e:  # 记忆写入失败绝不影响任务结果
                log.warning("长期记忆蒸馏失败（不影响任务）：%s", e)
                return
            if not summarized:
                return
            log.info(
                "长期记忆已更新（%s）：%s",
                "模型总结" if summarized.get("used_llm") else "模板总结",
                summarized.get("summary", ""),
            )
            self._emit(
                {
                    "type": "memory",
                    "summary": summarized.get("summary", ""),
                    "lesson": summarized.get("lesson", ""),
                    "steps": summarized.get("steps", []),
                    "used_llm": summarized.get("used_llm", False),
                }
            )

        t = threading.Thread(target=_work, name="yu-agent-distill", daemon=True)
        self._distill_thread = t
        t.start()

    def _task_timed_out(self) -> bool:
        """任务总时长是否已经超限（超限则自动停止）。"""
        if not self._deadline or time.monotonic() <= self._deadline:
            return False
        self._auto_stop(
            self.state.step,
            f"任务总时长超过 {self.task_timeout_s:.0f}s，已自动停止（避免无限占用屏幕/额度）。",
            success=False,
        )
        return True

    # ------------------------------------------------------------------ #
    # 1) observe
    # ------------------------------------------------------------------ #
    def _observe(self, step: int, reuse: Optional[Frame] = None) -> Frame:
        """截屏 + 元素标注 + 注入扫描，产出一帧 Frame。"""
        # 上一步 verify 拍到的画面就是「现在」的画面，直接复用（省一次全屏截屏）
        before = self._next_frame
        self._next_frame = None
        if before is None:
            before = capture_screen()
        # 纯黑/纯色帧 = 锁屏、屏幕录制没授权、显示器休眠。模型这时等于瞎点，
        # 必须告诉用户一句，否则只会看到 agent 在空屏幕上乱点还不明白为什么。
        if not self._blank_warned and is_blank(before):
            self._blank_warned = True
            msg = (
                "当前截到的画面是纯黑（或纯色）——通常是屏幕已锁定、显示器休眠，"
                "或没有授予「屏幕录制」权限。模型看不到界面，接下来的点击很可能无效。"
            )
            log.warning(msg)
            self._emit({"type": "notice", "step": step, "message": msg})
        screen = get_screen_size()
        obs_img = scale_image(before, self.max_image_side)
        # 记录缩放比例，用于把模型返回的坐标还原回逻辑像素
        sx, sy = scale_factor(before, obs_img)

        marks_map: Dict[int, Any] = {}
        marks_desc = ""
        a11y_stats: Dict[str, Any] = {}
        if self.som_mode != "off":
            try:
                # 元素坐标是逻辑像素；若截图被缩放过，画框时要换算到图像像素
                img_scale = (1.0 / sx) if sx else 1.0
                draw_boxes = self.som_mode == "marks"
                # AX 元素已经很多时没必要再跑 OCR（原生应用），用上一帧的数量判断
                use_ocr = self.som_ocr
                if use_ocr and self.som_ocr_skip_ax_elements > 0 and reuse is not None:
                    last_ax = int(reuse.stats.get("app_elements") or 0)
                    if last_ax >= self.som_ocr_skip_ax_elements:
                        use_ocr = False
                marked_img, marks_map, _ = build_som_mapping(
                    obs_img,
                    max_elements=self.som_max_marks,
                    coord_scale=img_scale,
                    dock_max=self.som_dock_max,
                    max_apps=self.som_max_apps,
                    language=self.prompt_lang,
                    stabilizer=self._mark_stab,
                    skip_menubar=self._skip_menubar,
                    draw=draw_boxes,
                    content_check=self.som_content_check,
                    shrink=self.som_content_check,
                    use_ocr=use_ocr,
                    ocr_max=self.som_ocr_max,
                    ocr_min_conf=self.som_ocr_min_conf,
                )
                if draw_boxes:
                    obs_img = marked_img
                    marks_desc = describe_marks(marks_map, self.prompt_lang)
                a11y_stats = dict(a11y_mod.last_stats)
                self._log_frame(a11y_stats, marks_map, step)
            except Exception as e:
                log.warning("SOM 标记失败，回退到像素坐标：%s", e)
                marks_map = {}

        # 提示词注入扫描：屏幕上的文字是不可信输入
        if not self._injection_hint and not self.dry_run:
            hit = self._guard.scan_injection(summarize_texts(marks_map))
            if hit:
                en = self.prompt_lang.lower().startswith("en")
                self._injection_hint = self._guard.injection_hint(hit, en=en)
                log.warning("检测到疑似提示词注入：%s", hit)
                self._emit(
                    {
                        "type": "notice",
                        "step": step,
                        "message": f"屏幕上出现可疑指令文字「{hit[:40]}」，已按普通文字处理",
                    }
                )

        return Frame(
            image=obs_img,
            before=before,
            sx=sx,
            sy=sy,
            marks=marks_map,
            marks_desc=marks_desc,
            stats=a11y_stats,
            screen=screen,
            data_url=image_to_data_url(obs_img),
        )

    def _log_frame(self, stats: Dict[str, Any], marks: Dict[int, Any], step: int) -> None:
        """把这一帧的感知数量写进日志，并在界面上给一行摘要。"""
        apps = stats.get("apps") or []
        apps_desc = "、".join(f"{a.get('name')}({a.get('elements')})" for a in apps)
        audit = stats.get("audit") or {}
        ocr_count = sum(1 for el in marks.values() if getattr(el, "kind", "") == "ocr")
        stats["ocr_marks"] = ocr_count
        app_elements = stats.get("app_elements") or 0
        if apps:
            app_elements = sum(int(a.get("elements") or 0) for a in apps)
        log.info(
            "观察：标记 %d 个元素（应用 %s 个：%s；程序坞 %s 个；OCR 补 %d 个；校准 %s；"
            "丢弃空框 %s / 收缩 %s / 窗口外 %s / 无名字图标上限 %s / 预算截断 %s）",
            len(marks),
            stats.get("app_count"),
            apps_desc or "无",
            stats.get("dock_elements"),
            ocr_count,
            stats.get("calibration"),
            audit.get("dropped_empty", 0),
            audit.get("shrunk", 0),
            audit.get("dropped_outside_window", 0),
            audit.get("dropped_unnamed", 0),
            audit.get("truncated_by_limit", 0),
        )
        # AX 一个都没读出来、OCR 却读到了字 → 这个应用没把界面暴露给辅助功能
        if ocr_count and not app_elements:
            log.warning(
                "应用「%s」的 AX 元素为 0，已用 OCR 补了 %d 个文字框；"
                "若目标是无文字的图标按钮，请检查辅助功能权限",
                stats.get("app_name") or "未知",
                ocr_count,
            )
        self._emit(
            {
                "type": "debug",
                "step": step,
                "message": (
                    f"识别到 {len(marks)} 个可点元素"
                    f"（应用 {apps_desc or '无'}"
                    f"；程序坞 {stats.get('dock_elements')} 个"
                    f"；OCR 补 {ocr_count} 个）"
                ),
            }
        )

    # ------------------------------------------------------------------ #
    # 2) decide
    # ------------------------------------------------------------------ #
    def _decide(self, task: str, frame: Frame, c: StepCounters) -> Action:
        """组装提示词并让模型给出下一步动作。"""
        # recall：召回记忆上下文（隔离为不可信内容）
        mem_ctx = sanitize_context(
            self.memory.recall_context(task, self.prompt_lang),
            self.prompt_lang,
        )
        work_ctx = sanitize_context(
            # include_plan=False：子任务计划由 _build_subtask_hint 单独强调
            self.memory.working.context_for_prompt(self.prompt_lang, include_plan=False),
            self.prompt_lang,
        )

        # 提示词顺序 = 重要度递增，模型最后看到的最强
        hint_parts = []
        if self._injection_hint:
            hint_parts.append(self._injection_hint)
        if self._answer_hint:
            hint_parts.append(self._answer_hint)
        hint_parts += [
            self._build_subtask_hint(),
            self._build_progress_hint(c.recent, c.repeat_count, c.no_change_count),
            self._build_input_hint(),
        ]
        if self._gate is not None and self._gate.hint:
            hint_parts.append(self._gate.hint)
        warn = self._build_perception_warning(frame.stats, frame.marks)
        if warn:
            hint_parts.append(warn)
            if not c.perception_warned:  # 只在首次提醒用户，避免刷屏
                c.perception_warned = True
                self._emit({"type": "notice", "step": self.state.step, "message": warn})

        action = self.planner.plan(
            task=task,
            screenshot_data_url=frame.data_url,
            memory_context=mem_ctx,
            working_context=work_ctx,
            screen_size=(frame.screen.width, frame.screen.height),
            # coords 模式不把编号表给模型（编号没画在图上，给了也指不准）
            marks=frame.marks if self.som_mode == "marks" else None,
            marks_desc=frame.marks_desc,
            extra_hint="\n\n".join(p for p in hint_parts if p),
            language=self.prompt_lang,
        )
        self.usage.add(
            getattr(self.planner, "last_usage", None),
            getattr(self.planner, "last_latency_s", 0.0),
            role="planner",
        )
        return action

    # ------------------------------------------------------------------ #
    # 3) 控制类动作
    # ------------------------------------------------------------------ #
    def _handle_ask(
        self, action: Action, step: int, frame: Frame, c: StepCounters
    ) -> Optional[_Flow]:
        """模型需要用户补信息（选哪个账号/哪个文件/要不要覆盖…）。"""
        if action.type != "ask":
            return None
        outcome = handle_ask(action, self.control, self._t)
        self._answer_hint = outcome.hint
        log.info("ask：%s → %s", outcome.question[:60], outcome.description)
        self._emit({"type": "result", "step": step, "result": outcome.description})
        self.memory.add_step(
            step=step,
            observation_summary=describe_screen(
                frame.stats, en=self.prompt_lang.lower().startswith("en")
            ),
            action=f"提问「{outcome.question[:40]}」",
            result=outcome.description,
            effect="changed",
        )
        self.state.history.append(
            {
                "step": step,
                "action": "ask",
                "action_desc": f"提问「{outcome.question[:40]}」",
                "reason": action.reason,
                "result": outcome.description,
            }
        )
        c.remember(f"步骤{step}: 提问 → {outcome.description[:40]}")
        return _Flow.NEXT

    def _handle_plan(
        self, action: Action, step: int, frame: Frame, c: StepCounters
    ) -> Optional[_Flow]:
        """任务拆分：整个任务只接受一次，卡住时允许重规划一次。

        ⚠️ 曾经的 bug：plan 分支直接 continue，既不计数也不进卡死检测，
        于是模型只要每轮都输出 plan，就会无限循环在「拆分 → 观察 → 拆分」。
        """
        if action.type != "plan":
            return None

        subtasks = [
            str(s).strip() for s in (action.args.get("subtasks") or []) if str(s).strip()
        ]

        # 4a. 首次拆分：接受（此后不再允许拆分）
        if subtasks and not self.state.subtasks:
            self.state.subtasks = subtasks
            self.state.current_subtask = 0
            self.memory.set_subtasks(subtasks)
            self._emit({"type": "plan", "subtasks": subtasks})
            log.info("任务拆分为 %d 个子任务：%s", len(subtasks), " / ".join(subtasks))
            self._trace_plan(
                frame=frame, step=step, action=action,
                result_desc=f"已拆分为 {len(subtasks)} 个子任务",
            )
            return _Flow.NEXT

        # 4a2. 卡住了 → 允许重新拆分一次（原计划走不通时唯一的出路）
        stuck = (
            c.repeat_count >= max(2, self.max_repeats - 1)
            or c.no_change_count >= max(1, self.max_stuck - 1)
        )
        if subtasks and stuck and self._replans < self.max_replans:
            self._replans += 1
            old_plan = list(self.state.subtasks)
            self.state.subtasks = subtasks
            self.state.current_subtask = 0
            self.memory.set_subtasks(subtasks)
            self._emit({"type": "plan", "subtasks": subtasks})
            msg = (
                f"原计划走不通，已按新思路重新规划"
                f"（第 {self._replans}/{self.max_replans} 次）："
                + " / ".join(subtasks)
            )
            log.warning(msg)
            self._emit({"type": "notice", "step": step, "message": msg})
            self.state.history.append(
                {
                    "step": step,
                    "action": "plan",
                    "action_desc": f"重新规划（原计划：{' / '.join(old_plan)}）",
                    "reason": action.reason,
                    "result": msg,
                }
            )
            c.remember(f"步骤{step}: 重新规划 → {' / '.join(subtasks)[:50]}")
            return _Flow.NEXT

        # 4b. 不该再拆分 → 忽略 + 计数 + 回灌纠正提示
        c.plan_rejects += 1
        if subtasks:
            reason = f"⚠️ 任务已经拆分过了，忽略这次重复拆分（第 {c.plan_rejects} 次）"
            tip = "现在必须直接执行当前子任务，输出 click / type / key 等具体动作"
        else:
            reason = "⚠️ plan 动作没有给出 subtasks，已忽略"
            tip = "确实需要拆分时，subtasks 必须是非空的字符串列表"
        log.warning("%s：subtasks=%s", reason, subtasks)
        self._emit({"type": "notice", "step": step, "message": f"{reason}。{tip}。"})
        self._emit({"type": "result", "step": step, "result": reason})
        c.remember(f"步骤{step}: 重复拆分被系统忽略（{reason}）")
        self._trace_plan(frame=frame, step=step, action=action, result_desc=reason)
        if c.plan_rejects >= self.max_plan_rejects:
            self._auto_stop(
                step,
                "模型连续把任务反复拆分成子任务、却没有执行任何实际操作，"
                "已自动停止以免陷入死循环。请把任务描述得更具体一些，"
                "或换用能力更强的模型。",
                success=False,
            )
            return _Flow.STOP
        return _Flow.NEXT

    def _handle_subtask_done(
        self, action: Action, task: str, frame: Frame, step: int, c: StepCounters
    ) -> Optional[_Flow]:
        """子任务完成：推进到下一个；全部完成则收尾。"""
        if action.type != "subtask_done":
            return None
        total = len(self.state.subtasks)
        idx = self.state.current_subtask
        if total == 0 or idx >= total - 1:
            # 没有子任务，或已是最后一个 → 整体任务完成（但要先复核）
            if self._finish_if_verified(
                task, action.reason or "全部子任务已完成", frame.image, step, c.recent
            ):
                return _Flow.STOP
            return _Flow.NEXT
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
        return _Flow.NEXT

    # ------------------------------------------------------------------ #
    # 4) ground
    # ------------------------------------------------------------------ #
    def _ground(self, action: Action, frame: Frame, step: int) -> Tuple[Any, Any, Any]:
        """把编号/坐标解析成精确的点击点，返回 (命中的元素, 原始 x, 原始 y)。"""
        marks_map = frame.marks
        raw_x, raw_y = action.x, action.y
        clicked_el = None

        # 编号语义校验：模型说点 A、编号却是 B 时自动纠正
        fixed = _verify_mark_semantics(action, marks_map)
        # _verify_mark_semantics 只在 action.mark 命中 marks_map 时才返回非 None，
        # 显式再判一次是为了让类型收窄（也顺手挡住将来改坏的情况）。
        if fixed is not None and action.mark is not None and action.mark in marks_map:
            old_num, old_el = action.mark, marks_map[action.mark]
            new_el = marks_map[fixed]
            action.mark = fixed
            msg = (
                f"编号纠正：模型说要点「{new_el.title or new_el.label()}」，"
                f"但给的编号 #{old_num} 是「{old_el.title or old_el.label()}」→ 改用 #{fixed}"
            )
            log.warning(msg)
            self._emit({"type": "notice", "step": step, "message": msg})

        if action.mark is not None and action.mark in marks_map:
            el = marks_map[action.mark]
            clicked_el = el
            # 元素坐标本身就是逻辑像素（与点击同一坐标系），不要乘缩放比
            action.x = int(round(el.cx))
            action.y = int(round(el.cy))
            log.info("SOM 定位：#%d -> %s (%d,%d)", action.mark, el.label(), action.x, action.y)
            return clicked_el, raw_x, raw_y

        if action.x is None or action.y is None:
            return None, raw_x, raw_y

        action.x, action.y = region_to_pixel(action.x, action.y, frame.sx, frame.sy)
        if frame.sx != 1.0 or frame.sy != 1.0:
            log.info(
                "坐标还原：(原始 %s,%s) * (%s,%s) → (%s,%s)",
                raw_x, raw_y, frame.sx, frame.sy, action.x, action.y,
            )
        if action.type not in _CLICK_TYPES:
            return None, raw_x, raw_y

        # 模型没用编号、直接给坐标：先几何吸附，再语义锚定，最后 OCR 兜底
        snap = self._snap_to_mark(action.x, action.y, marks_map)
        how = "坐标吸附"
        if snap is None and self.prefer_a11y_over_pixels:
            snap = _anchor_by_text(action, marks_map, action.x, action.y)
            how = "语义锚定"
        if snap is None and not self.dry_run:
            ocr_hit = ocr_anchor(
                action.reason, frame.before,
                near=(action.x, action.y),
                enabled=self.ocr_anchor_enabled,
            )
            if ocr_hit is not None:
                log.info(
                    "OCR 锚定：模型给的 (%d,%d) → 文字「%s」(%d,%d)",
                    action.x, action.y, ocr_hit.text, ocr_hit.x, ocr_hit.y,
                )
                self._emit(
                    {
                        "type": "debug",
                        "step": step,
                        "message": f"OCR 锚定到「{ocr_hit.text}」",
                    }
                )
                action.x, action.y = ocr_hit.x, ocr_hit.y
        if snap is not None:
            sx_, sy_, num, el = snap
            clicked_el = el
            log.info(
                "%s：模型给的 (%d,%d) → #%d %s 中心 (%d,%d)",
                how, action.x, action.y, num, el.label(), sx_, sy_,
            )
            action.x, action.y = sx_, sy_
        elif marks_map:
            log.warning(
                "模型未使用编号、直接给了坐标 (%d,%d)，且附近没有匹配的标记元素，"
                "可能点偏；标记图见本次 runs 目录。",
                action.x, action.y,
            )
        return clicked_el, raw_x, raw_y

    def _snap_to_mark(
        self,
        x: int,
        y: int,
        marks_map: Dict[int, Any],
        radius: float = 45.0,
    ) -> Optional[Tuple[int, int, int, Any]]:
        """把模型给的像素坐标吸附到最近的已标记元素中心。

        距离用「点到元素矩形」而不是「点到元素中心」：
        一个大按钮（比如整行输入框）模型点在左边缘，算中心距离可能超过阈值，
        明明点对了却被判成「没匹配上」，白白丢掉一次吸附机会。
        """
        if not marks_map:
            return None
        best = None
        best_d = None
        for num, el in marks_map.items():
            d = _dist_to_rect(x, y, el)
            if d <= radius and (best_d is None or d < best_d):
                best_d = d
                best = (int(round(el.cx)), int(round(el.cy)), num, el)
        return best

    # ------------------------------------------------------------------ #
    # 5) 风险闸门 / 输入语义
    # ------------------------------------------------------------------ #
    def _risk_gate(
        self, action: Action, clicked_el: Any, step: int, frame: Frame, c: StepCounters
    ) -> bool:
        """删除/发送/支付这类不可逆动作必须先问人。返回是否放行。"""
        target_text = str(getattr(clicked_el, "title", "") or "") if clicked_el else ""
        decision = self._guard.check(
            action.type, target_text=target_text, key=action.key or ""
        )
        if decision.allow:
            if decision.risk is not None and decision.risk.needs_approval:
                log.info("高风险动作已确认：%s", decision.reason or decision.risk)
            return True

        log.warning("%s", decision.reason)
        self._emit({"type": "notice", "step": step, "message": decision.reason})
        blocked = f"未执行：{decision.reason}"
        self.memory.add_step(
            step=step,
            observation_summary=describe_screen(
                frame.stats, en=self.prompt_lang.lower().startswith("en")
            ),
            action=self._describe_step_action(action, frame.marks),
            result=blocked,
            effect="failed",
        )
        c.remember(f"步骤{step}: {blocked[:50]}")
        return False

    def _prepare_input(self, action: Action) -> None:
        """上一步点的是**单行**输入框、这一步又直接 type → 默认「替换」而不是追加。"""
        explicit = any(k in action.args for k in ("replace", "clear", "clear_first"))
        if (
            action.type != "type"
            or self._pending_input_role not in _SINGLE_LINE_INPUT_ROLES
            or explicit
        ):
            return
        # 模型显式写了 replace:false 就是它要追加，尊重它（用 `in` 判断，
        # 不能取真假值，否则 False 会被当成"没写"又覆盖掉）
        action.args["replace"] = True
        log.info(
            "上一步聚焦的是单行输入框（%s），本次输入改为「全选覆盖」", self._pending_input_role
        )
        self._emit(
            {
                "type": "debug",
                "step": self.state.step,
                "message": "输入前自动全选：覆盖输入框里的旧内容",
            }
        )

    # ------------------------------------------------------------------ #
    # 6) act
    # ------------------------------------------------------------------ #
    def _act(self, action: Action) -> str:
        """执行动作，返回结果描述。"""
        self._emit(
            {
                "type": "action",
                "step": self.state.step,
                "action": action.type,
                "reason": action.reason,
                "x": action.x,
                "y": action.y,
                "text": action.text,
                "skill": action.skill,
                "args": action.args,
            }
        )
        if self.dry_run:
            return f"[dry-run] 跳过执行：{action.type}"
        return self._execute(action)

    def _watch_input(self, action: Action, clicked_el: Any) -> str:
        """输入流程看护：点了输入框却没输入文字，是最常见的失败模式。

        返回要追加到 result_desc 的提示文字（没有则空串）。
        """
        if action.type in _CLICK_TYPES and clicked_el is not None and _is_input_like(clicked_el):
            self._pending_input = clicked_el.label()
            self._pending_input_role = str(getattr(clicked_el, "role", "") or "")
            log.info(
                "点击了输入框「%s」：下一轮必须输出 type 输入文字", self._pending_input
            )
            return "（输入框已聚焦：下一步必须用 type 输入文字）"
        if action.type == "type":
            self._pending_input = None
            self._pending_input_role = ""
        return ""

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
        self.executor.execute(action)
        return f"已执行 {action.type}" + (
            f"({action.x},{action.y})" if action.x is not None else ""
        )

    # ------------------------------------------------------------------ #
    # 7) verify
    # ------------------------------------------------------------------ #
    def _verify_change(self, frame: Frame, action: Action) -> Tuple[bool, Any]:
        """动作后截图对比，返回 (屏幕是否变化, 动作后的截图或 None)。

        拍到的这张图会被下一步直接当作观察帧复用，等于把「验证」和「下一步截屏」
        合成一次，整步少一次全屏截图。
        """
        if not self.verify_after_action or action.type in _NO_VERIFY_TYPES:
            return True, None
        time.sleep(self.verify_delay_s)
        after = capture_screen()
        changed = verify_screen_changed(frame.before, after, self.diff_threshold)
        return changed, after

    # ------------------------------------------------------------------ #
    # 8) 记录
    # ------------------------------------------------------------------ #
    def _record_step(
        self,
        action: Action,
        frame: Frame,
        step: int,
        result_desc: str,
        screen_changed: bool,
    ) -> str:
        """写工作记忆 + 历史，返回本步效果（changed/no_change/failed）。"""
        step_action = self._describe_step_action(action, frame.marks)
        step_screen = describe_screen(
            frame.stats, en=str(self.prompt_lang).lower().startswith("en")
        )
        if not screen_changed and action.type not in _NO_VERIFY_TYPES:
            step_effect = "no_change"
        elif result_desc.startswith("执行失败") or "失败" in result_desc[:8]:
            step_effect = "failed"
        else:
            step_effect = "changed"

        self.memory.add_step(
            step=step,
            observation_summary=step_screen,
            action=step_action,
            result=result_desc,
            effect=step_effect,
        )
        self.state.history.append(
            {
                "step": step,
                "action": action.type,
                "action_desc": step_action,
                "reason": action.reason,
                "result": result_desc,
            }
        )
        # 有实质进展 → 上一轮复核打回的意见已过期，别再干扰模型
        if (
            self._gate is not None
            and action.type not in ("done", "finish", "subtask_done", "plan")
            and step_effect != "failed"
        ):
            self._gate.clear_hint()
        return step_effect

    def _clean(self, text: Any) -> str:
        """写盘前脱敏（trace 里可能带模型复述的 key/token）。"""
        return redact_secrets(text) if self.redact_secrets else str(text or "")

    def _trace_step(
        self,
        *,
        frame: Frame,
        step: int,
        action: Action,
        raw_x: Any,
        raw_y: Any,
        result_desc: str,
    ) -> None:
        """把单步的观察/决策/坐标落盘，并把调试面板要的信息推给界面。"""
        img_path = self._save_trace(
            frame=frame, step=step, action=action,
            raw_x=raw_x, raw_y=raw_y, result_desc=result_desc,
        )
        self._emit(
            {
                "type": "step_detail",
                "step": step,
                "image": str(img_path) if img_path else "",
                "marks_total": len(frame.marks),
                "marks_desc": frame.marks_desc,
                "model_raw": self.planner.last_raw_text or "",
                "action": action.type,
                "mark": action.mark,
                "x": action.x,
                "y": action.y,
                "text": action.text,
                "key": action.key,
                "skill": action.skill,
                "args": action.args,
                "reason": action.reason,
                "result": result_desc,
                "run_dir": str(self._run_dir) if self._run_dir else "",
            }
        )

    def _save_trace(
        self,
        *,
        frame: Frame,
        step: int,
        action: Action,
        raw_x: Any,
        raw_y: Any,
        result_desc: str,
    ):
        """写 runs/<ts>/step_NN.png + trace.jsonl。返回截图路径（失败返回 None）。"""
        if self._run_dir is None:
            return None
        try:
            img_path = self._run_dir / f"step_{step:02d}.png"
            frame.image.save(img_path)
            record = {
                "step": step,
                "a11y": frame.stats,
                "marks_total": len(frame.marks),
                "marks_desc": self._clean(frame.marks_desc),
                "model_raw": self._clean(self.planner.last_raw_text),
                "action_type": action.type,
                "action_mark": action.mark,
                "action_raw_xy": [raw_x, raw_y],
                "action_final_xy": [action.x, action.y],
                "action_text": self._clean(action.text or ""),
                "action_key": action.key,
                "reason": self._clean(action.reason or ""),
                "result": self._clean(result_desc),
                "screenshot": img_path.name,
            }
            with (self._run_dir / "trace.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return img_path
        except Exception as e:
            log.debug("写追踪记录失败（忽略）：%s", e)
            return None

    # ------------------------------------------------------------------ #
    # 9) 收尾判定 / 卡死检测
    # ------------------------------------------------------------------ #
    def _post_action(
        self,
        action: Action,
        task: str,
        frame: Frame,
        step: int,
        c: StepCounters,
        result_desc: str,
    ) -> Optional[_Flow]:
        """done / 重复统计 / 卡死 / 单步等待。返回 None 表示继续下一步。"""
        # 9a. 模型说完成 → 复核通过才真的收尾
        if action.is_done:
            if self._finish_if_verified(
                task, action.reason or "任务完成", frame.image, step, c.recent
            ):
                return _Flow.STOP
            return _Flow.NEXT

        # 9b. 目标统计 + 多层防重复
        sig = _action_target_sig(action)
        c.sig_totals[sig] = c.sig_totals.get(sig, 0) + 1
        if sig == c.last_sig:
            c.repeat_count += 1
        else:
            c.last_sig = sig
            c.repeat_count = 1
        c.remember(
            f"步骤{step}: {action.type} {_describe_target(action)} → {result_desc[:40]}"
        )
        if action.type in _CLICK_TYPES:
            c.click_count += 1

        stop = self._check_stuck(
            action=action,
            sig=sig,
            sig_totals=c.sig_totals,
            repeat_count=c.repeat_count,
            no_change_count=c.no_change_count,
            click_count=c.click_count,
        )
        if stop:
            reason, as_success = stop
            self._auto_stop(step, reason, success=as_success)
            return _Flow.STOP

        c.consecutive_failures = 0  # 成功执行，重置失败计数
        # 有「真正推进」的动作 → 清空无效拆分计数
        if action.type != "wait":
            c.plan_rejects = 0

        # 10. 单步调试：本步执行完就挂起，等界面点「下一步」
        if not self._wait_for_next_step(step):
            return _Flow.STOP
        return None

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

        # (d) 屏幕连续无变化 → 操作没生效/界面无响应（不能报成功）
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

    # ------------------------------------------------------------------ #
    # 提示词片段
    # ------------------------------------------------------------------ #
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
                self._t(
                    "【系统记录：本任务最近的实际操作（请勿重复做同样的事）】\n",
                    "[System log: what you actually did recently (do NOT repeat these)]\n",
                )
                + "\n".join(recent)
            )
        if repeat_count >= 2:
            parts.append(
                self._t(
                    f"【系统警告】你已经连续 {repeat_count} 次执行完全相同的动作，"
                    "这几乎一定说明任务已经完成、或者这个动作完全无效。"
                    "请立即停止重复它：如果目标已达成，直接输出 "
                    '{"type": "done", "reason": "已完成"}；'
                    "如果还没达成，必须换一种完全不同的方式。",
                    f"[System warning] You have repeated the exact same action {repeat_count} times. "
                    "That almost always means the goal is already achieved or the action does "
                    "nothing. Stop repeating it: if the goal is achieved output "
                    '{"type": "done", "reason": "done"}; otherwise try a completely different approach.',
                )
            )
        if no_change_count >= 1:
            parts.append(
                self._t(
                    f"【系统提示】上一步操作后屏幕没有任何变化（已连续 {no_change_count} 次），"
                    "说明这个操作没有生效，或任务其实已经完成了。请不要再重复它。",
                    f"[System note] The screen did not change after your last action "
                    f"({no_change_count} in a row). The action had no effect, or the task is "
                    "already done. Do not repeat it.",
                )
            )
        return "\n\n".join(parts)

    def _build_subtask_hint(self) -> str:
        """计划已确定时，明确把「当前子任务」摆到模型面前。

        这一段是防止「拆分↔观察」死循环的关键：只要上下文里出现计划，
        就告诉模型「不要再拆分，去执行」，并把当前子任务原样复述一遍。
        """
        subs = self.state.subtasks
        if not subs:
            return ""
        idx = max(0, min(self.state.current_subtask, len(subs) - 1))
        header = self._t(
            "【子任务计划（系统已确认，禁止再次拆分）】",
            "[Subtask plan (confirmed by the system — do NOT decompose again)]",
        )
        if self._plan_from_memory:
            header += self._t(
                "（来自上次成功的做法，仅供参考：界面和上次不一样就自己换路）",
                " (recalled from a previous success — for reference only; "
                "deviate if the screen differs)",
            )
        lines = [header]
        for i, s in enumerate(subs):
            if i < idx:
                mark = self._t("✅ 已完成", "DONE")
            elif i == idx:
                mark = self._t("→ 当前执行", "CURRENT")
            else:
                mark = self._t("待执行", "pending")
            sep = "：" if not self.prompt_lang.lower().startswith("en") else ": "
            lines.append(f"  [{i + 1}/{len(subs)}] {mark}{sep}{s}")
        lines.append(
            self._t(
                "现在请只输出「完成当前子任务」所需的**一个具体动作**"
                "（click / type / key / scroll / skill 等）。"
                "**禁止再输出 plan**（会被系统直接忽略并记违规）；"
                "只有当前子任务确实完成了，才输出 subtask_done 推进到下一个。",
                "Now output **one concrete action** that advances the CURRENT subtask "
                "(click / type / key / scroll / skill). **Do not output plan again** — it is "
                "ignored and counted as a violation. Only output subtask_done when the current "
                "subtask is really finished.",
            )
        )
        return "\n".join(lines)

    def _build_input_hint(self) -> str:
        """「点了输入框但还没输入文字」时，把模型推回 type 动作。

        这是本地模型最典型的失败模式：点了搜索框 → 又点一次搜索框 → 再点一次，
        然后被重复检测当成"任务已完成"。输入框只需要点一次，之后就该是 type。
        """
        if not self._pending_input:
            return ""
        replace_note_zh = ""
        replace_note_en = ""
        if self._pending_input_role in _SINGLE_LINE_INPUT_ROLES:
            replace_note_zh = (
                "\n这是**单行**输入框（搜索框/地址栏这类）：直接 type 你要的新内容即可，"
                "系统会**自动全选覆盖**框里的旧内容，不要自己再输出 cmd+a / backspace 去删。"
            )
            replace_note_en = (
                "\nThis is a SINGLE-LINE field (search box / address bar): just `type` the new "
                "text — the old content is selected and replaced automatically. Do NOT emit "
                "cmd+a / backspace beforehand."
            )
        return self._t(
            f"【系统提示】你上一步已经点击了输入框「{self._pending_input}」，"
            f"但**现在还没有输入任何文字**。输入框只需点一次，它已经聚焦了。\n"
            f'下一步**必须**输出：{{"type": "type", "text": "你要输入的内容"}}，'
            f"把文字打进去（`type` 不需要坐标或编号，直接输到当前焦点）。\n"
            f'输入完成后，如果需要提交，再输出 {{"type": "key", "key": "enter"}}。\n'
            f"**绝对不要再次点击这个输入框**——那不会输入任何东西。"
            + replace_note_zh,
            f'[System note] You clicked the input field "{self._pending_input}" but have NOT typed '
            f"anything yet. The field only needs one click and it is already focused.\n"
            f'The next action MUST be {{"type": "type", "text": "the text to enter"}} '
            f"(`type` needs no coordinates or mark; it goes to the focused field).\n"
            f'When done, submit with {{"type": "key", "key": "enter"}} if needed.\n'
            f"**Never click that input field again** — it types nothing."
            + replace_note_en,
        )

    def _build_perception_warning(
        self, a11y_stats: Dict[str, Any], marks_map: Dict[int, Any]
    ) -> str:
        """当感知明显不正常时，给模型（和用户）一条明确的提醒。"""
        if not self.use_som:
            return ""
        app_count = a11y_stats.get("app_elements") or 0
        if a11y_stats.get("apps"):
            app_count = sum(int(a.get("elements") or 0) for a in a11y_stats["apps"])
        dock_count = a11y_stats.get("dock_elements") or 0

        # 帧可信度最低：截屏与元素枚举不是同一时刻/同一桌面时，所有红框会整体错位
        if a11y_stats.get("frame_reliable") is False:
            return self._t(
                "【系统提示】这一帧的编号框和屏幕画面**对不上**"
                f"（只有 {a11y_stats.get('frame_content_ratio')} 的框落在有画面的位置），"
                "框的位置不可信，不要照编号点。\n"
                "请直接看画面里**实际显示**的元素，用像素坐标 x/y 输出点击位置。\n"
                "如果画面里根本没有目标窗口，先用 "
                '{"type": "skill", "skill": "open_app", "args": {"app_name": "应用名"}} 切过去。',
                "[System note] The numbered boxes in this frame do NOT line up with the picture "
                f"(only {a11y_stats.get('frame_content_ratio')} of them land on visible content), "
                "so their positions cannot be trusted — do not click by number.\n"
                "Look at the elements that are ACTUALLY visible and output pixel x/y for the click.\n"
                "If the target window is not visible at all, switch to it first with "
                '{"type": "skill", "skill": "open_app", "args": {"app_name": "<app>"}}.',
            )

        if a11y_stats.get("dock_hidden"):
            # 程序坞开了「自动隐藏」：图标在屏幕外，点了也没用
            return self._t(
                "【系统提示】程序坞当前是「自动隐藏」状态，图标在屏幕外，没放进编号表，"
                "点屏幕底部没有反应。打开应用走这两条：\n"
                "① 一步到位："
                '{"type": "skill", "skill": "open_app", "args": {"app_name": "Safari"}}\n'
                "② 必须点图标时，先唤醒程序坞："
                '{"type": "skill", "skill": "show_dock", "args": {"edge": "bottom"}}\n'
                "　 鼠标移到底部后程序坞会滑出，下一张截图里图标就有编号了，再点它。",
                "[System note] The dock is set to auto-hide: its icons are off-screen, they were "
                "left out of the element list, and clicking the bottom edge does nothing. Two ways "
                "to open an app:\n"
                "1) one step: "
                '{"type": "skill", "skill": "open_app", "args": {"app_name": "Safari"}}\n'
                "2) if you really need to click an icon, wake the dock first: "
                '{"type": "skill", "skill": "show_dock", "args": {"edge": "bottom"}}\n'
                "   It slides the dock out; the icons get numbers in the next screenshot — click "
                "them then.",
            )

        if marks_map and not app_count and dock_count:
            return self._t(
                "【系统提示】当前只识别到了屏幕底部的「程序坞图标」，"
                "没能识别到任何窗口内的按钮/输入框。"
                "这通常是因为目标应用没有授予「辅助功能」权限（系统设置 → 隐私与安全性 → 辅助功能）。"
                "请不要去点程序坞图标；如果目标应用已经打开，改用键盘操作"
                "（例如浏览器用 cmd+l 聚焦地址栏再输入）。",
                "[System note] Only dock icons were detected — no buttons or input fields inside any "
                "window. This usually means the target app lacks Accessibility permission "
                "(System Settings → Privacy & Security → Accessibility). Do not click dock icons; "
                "if the app is already open, use the keyboard instead "
                "(e.g. cmd+l to focus the browser address bar, then type).",
            )
        if not marks_map:
            return self._t(
                "【系统提示】当前没有识别到任何可标记元素，请使用像素坐标，"
                "并且操作后注意观察屏幕是否真的变化了。",
                "[System note] No markable elements were detected. Use pixel coordinates and check "
                "whether the screen actually changed after each action.",
            )
        return ""

    # ------------------------------------------------------------------ #
    # 记忆
    # ------------------------------------------------------------------ #
    def _describe_step_action(self, action: Action, marks_map=None) -> str:
        """把动作翻译成一句人话（实现见 agent/describe.py）。"""
        return describe_action(action, marks_map, self.prompt_lang)

    def _llm_summarize(self, system: str, user: str) -> str:
        """给记忆蒸馏用的纯文本总结（不附图，固定低温，短输出）。"""
        from ..models.base import VLMRequest

        resp = self.backend.chat(
            VLMRequest(
                system=system,
                prompt=user,
                images=[],
                temperature=0.2,
                max_tokens=400,
            )
        )
        return str(resp.text or "")

    # ------------------------------------------------------------------ #
    # 结果自校验
    # ------------------------------------------------------------------ #
    def _check_goal(self, task: str, claim: str, obs_img, step: int) -> Verdict:
        """对着「当前这张截图」复核任务是否真的完成。

        用独立的一次调用（不带历史截图、温度 0），只看证据。失败一律返回
        「未验证」——宁可标成未验证，也不要假装成功。
        """
        if not self.verify_goal:
            return Verdict(None, "未开启结果复核", method="disabled")
        if self.dry_run:
            return Verdict(None, "dry-run 不复核", method="dry-run")
        try:
            data_url = image_to_data_url(scale_image(obs_img, self.max_image_side))
        except Exception as e:
            return Verdict(None, f"截图编码失败：{e}", method="error")

        self._emit(
            {
                "type": "debug",
                "step": step,
                "message": f"复核「{claim[:40]}」：对照截图确认是否真的完成…",
            }
        )
        verdict = self._verifier.verify(task, self.state.history, data_url)
        self.usage.add(
            getattr(self._verifier, "last_usage", None),
            getattr(self._verifier, "last_latency_s", 0.0),
            role="verify",
        )
        log.info(
            "结果复核：%s（%s，置信度 %.2f）%s",
            {True: "已完成", False: "未完成", None: "无法验证"}.get(verdict.ok),
            verdict.method,
            verdict.confidence,
            f" — {verdict.reason}" if verdict.reason else "",
        )
        self._emit(
            {
                "type": "verify",
                "step": step,
                "ok": verdict.ok,
                "reason": verdict.reason,
                "method": verdict.method,
                "confidence": verdict.confidence,
            }
        )
        return verdict

    def _finish_if_verified(
        self, task: str, claim: str, obs_img, step: int, recent: List[str]
    ) -> bool:
        """模型声称完成时的统一出口（决策逻辑在 agent/finish_gate.py）。"""
        assert self._gate is not None
        result = self._gate.finish(task, claim, obs_img, step, recent)
        return result.finished

    def _trace_plan(
        self, *, frame: Frame, step: int, action: Action, result_desc: str
    ) -> None:
        """plan 步骤的排障记录（没有坐标）。"""
        self._save_trace(
            frame=frame, step=step, action=action,
            raw_x=None, raw_y=None, result_desc=result_desc,
        )

    def stop(self) -> None:
        """外部请求停止（用户点击停止 / 关闭窗口）。"""
        self._user_stopped = True
        self.state.running = False
        # 若正卡在单步暂停里，立刻放行让它走到 break 分支退出
        self._step_gate.set()

    # ------------------------------------------------------------------ #
    # 记忆：复用上次成功的流程
    # ------------------------------------------------------------------ #
    def _load_remembered_plan(self, task: str) -> None:
        """把上次成功任务的步骤读出来当初始计划。

        只放在「还没有计划」的时候；读不到就什么都不做——绝不能因为记忆里
        有条旧流程就打乱本次任务的正常规划。
        """
        if self.state.subtasks:
            return
        try:
            steps = self.memory.suggest_procedure(task)
        except Exception:
            steps = []
        if len(steps) < 2:
            return
        self.state.subtasks = steps
        self.state.current_subtask = 0
        self.memory.set_subtasks(steps)
        self._plan_from_memory = True
        log.info("复用上次成功的流程（%d 步）：%s", len(steps), " / ".join(steps))
        self._emit({"type": "plan", "subtasks": steps, "source": "memory"})
        self._emit(
            {
                "type": "notice",
                "step": 0,
                "message": "已参考上次成功的做法拆好步骤；界面不同会自动偏离",
            }
        )

    # ------------------------------------------------------------------ #
    # 暂停 / 单步
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        """暂停（当前这一步做完后停住，不会打断正在进行的点击）。"""
        self.control.pause()
        self._emit({"type": "run_paused", "step": self.state.step})
        log.info("已暂停（第 %d 步之后停住）", self.state.step)

    def resume(self) -> None:
        """从暂停处继续。"""
        self.control.resume()
        self._emit({"type": "run_resumed", "step": self.state.step})
        log.info("已继续")

    @property
    def paused(self) -> bool:
        return self.control.paused

    def request_next_step(self) -> None:
        """单步调试：放行下一步（界面点「下一步」时调用）。"""
        self._step_gate.set()

    def _wait_for_next_step(self, step: int) -> bool:
        """单步模式下暂停；返回 False 表示用户已停止，应退出循环。"""
        if not self.step_mode:
            return True
        self._step_gate.clear()
        self._emit({"type": "paused", "step": step, "max_steps": self.max_steps})
        log.info("单步模式：第 %d 步已执行，等待「下一步」", step)
        while not self._step_gate.wait(0.2):
            if self._user_stopped:
                log.info("单步暂停中被用户停止，退出循环")
                return False
        return not self._user_stopped

    def _emit(self, event: Dict[str, Any]) -> None:
        if self.callback:
            try:
                self.callback(event)
            except Exception as e:
                # 界面回调失败不能拖垮任务，但也不能悄悄吞掉（以前是 pass）
                log.debug("事件回调异常（忽略）：%s", e)
