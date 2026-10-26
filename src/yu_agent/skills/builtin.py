"""内置技能：预写的可复用 Python 函数。

这些技能封装了常见 GUI 操作，模型可以调用它们而不是自己拼 pyautogui 动作，
更可控、更可靠。
"""
from __future__ import annotations

import subprocess
import time
from typing import Any

from .registry import SkillRegistry


# macOS 的 `open -a` 只认英文名 / bundle 名，中文名有一半认不出来
# （实测：访达✓ 终端✓ 文本编辑✗ 系统设置✗ 都报 -1728）。
# 这里补一张常见应用的中英文对照，命中就直接换成英文名去开。
_APP_ALIASES = {
    "访达": "Finder", "finder": "Finder", "文件管理器": "Finder",
    "文本编辑": "TextEdit", "textedit": "TextEdit", "记事本": "TextEdit",
    "系统设置": "System Settings", "系统偏好设置": "System Settings",
    "偏好设置": "System Settings", "system settings": "System Settings",
    "system preferences": "System Settings",
    "终端": "Terminal", "terminal": "Terminal",
    "计算器": "Calculator", "预览": "Preview", "照片": "Photos",
    "地图": "Maps", "邮件": "Mail", "日历": "Calendar",
    "备忘录": "Notes", "提醒事项": "Reminders", "音乐": "Music",
    "播客": "Podcasts", "活动监视器": "Activity Monitor",
    "磁盘工具": "Disk Utility", "钥匙串访问": "Keychain Access",
    "字体册": "Font Book", "控制台": "Console", "时间机器": "Time Machine",
    "截图": "Screenshot", "截屏": "Screenshot",
    "微信": "WeChat", "qq": "QQ", "钉钉": "DingTalk", "飞书": "Feishu",
    "浏览器": "Safari", "safari浏览器": "Safari",
    "谷歌浏览器": "Google Chrome", "chrome": "Google Chrome",
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code",
    "code": "Visual Studio Code",
}


def _resolve_app_name(name: str) -> str:
    """把模型给的应用名换成 `open -a` 能识别的名字。"""
    raw = (name or "").strip()
    return _APP_ALIASES.get(raw.lower(), raw)


def _frontmost_app_name() -> str:
    """当前前台应用名（拿不到返回空串）。"""
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName() or "") if app else ""
    except Exception:
        return ""


def _names_match(a: str, b: str) -> bool:
    """应用名宽松比对：英文名 / 本地化名可能不一致（Finder vs 访达）。"""
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    # Finder <-> 访达 这类：别名表里能对应上也算
    return _APP_ALIASES.get(a, "").lower() == b or _APP_ALIASES.get(b, "").lower() == a


# 应用常见的安装位置。Spotlight 索引被关掉时 mdfind 查不到，
# 但「就在应用程序文件夹里」的情况占绝大多数，直接扫目录最快也最稳。
_APP_DIRS = (
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    "~/Applications",
)


def _scan_app_dirs(name: str) -> str:
    """在应用程序目录里按名字找 .app，返回完整路径（找不到返回空串）。

    匹配规则：去掉空格和大小写后相同 > 包含。先比精确再比包含，
    避免「Music」误命中「Music Converter」这类。
    """
    import os

    key = name.strip().lower().replace(" ", "")
    if not key:
        return ""
    exact: list[str] = []
    loose: list[str] = []

    def _collect(dirpath: str, depth: int = 0) -> None:
        try:
            entries = sorted(os.listdir(dirpath))
        except OSError:
            return
        for entry in entries[:400]:            # 目录项太多时别死磕
            full = os.path.join(dirpath, entry)
            if entry.endswith(".app"):
                stem = entry[:-4].lower().replace(" ", "")
                if stem == key:
                    exact.append(full)
                elif key in stem or stem in key:
                    loose.append(full)
            elif depth == 0 and os.path.isdir(full) and not entry.startswith("."):
                # 再往下看一层：/Applications/Adobe xxx/Photoshop.app 这种
                _collect(full, depth=1)

    for d in _APP_DIRS:
        _collect(os.path.expanduser(d))
    if exact:
        return exact[0]
    if loose:
        # 挑路径最短的：/Applications/Xxx.app 比深处某个重名 bundle 更像正主
        return sorted(loose, key=len)[0]
    return ""


def _mdfind_app(name: str) -> str:
    """用 Spotlight 索引找 .app 的完整路径（目录扫描失败时的兜底）。

    纯命令行、零副作用，不需要抢焦点 —— 比用 cmd+space 敲 Spotlight 稳得多。
    """
    queries = (
        f'kMDItemContentType == "com.apple.application-bundle" && '
        f'kMDItemDisplayName == "{name.strip()}*"c',
        f'kMDItemContentType == "com.apple.application-bundle" && '
        f'kMDItemFSName == "{name.strip()}.app"c',
    )
    for q in queries:
        try:
            r = subprocess.run(
                ["mdfind", q], capture_output=True, text=True, timeout=8
            )
        except Exception:
            continue
        paths = [
            p.strip() for p in (r.stdout or "").splitlines()
            if p.strip().endswith(".app") and "/.Trash/" not in p
        ]
        if paths:
            # /Applications 下的优先
            paths.sort(key=lambda p: (0 if p.startswith("/Applications") else 1, len(p)))
            return paths[0]
    return ""


def _paste_text(text: str) -> bool:
    """用剪贴板 + ⌘V 输入（用于 Spotlight 这类只能靠键盘的输入框）。"""
    try:
        import pyautogui
    except Exception:
        return False
    old = None
    try:
        import AppKit

        pb = AppKit.NSPasteboard.generalPasteboard()
        old = pb.stringForType_(AppKit.NSPasteboardTypeString)
        pb.clearContents()
        pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
        pyautogui.hotkey("command", "v")
        return True
    except Exception:
        return False
    finally:
        if old is not None:
            try:
                import AppKit

                pb = AppKit.NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(old, AppKit.NSPasteboardTypeString)
            except Exception:
                pass


def _spotlight_visible() -> bool:
    """Spotlight 搜索框是否真的弹出来了（防止盲敲键盘把字打进文档里）。"""
    try:
        import Quartz

        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
    except Exception:
        return False
    for w in info:
        owner = str(w.get("kCGWindowOwnerName") or "")
        if owner in ("Spotlight", "SpotlightNetworking"):
            b = w.get("kCGWindowBounds") or {}
            if float(b.get("Width", 0)) > 100:
                return True
    return False


def build_default_registry() -> SkillRegistry:
    """构建包含内置技能的注册表。"""
    reg = SkillRegistry()

    @reg.register(
        "open_app",
        "打开/切换到指定应用。内部依次尝试 open -a 应用名 → 扫应用程序目录 → "
        "Spotlight 索引(mdfind) → AppleScript，支持中文名与非标准安装位置。"
        "**屏幕上找不到目标应用时优先用它，不要去找 Dock 图标**——"
        "Dock 可能被隐藏或图标太多点不准",
        params={"app_name": "应用名称，如 'Safari'、'微信'、'文本编辑'"},
        returns="真实结果：说明是否打开成功（含用了哪种方式），并给出当前前台应用名",
    )
    def open_app(app_name: str) -> str:
        raw = (app_name or "").strip()
        if not raw:
            return "打开失败：应用名是空的"

        target = _resolve_app_name(raw)
        # 别名猜错时退回模型给的原名再试一次
        candidates = [target] if target == raw else [target, raw]

        last_err = ""
        opened = False
        how = ""

        def _bundle_id_of(path: str) -> str:
            """从 .app 路径读 bundle id（`open -b` 比 `open 路径` 更容易激活已运行的实例）。"""
            try:
                import plistlib
                import os

                with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
                    return str(plistlib.load(f).get("CFBundleIdentifier") or "")
            except Exception:
                return ""

        for name in candidates:
            # ① 最常见的路：open -a 应用名
            try:
                r = subprocess.run(
                    ["open", "-a", name], capture_output=True, text=True, timeout=15
                )
                if r.returncode == 0:
                    opened, how = True, "open -a"
                    break
                last_err = (r.stderr or r.stdout or "").strip()[:120]
            except Exception as e:
                last_err = str(e)[:120]

            # ② 扫应用程序目录找到 .app 再 open 路径
            #    （`open -a` 认不出的名字、中文名、非标准安装位置都靠这一级）
            path = _scan_app_dirs(name)
            if path:
                try:
                    r = subprocess.run(
                        ["open", path], capture_output=True, text=True, timeout=20
                    )
                    if r.returncode == 0:
                        opened, how = True, f"目录扫描 {path}"
                        break
                except Exception as e:
                    last_err = str(e)[:120]

            # ③ Spotlight 索引（mdfind）兜底 —— 纯命令行，不需要抢焦点
            path = _mdfind_app(name)
            if path:
                try:
                    r = subprocess.run(
                        ["open", path], capture_output=True, text=True, timeout=20
                    )
                    if r.returncode == 0:
                        opened, how = True, f"mdfind {path}"
                        break
                except Exception as e:
                    last_err = str(e)[:120]

        if not opened:
            # ④ 最后一招：AppleScript activate（对某些老应用有效）
            try:
                r = subprocess.run(
                    ["osascript", "-e", f'tell application "{target}" to activate'],
                    capture_output=True, text=True, timeout=10,
                )
                opened = r.returncode == 0
                if opened:
                    how = "AppleScript activate"
                else:
                    last_err = (r.stderr or "").strip()[:120] or last_err
            except Exception as e:
                last_err = last_err or str(e)[:120]

        # 等应用起来再回读前台，给模型一个**可信**的反馈（而不是"已尝试"）
        time.sleep(1.2)
        front = _frontmost_app_name()

        if front and (_names_match(target, front) or _names_match(raw, front)):
            return f"已打开「{raw}」并切到前台（当前前台：{front}）"

        if opened:
            # 命令成功但前台没变：macOS 的焦点保护不允许后台进程把应用抢到前台
            # （实测：连全新启动的应用也抢不到，前台仍停在原应用）。
            # 但它的窗口通常已经出现在**最上层**，而感知层是按窗口前后顺序枚举的，
            # 所以下一步往往能直接在它上面操作。这里给模型一个"先看截图"的指引，
            # 而不是一上来就绕道 show_dock。
            return (
                f"已启动/激活「{raw}」（方式：{how}），但系统没允许后台进程把它切到前台"
                f"（当前前台仍是「{front or '未知'}」）。\n"
                f"**下一步先看截图**：如果它的按钮/输入框出现在编号表里，直接在它上面操作，"
                f"点一下就会真正聚焦；如果被别的窗口盖住看不到，再调用 show_dock "
                f"唤醒程序坞、点「{raw}」的图标。"
            )

        hint = f"：{last_err}" if last_err else ""
        return (
            f"打开「{raw}」失败{hint}。"
            f"已经试过：open -a、扫应用程序目录、Spotlight 索引（mdfind）、AppleScript。\n"
            f"还有两条路：① 换成英文名/官方名重试（Safari / TextEdit / Terminal / Finder）；"
            f"② 调用 spotlight_search（键盘搜索应用名），或用 show_dock 唤醒程序坞点图标。"
        )

    @reg.register(
        "show_dock",
        "唤醒自动隐藏的程序坞：把鼠标移到屏幕边缘并等待图标滑出。"
        "调用后下一步的截图里就能看到程序坞图标及其编号",
        params={"edge": "程序坞所在边缘：bottom / left / right，默认 bottom"},
        returns="唤醒结果，并提示下一步看截图点编号",
    )
    def show_dock(edge: str = "bottom") -> str:
        import pyautogui

        w, h = pyautogui.size()
        e = (edge or "bottom").strip().lower()
        if e.startswith("l") or "左" in e:
            pyautogui.moveTo(2, h // 2)
            where = "左侧"
        elif e.startswith("r") or "右" in e:
            pyautogui.moveTo(w - 2, h // 2)
            where = "右侧"
        else:
            pyautogui.moveTo(w // 2, h - 2)
            where = "底部"
        time.sleep(0.9)  # 等滑出动画结束，否则下一张截图还是空的
        return (
            f"已把鼠标移到屏幕{where}，程序坞应已滑出。"
            f"下一步看截图里程序坞图标的编号，点目标应用的图标即可（不要点空白处）。"
        )

    @reg.register(
        "type_text",
        "在当前位置输入文本",
        params={"text": "要输入的文本", "interval": "每个字符间隔秒数，默认 0.02"},
    )
    def type_text(text: str, interval: float = 0.02) -> str:
        import pyautogui

        pyautogui.write(text, interval=interval)
        return f"已输入文本（{len(text)} 字符）"

    @reg.register(
        "press_hotkey",
        "按下组合快捷键",
        params={"keys": "逗号分隔的键，如 'command,space' 表示 Cmd+Space"},
    )
    def press_hotkey(keys: str) -> str:
        import pyautogui

        key_list = [k.strip() for k in keys.split(",") if k.strip()]
        pyautogui.hotkey(*key_list)
        return f"已按下快捷键 {keys}"

    @reg.register(
        "take_screenshot",
        "截取当前屏幕并保存到指定路径",
        params={"path": "保存路径（可选，默认 runs 目录）"},
    )
    def take_screenshot(path: str = "") -> str:
        import pyautogui

        img = pyautogui.screenshot()
        if not path:
            path = "/tmp/yu_agent_screenshot.png"
        img.save(path)
        return f"截图已保存到 {path}"

    @reg.register(
        "wait",
        "等待指定秒数",
        params={"seconds": "等待的秒数"},
    )
    def wait(seconds: float = 1.0) -> str:
        time.sleep(min(float(seconds), 30.0))
        return f"已等待 {seconds} 秒"

    @reg.register(
        "spotlight_search",
        "用 Spotlight（⌘+空格）搜索并打开应用。"
        "open_app 的所有方式都失败、或应用不在应用程序目录里时用它",
        params={"app_name": "要搜索的应用名（中英文都可以）"},
        returns="搜索结果，说明是否打开成功以及当前前台应用",
    )
    def spotlight_search(app_name: str = "") -> str:
        """键盘唤起 Spotlight 搜应用名并回车。

        ⚠️ 这一步会真的敲键盘，所以必须**先确认 Spotlight 窗口真的弹出来了**
        （`_spotlight_visible()`），否则按下的字符会打进当前应用里
        —— 那可能把用户的文档改坏，比什么都不做糟得多。
        """
        name = (app_name or "").strip()
        if not name:
            return "搜索失败：应用名是空的"
        try:
            import pyautogui
        except Exception as e:
            return f"搜索失败：pyautogui 不可用（{e}）"

        before = _frontmost_app_name()
        pyautogui.hotkey("command", "space")
        time.sleep(0.8)

        if not _spotlight_visible():
            pyautogui.press("escape")
            return (
                "Spotlight 没有弹出来（可能被系统快捷键改动或权限限制），已按 Esc 取消，"
                "没有输入任何字符。请改用 show_dock 唤醒程序坞点图标。"
            )

        # Spotlight 会记住上次的输入，先全选清掉，免得变成「旧词新词」
        pyautogui.hotkey("command", "a")
        time.sleep(0.1)
        if not _paste_text(name):
            pyautogui.press("escape")
            return "搜索失败：无法写入剪贴板，已按 Esc 取消，没有输入任何字符。"

        time.sleep(1.2)
        pyautogui.press("enter")
        time.sleep(2.5)

        front = _frontmost_app_name()
        if front and front != before and (_names_match(name, front) or not before):
            return f"已通过 Spotlight 打开「{name}」（当前前台：{front}）"
        if front and _names_match(name, front):
            return f"已通过 Spotlight 切换到「{name}」（当前前台：{front}）"
        return (
            f"已在 Spotlight 里搜索「{name}」并回车，但前台变为了「{front or '未知'}」"
            f"（搜索前是「{before or '未知'}」）。"
            f"请**看截图确认**：应用是否已经打开/在最上层。没打开就 show_dock 点图标。"
        )

    # ── 按领域拆分的技能模块 ────────────────────────────────────
    # 技能越加越多，全塞在这个文件里会变成第二个 loop.py。
    # 每个领域一个模块，各自暴露 register(reg)，这里只做组装。
    from . import clipboard as clipboard_skills
    from . import files as file_skills
    from . import screen as screen_skills

    screen_skills.register(reg)
    file_skills.register(reg)
    clipboard_skills.register(reg)

    return reg
