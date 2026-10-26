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


def build_default_registry() -> SkillRegistry:
    """构建包含内置技能的注册表。"""
    reg = SkillRegistry()

    @reg.register(
        "open_app",
        "打开/切换到指定应用（macOS 用 open -a，支持英文名与常见中文名）。"
        "**打开应用优先用它，不要去找 Dock 图标**——Dock 可能被隐藏或图标太多点不准",
        params={"app_name": "应用名称，如 'Safari'、'微信'、'文本编辑'"},
        returns="真实结果：说明是否打开成功，并给出当前前台应用名",
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
        for name in candidates:
            try:
                r = subprocess.run(
                    ["open", "-a", name], capture_output=True, text=True, timeout=15
                )
                if r.returncode == 0:
                    opened = True
                    break
                last_err = (r.stderr or r.stdout or "").strip()[:120]
            except Exception as e:
                last_err = str(e)[:120]

        if not opened:
            # 最后一招：AppleScript activate（对某些老应用有效）
            try:
                r = subprocess.run(
                    ["osascript", "-e", f'tell application "{target}" to activate'],
                    capture_output=True, text=True, timeout=10,
                )
                opened = r.returncode == 0
                if not opened:
                    last_err = (r.stderr or "").strip()[:120] or last_err
            except Exception as e:
                last_err = last_err or str(e)[:120]

        # 等应用起来再回读前台，给模型一个**可信**的反馈（而不是"已尝试"）
        time.sleep(1.2)
        front = _frontmost_app_name()

        if front and _names_match(target, front):
            return f"已打开「{raw}」并切到前台（当前前台：{front}）"

        if opened:
            # 命令成功但前台没变：macOS 的焦点保护不允许后台进程把应用抢到前台
            # （实测：连全新启动的应用也抢不到，前台仍停在原应用）。
            # 但它的窗口通常已经出现在**最上层**，而感知层是按窗口前后顺序枚举的，
            # 所以下一步往往能直接在它上面操作。这里给模型一个"先看截图"的指引，
            # 而不是一上来就绕道 show_dock。
            return (
                f"已启动/激活「{raw}」，但系统没允许后台进程把它切到前台"
                f"（当前前台仍是「{front or '未知'}」）。\n"
                f"**下一步先看截图**：如果它的按钮/输入框出现在编号表里，直接在它上面操作，"
                f"点一下就会真正聚焦；如果被别的窗口盖住看不到，再调用 show_dock "
                f"唤醒程序坞、点「{raw}」的图标。"
            )

        hint = f"：{last_err}" if last_err else ""
        return (
            f"打开「{raw}」失败{hint}。\n"
            f"可以改用英文名重试（Safari / TextEdit / Terminal / Finder / System Settings），"
            f"或者调用 show_dock 唤醒程序坞后点它的图标。"
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
        "click_text",
        "通过 OCR 找到屏幕上包含指定文字的位置并点击",
        params={"text": "要查找并点击的文字", "ocr_lang": "OCR 语言，默认 eng"},
    )
    def click_text(text: str, ocr_lang: str = "eng") -> str:
        # 简化实现：依赖 pytesseract；若未安装则报错提示
        try:
            import pytesseract
            import pyautogui
            from PIL import Image
        except ImportError:
            return "未安装 pytesseract，无法 OCR 定位文字。请改用坐标点击。"
        img = pyautogui.screenshot()
        data = pytesseract.image_to_data(img, lang=ocr_lang, output_type=pytesseract.Output.DICT)
        for i, word in enumerate(data["text"]):
            if text.lower() in word.lower():
                x = data["left"][i] + data["width"][i] // 2
                y = data["top"][i] + data["height"][i] // 2
                pyautogui.click(x, y)
                return f"已点击文字『{text}』所在位置 ({x}, {y})"
        return f"未在屏幕上找到文字『{text}』"

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

    return reg
