"""后台执行线程：先做意图识别，再决定是否调用 GUI agent。

与界面的双向交互（危险动作确认 / 向用户提问 / 暂停继续）都走这里：
worker 线程阻塞等待，主线程弹窗，回答通过 `provide_*` 回填。
这样 agent 主循环里不需要出现任何 Qt 代码。
"""
from __future__ import annotations

import threading
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from ..agent.control import RunControl
from ..agent.responder import Responder
from ..agent_facade import YuAgent
from ..safety.risk import Risk
from ..settings import ModelConfig


class AgentWorker(QThread):
    """后台线程：意图识别 → （可选）GUI agent 执行。"""

    # 信号
    # 注意：这里**不能**把信号命名为 finished —— QThread 自带一个无参的 finished
    # 信号，在子类里用 Signal(dict) 覆盖会让基类那个信号彻底消失（Qt 内部靠它
    # 做清理），所以本类的「任务结束」信号叫 completed。
    event = Signal(dict)          # 过程事件
    reply = Signal(str)           # AI 给用户的回复文字
    completed = Signal(dict)      # 最终 state dict
    approval_needed = Signal(dict)  # 危险动作需要确认
    question_asked = Signal(dict)   # agent 想向用户提问

    def __init__(
        self,
        task: str,
        model_config: Optional[ModelConfig] = None,
        dry_run: bool = False,
        step_mode: bool = False,
    ):
        super().__init__()
        self.task = task
        self.model_config = model_config
        self.dry_run = dry_run
        self.step_mode = step_mode
        self._agent: Optional[YuAgent] = None
        # 人机往返：主线程把结果写进这两个槽，再 set 事件放行 worker
        self._approval_event = threading.Event()
        self._approval_result = False
        self._answer_event = threading.Event()
        self._answer_text = ""

    # ---- 子线程 → 主线程（阻塞等待回答）----
    def _ask_approval(self, description: str, risk: Risk) -> bool:
        self._approval_event.clear()
        self._approval_result = False
        self.approval_needed.emit(
            {
                "description": description,
                "level": risk.level,
                "category": risk.category,
                "detail": risk.detail,
            }
        )
        # 最多等 5 分钟；没人理就当作拒绝（不可逆动作宁可不做）
        self._approval_event.wait(timeout=300)
        return self._approval_result

    def _ask_question(self, question: str, options: List[str]) -> str:
        self._answer_event.clear()
        self._answer_text = ""
        self.question_asked.emit({"question": question, "options": list(options or [])})
        self._answer_event.wait(timeout=180)
        return self._answer_text

    # ---- 主线程 → 子线程（回填答案）----
    def provide_approval(self, ok: bool) -> None:
        self._approval_result = bool(ok)
        self._approval_event.set()

    def provide_answer(self, text: str) -> None:
        self._answer_text = str(text or "")
        self._answer_event.set()

    def run(self) -> None:
        try:
            control = RunControl(
                asker=self._ask_question, approver=self._ask_approval
            )
            # 1. 构建 agent（先建，拿到后端做意图识别）
            self._agent = YuAgent(
                model_config=self.model_config,
                dry_run=self.dry_run,
                callback=self._on_event,
                control=control,
            )
            # 单步调试：主循环每执行完一步就挂起，等界面点「下一步」
            try:
                self._agent.loop.step_mode = bool(self.step_mode)
            except Exception:
                pass

            # 2. 意图识别 + 生成回复
            responder = Responder(self._agent.backend)
            result = responder.respond(self.task)
            needs_gui = result.get("needs_gui", True)
            reply_text = result.get("reply", "")
            self.reply.emit(reply_text)

            if not needs_gui:
                # 纯对话，不调用 agent
                self.completed.emit(
                    {
                        "success": True,
                        "steps": 0,
                        "reason": "对话回复（无需操作屏幕）",
                        "skipped": True,
                        "reply": reply_text,
                    }
                )
                return

            # 3. 需要操作屏幕 → 启动 agent（start 事件由主循环自己发出，这里不重复）
            state = self._agent.run(self.task)
            out = state.to_dict()
            out["reply"] = reply_text
            self.completed.emit(out)

        except Exception as e:
            self.event.emit({"type": "error", "message": str(e)})
            self.completed.emit(
                {"success": False, "reason": str(e), "steps": 0}
            )
        finally:
            if self._agent:
                try:
                    self._agent.close()
                except Exception:
                    pass

    def stop(self) -> None:
        if self._agent:
            self._agent.stop()
        # 若正卡在等人回答，先放行，否则 worker 会等到超时才退出
        self._approval_event.set()
        self._answer_event.set()

    def pause(self) -> None:
        if self._agent:
            self._agent.pause()

    def resume(self) -> None:
        if self._agent:
            self._agent.resume()

    def next_step(self) -> None:
        """单步调试：放行下一步。"""
        try:
            if self._agent:
                self._agent.loop.request_next_step()
        except Exception:
            pass

    def _on_event(self, event: dict) -> None:
        self.event.emit(event)
