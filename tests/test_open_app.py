"""打开应用（open_app / show_dock）回归测试。

背景（2026-10-26 用户反馈）：Dock 设为自动隐藏时，让 agent 打开软件它就找不到 ——
Dock 图标在屏幕外被 a11y 丢弃，编号表里一个都没有，模型只能瞎点屏幕底部。

修复后两条路：
1. `open_app` 技能：`open -a` 直接开（支持中文别名），并回读前台给**真实**结果；
2. `show_dock` 技能：鼠标移到屏幕边缘让 Dock 滑出，下一步再点图标。

三条返回分支必须分清楚，否则模型不知道该补救什么：
- 前台变成目标  → 真成功
- 命令成功但前台没变 → macOS 焦点保护拦住了，指向 show_dock
- 命令失败 → 名字不对，给改进建议
"""
from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from yu_agent.skills import builtin  # noqa: E402
from yu_agent.skills.builtin import _names_match, _resolve_app_name  # noqa: E402

# 保存原始实现：pytest 会先导入全部测试模块再跑用例，
# 直接在模块级改全局会污染同批次其它文件（之前踩过这个坑）。
_ORIG = {
    "run": builtin.subprocess.run,
    "front": builtin._frontmost_app_name,
    "sleep": builtin.time.sleep,
}


def _make_registry():
    return builtin.build_default_registry()


class FakeProc:
    def __init__(self, returncode: int, stderr: str = "", stdout: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@contextmanager
def patched(rc=0, front="Safari", stderr=""):
    """打桩 subprocess.run / 前台应用 / sleep，退出时恢复。"""
    builtin.subprocess.run = lambda *a, **k: FakeProc(rc, stderr=stderr)
    builtin._frontmost_app_name = lambda: front
    builtin.time.sleep = lambda *_: None
    try:
        yield
    finally:
        builtin.subprocess.run = _ORIG["run"]
        builtin._frontmost_app_name = _ORIG["front"]
        builtin.time.sleep = _ORIG["sleep"]


def test_aliases() -> None:
    cases = {
        "文本编辑": "TextEdit",
        "访达": "Finder",
        "系统设置": "System Settings",
        "微信": "WeChat",
        "terminal": "Terminal",
        "Safari": "Safari",
        "某个陌生应用": "某个陌生应用",
    }
    for src, want in cases.items():
        got = _resolve_app_name(src)
        assert got == want, f"{src} → {got}，应为 {want}"
    print(f"✓ 别名表 {len(cases)} 条正确（中文名能换成 open -a 认的英文名）")


def test_name_match() -> None:
    assert _names_match("Finder", "访达"), "Finder 与访达应视为同一应用"
    assert _names_match("Safari", "Safari浏览器")
    assert not _names_match("Safari", "Finder")
    assert not _names_match("", "Safari")
    print("✓ 应用名宽松比对正确")


def test_open_app_success() -> None:
    with patched(rc=0, front="Safari"):
        out = _make_registry().call("open_app", app_name="Safari")
    assert "已打开" in out and "切到前台" in out, out
    assert "show_dock" not in out, "成功时不该提补救步骤"
    print(f"✓ 成功分支：{out}")


def test_open_app_focus_blocked() -> None:
    """命令成功但前台没变 —— macOS 焦点保护，必须指向 show_dock 而不是谎报成功。"""
    with patched(rc=0, front="WorkBuddy"):
        out = _make_registry().call("open_app", app_name="Safari")
    assert "没允许" in out or "切到前台" in out, out
    assert "show_dock" in out, f"应指向 show_dock 补救，实际：{out}"
    assert "已打开「Safari」并" not in out, "不能谎报已切到前台"
    print("✓ 焦点被拦分支：如实说明并指向 show_dock")


def test_open_app_failure() -> None:
    with patched(rc=1, front="WorkBuddy", stderr="Unable to find application named 'xxx'"):
        out = _make_registry().call("open_app", app_name="xxx不存在")
    assert "失败" in out, out
    assert "英文名" in out or "show_dock" in out, out
    print("✓ 失败分支：给出原因和改进建议")


def test_open_app_empty_name() -> None:
    out = _make_registry().call("open_app", app_name="  ")
    assert "失败" in out and "空" in out, out
    print("✓ 空应用名直接报错")


def test_show_dock_moves_to_edge() -> None:
    """show_dock 要把鼠标移到屏幕边缘（默认底部），并等到动画结束。"""
    calls = {}

    fake = types.SimpleNamespace()
    fake.size = lambda: (1512, 982)
    fake.moveTo = lambda x, y: calls.update({"pos": (x, y)})

    orig_pg = sys.modules.get("pyautogui")
    orig_sleep = builtin.time.sleep
    sys.modules["pyautogui"] = fake
    builtin.time.sleep = lambda *_: None
    try:
        out = _make_registry().call("show_dock", edge="bottom")
        assert calls["pos"] == (756, 980), f"应移到底部中央，实际 {calls['pos']}"
        assert "程序坞" in out and "编号" in out, out

        out2 = _make_registry().call("show_dock", edge="left")
        assert calls["pos"][0] <= 2, f"左侧应贴左边，实际 {calls['pos']}"
        assert "左侧" in out2, out2
    finally:
        builtin.time.sleep = orig_sleep
        if orig_pg is not None:
            sys.modules["pyautogui"] = orig_pg
        else:
            sys.modules.pop("pyautogui", None)
    print("✓ show_dock 会移到对应边缘并提示看下一步编号")


def test_prompts_have_both_paths() -> None:
    """提示词里必须把三条路都写清，否则模型只会用其中一条。"""
    from yu_agent.prompts.loader import load_prompt

    for lang in ("en", "zh"):
        text = load_prompt("system", lang)
        assert "open_app" in text, f"{lang} 提示词缺少 open_app"
        assert "show_dock" in text, f"{lang} 提示词缺少 show_dock"
        assert "spotlight_search" in text, f"{lang} 提示词缺少 spotlight_search"
        assert "Dock" in text or "程序坞" in text
    print("✓ 中英文提示词都写了 open_app / spotlight_search / show_dock 三条路")


# ── 应用查找：目录扫描 / mdfind / Spotlight ─────────────────────── #
def test_scan_app_dirs_finds_bundle() -> None:
    """open -a 认不出的名字，靠扫应用程序目录找 .app。"""
    import os
    import shutil

    base = Path(__file__).resolve().parent / "_tmp_appdirs"
    shutil.rmtree(base, ignore_errors=True)
    try:
        (base / "Safari.app").mkdir(parents=True)
        (base / "Sub").mkdir()
        (base / "Sub" / "Fancy App.app").mkdir()

        orig_dirs = builtin._APP_DIRS
        builtin._APP_DIRS = (str(base),)
        try:
            assert builtin._scan_app_dirs("safari").endswith("Safari.app")
            # 空格/大小写不敏感 + 能进一层子目录
            assert builtin._scan_app_dirs("fancyapp").endswith("Fancy App.app")
            assert builtin._scan_app_dirs("不存在的应用") == ""
        finally:
            builtin._APP_DIRS = orig_dirs
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("✓ 目录扫描能定位 .app（含子目录、忽略空格大小写）")


def test_open_app_falls_back_to_scanned_path() -> None:
    """`open -a` 失败时，要用扫到的 .app 完整路径再打开一次。"""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["open", "-a"]:
            return FakeProc(1, stderr="Unable to find application")
        if cmd[:1] == ["open"]:
            return FakeProc(0)
        return FakeProc(1)

    orig = (builtin.subprocess.run, builtin._frontmost_app_name,
            builtin.time.sleep, builtin._scan_app_dirs, builtin._mdfind_app)
    builtin.subprocess.run = fake_run
    builtin._frontmost_app_name = lambda: "Safari"
    builtin.time.sleep = lambda *_: None
    builtin._scan_app_dirs = lambda name: "/Applications/Safari.app"
    builtin._mdfind_app = lambda name: ""
    try:
        out = _make_registry().call("open_app", app_name="Safari")
    finally:
        (builtin.subprocess.run, builtin._frontmost_app_name,
         builtin.time.sleep, builtin._scan_app_dirs, builtin._mdfind_app) = orig

    assert any(c[:1] == ["open"] and c[-1].endswith("Safari.app") for c in calls), calls
    assert "已打开" in out, out
    print("✓ open -a 失败后自动改用目录扫描到的 .app 路径")


def test_open_app_uses_mdfind_as_last_resort() -> None:
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:1] == ["mdfind"]:
            return FakeProc(0, stdout="/Applications/Weird.app\n")
        if cmd[:2] == ["open", "-a"]:
            return FakeProc(1)
        if cmd[:1] == ["open"]:
            return FakeProc(0)
        return FakeProc(1)

    orig = (builtin.subprocess.run, builtin._frontmost_app_name,
            builtin.time.sleep, builtin._scan_app_dirs)
    builtin.subprocess.run = fake_run
    builtin._frontmost_app_name = lambda: "Weird"
    builtin.time.sleep = lambda *_: None
    builtin._scan_app_dirs = lambda name: ""          # 目录里找不到
    try:
        out = _make_registry().call("open_app", app_name="Weird")
    finally:
        (builtin.subprocess.run, builtin._frontmost_app_name,
         builtin.time.sleep, builtin._scan_app_dirs) = orig

    assert any(c[:1] == ["mdfind"] for c in calls), f"应调用 mdfind，实际 {calls}"
    assert any(c[-1].endswith("Weird.app") for c in calls if c[:1] == ["open"]), calls
    print("✓ 目录扫不到时用 mdfind 索引兜底")


def test_spotlight_search_never_types_blind() -> None:
    """Spotlight 没弹出来时绝不能敲键盘（会把字打进当前文档）。"""
    keys = []

    fake_pg = types.SimpleNamespace()
    fake_pg.hotkey = lambda *k: keys.append(("hotkey", tuple(k)))
    fake_pg.press = lambda k: keys.append(("press", k))

    orig = (sys.modules.get("pyautogui"), builtin._spotlight_visible,
            builtin.time.sleep, builtin._frontmost_app_name)
    sys.modules["pyautogui"] = fake_pg
    builtin._spotlight_visible = lambda: False
    builtin.time.sleep = lambda *_: None
    builtin._frontmost_app_name = lambda: "Code"
    try:
        out = _make_registry().call("spotlight_search", app_name="剪映")
    finally:
        pg, vis, slp, fr = orig
        if pg is not None:
            sys.modules["pyautogui"] = pg
        else:
            sys.modules.pop("pyautogui", None)
        builtin._spotlight_visible, builtin.time.sleep, builtin._frontmost_app_name = vis, slp, fr

    assert ("press", "escape") in keys, f"应取消，实际 {keys}"
    assert not any(c[0] == "hotkey" and "v" in c[1] for c in keys), \
        f"不能粘贴任何文字，实际 {keys}"
    assert "没有输入" in out or "没有输入任何字符" in out, out
    print("✓ Spotlight 没弹出时不盲打，直接 Esc 并说明原因")


if __name__ == "__main__":
    test_aliases()
    test_name_match()
    test_open_app_success()
    test_open_app_focus_blocked()
    test_open_app_failure()
    test_open_app_empty_name()
    test_show_dock_moves_to_edge()
    test_prompts_have_both_paths()
    test_scan_app_dirs_finds_bundle()
    test_open_app_falls_back_to_scanned_path()
    test_open_app_uses_mdfind_as_last_resort()
    test_spotlight_search_never_types_blind()
    print("\nALL PASS")
