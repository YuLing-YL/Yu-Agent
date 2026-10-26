"""运行控制：暂停/继续、问用户、危险动作审批。

把「跟人打交道」的部分从主循环里拆出来。loop 只调
`control.wait_while_paused()` / `control.ask()` / `control.approve()`，
具体是人点弹窗、终端敲字、还是测试里的假函数，它不需要知道。

三种接入方式：
- GUI：传 Qt 回调（阻塞 worker 线程、主线程弹窗）；
- CLI：传 input() 包装（交互式终端）；非交互环境自动降级；
- 测试/无人值守：不传回调，行为退化为「暂停无效、问题返回空、critical 拦下」。
"""
from __future__ import annotations

import threading
from typing import Callable, List, Optional

from ..safety.risk import Risk

# (问题, 选项列表) -> 用户回答；返回空串表示没人回答
Asker = Callable[[str, List[str]], str]
# (动作描述, 风险) -> 是否放行
Approver = Callable[[str, Risk], bool]


class RunControl:
    """一次运行的人机交互通道。"""

    def __init__(
        self,
        asker: Optional[Asker] = None,
        approver: Optional[Approver] = None,
        ask_timeout: float = 180.0,
    ):
        self.asker = asker
        self.approver = approver
        self.ask_timeout = ask_timeout
        self._pause_gate = threading.Event()
        self._pause_gate.set()          # 默认不暂停
        self._paused = False

    # ---- 暂停 / 继续 ----
    def pause(self) -> None:
        self._paused = True
        self._pause_gate.clear()

    def resume(self) -> None:
        self._paused = False
        self._pause_gate.set()

    @property
    def paused(self) -> bool:
        return self._paused

    def wait_while_paused(self, should_abort: Optional[Callable[[], bool]] = None) -> None:
        """暂停时在这里停住；每 0.2s 看一眼是否已被要求停止。

        用带超时的 wait 而不是死等：用户暂停时点「停止」，必须能立刻退出。
        """
        while self._paused:
            if should_abort is not None and should_abort():
                return
            self._pause_gate.wait(0.2)

    # ---- 问用户 ----
    def ask(self, question: str, options: Optional[List[str]] = None) -> str:
        """向用户提问。没人可问时返回空串，由调用方按「自行决定」处理。"""
        if self.asker is None:
            return ""
        try:
            return str(self.asker(question, list(options or [])) or "").strip()
        except Exception:
            return ""

    # ---- 审批 ----
    def approve(self, description: str, risk: Risk) -> bool:
        """危险动作审批。没有审批人时 critical 一律拦下。"""
        if self.approver is None:
            return risk.level != "critical"
        try:
            return bool(self.approver(description, risk))
        except Exception:
            return False


def headless() -> RunControl:
    """无人值守：不暂停、不问、critical 拦下。"""
    return RunControl()


def interactive() -> RunControl:
    """终端交互：input() 问答，危险动作 y/N 确认。"""

    def _ask(question: str, options: List[str]) -> str:
        suffix = f"（可选：{'/'.join(options)}）" if options else ""
        try:
            return input(f"[agent 提问] {question}{suffix}\n> ")
        except (EOFError, KeyboardInterrupt):
            return ""

    def _approve(description: str, risk: Risk) -> bool:
        try:
            ans = input(
                f"[需要确认] 即将执行 {description} —— {risk}\n"
                f"这类操作不可逆，继续吗？输入 y 确认："
            )
        except (EOFError, KeyboardInterrupt):
            return False
        return ans.strip().lower() in ("y", "yes")

    return RunControl(asker=_ask, approver=_approve)
