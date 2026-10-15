"""输入模块回归测试：中文输入、按键别名、非法按键报错。

背景（用户真实反馈「点了搜索框却没输入」）：本地模型把中文搜索词交给 `type`，
而 pyautogui **无法输入非 ASCII 字符**，会静默什么都不打；
另外模型爱写 `cmd+l`，pyautogui 只认 `command`，同样被静默忽略。
两个 bug 都表现为「动作执行了，但屏幕上什么都没发生」。
"""
import sys

sys.path.insert(0, "src")

from yu_agent.actions.executor import (  # noqa: E402
    ActionExecutor,
    _clipboard_get,
    _clipboard_set,
    _is_ascii,
    _normalize_key,
)
from yu_agent.actions.schema import Action  # noqa: E402
from yu_agent.common.errors import ActionError  # noqa: E402


class FakePG:
    """记录调用的假 pyautogui。"""

    KEYBOARD_KEYS = [
        "enter", "esc", "tab", "space", "command", "ctrl", "option", "shift",
        "a", "l", "c", "v", "up", "down", "left", "right", "delete",
    ]

    def __init__(self):
        self.written = []
        self.keys = []
        self.hotkeys = []

    def write(self, text, interval=0.0):
        self.written.append(text)

    def keyDown(self, k):
        self.keys.append(("down", k))

    def keyUp(self, k):
        self.keys.append(("up", k))

    def hotkey(self, *keys):
        self.hotkeys.append(tuple(keys))


def make_executor():
    ex = ActionExecutor.__new__(ActionExecutor)  # 跳过 __init__，不碰真 pyautogui
    ex.pg = FakePG()
    return ex


def test_ascii_uses_write():
    ex = make_executor()
    ex.execute(Action(type="type", text="hello 123"))
    assert ex.pg.written == ["hello 123"], ex.pg.written
    assert not ex.pg.hotkeys, "纯 ASCII 不该用剪贴板"
    print("✓ 纯 ASCII 走 write")


def test_chinese_uses_clipboard_paste():
    ex = make_executor()
    pasted = {}

    import yu_agent.actions.executor as exmod

    orig_set, orig_get = exmod._clipboard_set, exmod._clipboard_get
    exmod._clipboard_set = lambda t: (pasted.setdefault("text", t), True)[1]
    exmod._clipboard_get = lambda: "原剪贴板内容"
    try:
        ex.execute(Action(type="type", text="今天天气"))
    finally:
        exmod._clipboard_set, exmod._clipboard_get = orig_set, orig_get

    assert pasted.get("text") == "今天天气", f"没有把中文写进剪贴板：{pasted}"
    assert ex.pg.hotkeys == [("command", "v")], f"没有粘贴：{ex.pg.hotkeys}"
    assert not ex.pg.written, "中文不该走 write（pyautogui 打不出来）"
    print("✓ 中文走剪贴板 + ⌘V")


def test_mixed_text_uses_clipboard():
    ex = make_executor()
    assert not _is_ascii("搜索 today 天气")
    import yu_agent.actions.executor as exmod

    orig_set, orig_get = exmod._clipboard_set, exmod._clipboard_get
    exmod._clipboard_set = lambda t: True
    exmod._clipboard_get = lambda: None
    try:
        ex.execute(Action(type="type", text="搜索 today 天气"))
    finally:
        exmod._clipboard_set, exmod._clipboard_get = orig_set, orig_get
    assert ex.pg.hotkeys == [("command", "v")]
    print("✓ 中英混合也走剪贴板")


def test_key_alias_normalized():
    assert _normalize_key("CMD") == "command"
    assert _normalize_key("control") == "ctrl"
    assert _normalize_key("opt") == "option"
    assert _normalize_key("Return") == "enter"
    ex = make_executor()
    ex.execute(Action(type="key", key="cmd+l"))
    assert ex.pg.keys == [("down", "command"), ("down", "l"), ("up", "l"), ("up", "command")], ex.pg.keys
    print("✓ cmd+l 规范化成 command+l")


def test_invalid_key_raises():
    ex = make_executor()
    try:
        ex.execute(Action(type="key", key="cmd+不存在"))
    except ActionError as e:
        assert "不支持" in str(e)
        print("✓ 非法按键会报错（不再静默忽略）")
        return
    raise AssertionError("非法按键应当抛 ActionError")


def test_enter_and_esc_work():
    ex = make_executor()
    ex.execute(Action(type="key", key="enter"))
    ex.execute(Action(type="key", key="esc"))
    assert ("down", "enter") in ex.pg.keys and ("down", "esc") in ex.pg.keys
    print("✓ enter / esc 可用")


def test_real_clipboard_roundtrip():
    """真实剪贴板读写（macOS AppKit）：中文进出无损坏。"""
    old = _clipboard_get()
    try:
        assert _clipboard_set("今天天气 test"), "写剪贴板失败"
        assert _clipboard_get() == "今天天气 test"
        print("✓ 剪贴板中文读写正常")
    finally:
        if old is not None:
            _clipboard_set(old)


if __name__ == "__main__":
    test_ascii_uses_write()
    test_chinese_uses_clipboard_paste()
    test_mixed_text_uses_clipboard()
    test_key_alias_normalized()
    test_invalid_key_raises()
    test_enter_and_esc_work()
    test_real_clipboard_roundtrip()
    print("\n=== 输入模块测试全部通过 ===")
