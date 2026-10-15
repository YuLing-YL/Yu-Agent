"""动作执行器：把 Action 转成 pyautogui 调用。

⚠️ 输入相关的坑（已修，别退回去）：
1. **pyautogui 无法输入非 ASCII 字符**（中文、emoji 等）。`pg.write("今天天气")`
   里每个汉字都不在 pyautogui 的按键映射里，`_keyDown` 会**静默 return**，
   结果是"看起来执行了、实际一个字都没输进去"。所以非 ASCII 文本一律走
   「写剪贴板 + ⌘V 粘贴」。纯 ASCII 仍用 write（不污染用户剪贴板）。
2. **按键别名**：模型很爱写 `cmd+l` / `ctrl+c` / `opt+space`，
   但 pyautogui 只认 `command` / `ctrl` / `option`（`cmd` 和 `control` 不在
   194 个合法键里，同样会被静默忽略，变成只按了一个 `l`）。这里统一规范化。
3. 规范化后仍不合法的键名**不再静默忽略**，而是抛出 ActionError 写进日志，
   否则这类问题永远查不出来。
"""
from __future__ import annotations

import time
from typing import Optional

from ..common.errors import ActionError, SafetyViolation
from ..common.logging import get_logger
from .schema import Action

log = get_logger("yu_agent.executor")

# 按键别名 -> pyautogui 合法键名
_KEY_ALIASES = {
    "cmd": "command",
    "command": "command",
    "super": "command",
    "win": "command",
    "commandorcontrol": "command",
    "control": "ctrl",
    "ctrl": "ctrl",
    "controlleft": "ctrl",
    "opt": "option",
    "option": "option",
    "alt": "alt",
    "altgr": "alt",
    "meta": "command",
    "return": "enter",
    "esc": "esc",
    "escape": "esc",
    "del": "delete",
    "enter": "enter",
    "space": "space",
    "spacebar": "space",
    "tab": "tab",
    "backspace": "backspace",
    "shift": "shift",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
}


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
                self._type_text(action.text)
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

    # ------------------------------------------------------------------ #
    # 输入
    # ------------------------------------------------------------------ #
    def _type_text(self, text: str) -> None:
        """输入文字。非 ASCII（中文等）走剪贴板粘贴，pyautogui 打不出来。"""
        if not text:
            return
        if _is_ascii(text):
            self.pg.write(text, interval=0.02)
            return
        self._paste_text(text)

    def _paste_text(self, text: str) -> None:
        """用剪贴板 + ⌘V 粘贴文字（支持中文）。会临时借用剪贴板，之后还原。"""
        old = _clipboard_get()
        try:
            if not _clipboard_set(text):
                raise ActionError(
                    "无法写入剪贴板，中文无法通过键盘直接输入（pyautogui 不支持非 ASCII）"
                )
            self.pg.hotkey("command", "v")
            # 等目标应用把剪贴板读走再还原，否则慢的应用（Electron 等）可能读到旧内容
            time.sleep(0.35)
        finally:
            if old is not None:
                try:
                    _clipboard_set(old)
                except Exception:
                    pass
        log.info("已通过剪贴板粘贴 %d 个字符（含非 ASCII）", len(text))

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
        """按下一个键或组合键（自动处理 cmd/ctrl 等别名）。"""
        if not key:
            raise ActionError("key 动作缺少 key 字段")
        parts = [_normalize_key(p) for p in str(key).split("+") if p.strip()]
        if not parts:
            raise ActionError(f"key 动作内容无法解析：{key!r}")
        valid = set(self.pg.KEYBOARD_KEYS)
        bad = [p for p in parts if p not in valid]
        if bad:
            raise ActionError(
                f"不支持的按键 {bad}（原始 key={key!r}）。"
                f"可用示例：enter / esc / tab / cmd+l / ctrl+c / alt+tab / f5"
            )
        for p in parts:
            self.pg.keyDown(p)
        for p in reversed(parts):
            self.pg.keyUp(p)
        log.info("按键：%s", "+".join(parts))

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


# ---------------------------------------------------------------------- #
# 小工具
# ---------------------------------------------------------------------- #
def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _normalize_key(raw: str) -> str:
    """把模型写的按键名规范成 pyautogui 认的名字。"""
    k = raw.strip().lower()
    if k in _KEY_ALIASES:
        return _KEY_ALIASES[k]
    return k


def _clipboard_get() -> Optional[str]:
    """读取剪贴板文本（拿不到或不是文本时返回 None）。"""
    try:
        import AppKit

        pb = AppKit.NSPasteboard.generalPasteboard()
        ptype = getattr(AppKit, "NSPasteboardTypeString", "NSStringPboardType")
        return pb.stringForType_(ptype)
    except Exception:
        return None


def _clipboard_set(text: str) -> bool:
    """把文本写入剪贴板，成功返回 True。"""
    try:
        import AppKit
        from Foundation import NSString

        pb = AppKit.NSPasteboard.generalPasteboard()
        ptype = getattr(AppKit, "NSPasteboardTypeString", "NSStringPboardType")
        pb.clearContents()
        return bool(
            pb.setString_forType_(NSString.stringWithString_(text), ptype)
        )
    except Exception as e:
        log.warning("写剪贴板失败：%s", e)
        return False

