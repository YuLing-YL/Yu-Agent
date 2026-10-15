"""后台执行线程：把 agent 主循环放到独立线程，避免阻塞 GUI。"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from ..agent_facade import YuAgent


class AgentWorker(QThread):
    """在后台线程里运行 agent。"""

    # 事件信号（转发给 GUI）
    event = Signal(dict)
    finished = Signal(dict)  # 最终 state dict

    def __init__(self, task: str, use_mock: bool = False, dry_run: bool = False):
        super().__init__()
        self.task = task
        self.use_mock = use_mock
        self.dry_run = dry_run
        self._agent: YuAgent | None = None

    def run(self) -> None:
        try:
            self._agent = YuAgent(
                use_mock=self.use_mock,
                dry_run=self.dry_run,
                callback=self._on_event,
            )
            state = self._agent.run(self.task)
            self.finished.emit(state.to_dict())
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
