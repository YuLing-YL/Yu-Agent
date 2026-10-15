"""后台执行线程：先做意图识别，再决定是否调用 GUI agent。"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QThread, Signal

from ..agent.responder import Responder
from ..agent_facade import YuAgent
from ..settings import ModelConfig, get_settings


class AgentWorker(QThread):
    """后台线程：意图识别 → （可选）GUI agent 执行。"""

    # 信号
    event = Signal(dict)          # 过程事件
    reply = Signal(str)           # AI 给用户的回复文字
    finished = Signal(dict)       # 最终 state dict

    def __init__(
        self,
        task: str,
        model_config: Optional[ModelConfig] = None,
        dry_run: bool = False,
    ):
        super().__init__()
        self.task = task
        self.model_config = model_config
        self.dry_run = dry_run
        self._agent: Optional[YuAgent] = None

    def run(self) -> None:
        try:
            # 1. 构建 agent（先建，拿到后端做意图识别）
            self._agent = YuAgent(
                model_config=self.model_config,
                dry_run=self.dry_run,
                callback=self._on_event,
            )

            # 2. 意图识别 + 生成回复
            responder = Responder(self._agent.backend)
            result = responder.respond(self.task)
            needs_gui = result.get("needs_gui", True)
            reply_text = result.get("reply", "")
            self.reply.emit(reply_text)

            if not needs_gui:
                # 纯对话，不调用 agent
                self.finished.emit(
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
            self.finished.emit(out)

        except Exception as e:
            self.event.emit({"type": "error", "message": str(e)})
            self.finished.emit(
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

    def _on_event(self, event: dict) -> None:
        self.event.emit(event)
