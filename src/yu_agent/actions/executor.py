"""动作执行器：把 Action 转成 pyautogui 调用。"""
from __future__ import annotations

import time
from typing import Optional

from ..common.errors import ActionError, SafetyViolation
from .schema import Action


class ActionExecutor:
    """基于 pyautogui 的动作执行。"""

    def __init__(self, fail_safe: bool = True, pause: float = 0.05):
        import pyautogui

        self.pg = pyautogui
        self.pg.FAILSAFE = fail_safe  # 鼠标移到屏幕左上角可紧急中止
        self.pg.PAUSE = pause

    def execute(self, action: Action) -> None:
        """执行单个动作。"""
        t = action.type
        try:
            if t == "click":
                self._require_coords(action)
                self.pg.click(action.x, action.y)
            elif t == "double_click":
                self._require_coords(action)
                self.pg.doubleClick(action.x, action.y)
            elif t == "right_click":
                self._require_coords(action)
                self.pg.rightClick(action.x, action.y)
            elif t == "type":
                if action.text is None:
                    raise ActionError("type 动作缺少 text 字段")
                self.pg.write(action.text, interval=0.02)
            elif t == "key":
                self._press_key(action.key)
            elif t == "scroll":
                self._scroll(action)
            elif t == "move":
                self._require_coords(action)
                self.pg.moveTo(action.x, action.y, duration=action.duration)
            elif t == "drag":
                self._require_coords(action)
                dx = action.args.get("dx", 0)
                dy = action.args.get("dy", 0)
                self.pg.drag(dx, dy, duration=action.duration)
            elif t == "wait":
                time.sleep(min(action.duration, 10.0))
            elif t in ("done", "finish", "finished"):
                return  # 无操作，仅标记结束
            elif t == "skill":
                # 技能由上层 agent 调用，不在这里执行
                return
            else:
                raise ActionError(f"未知动作类型：{t}")
        except SafetyViolation:
            raise
        except ActionError:
            raise
        except Exception as e:
            raise ActionError(f"执行动作 {t} 失败：{e}") from e

    def _require_coords(self, action: Action) -> None:
        if action.x is None or action.y is None:
            raise ActionError(f"动作 {action.type} 缺少坐标 (x, y)")
        # 坐标越界保护
        try:
            w, h = self.pg.size()
            if not (0 <= action.x <= w and 0 <= action.y <= h):
                raise SafetyViolation(
                    f"坐标 ({action.x}, {action.y}) 超出屏幕范围 ({w}x{h})，已拦截。"
                )
        except SafetyViolation:
            raise
        except Exception:
            pass

    def _press_key(self, key: Optional[str]) -> None:
        if not key:
            raise ActionError("key 动作缺少 key 字段")
        parts = key.lower().split("+")
        for p in parts:
            self.pg.keyDown(p.strip())
        for p in reversed(parts):
            self.pg.keyUp(p.strip())

    def _scroll(self, action: Action) -> None:
        if action.x is not None and action.y is not None:
            self.pg.moveTo(action.x, action.y)
        amount = 0
        if action.text in ("up", "上", "向上"):
            amount = 3
        elif action.text in ("down", "下", "向下"):
            amount = -3
        elif action.text:
            try:
                amount = int(action.text)
            except ValueError:
                amount = -3
        self.pg.scroll(amount)
