"""内置技能：预写的可复用 Python 函数。

这些技能封装了常见 GUI 操作，模型可以调用它们而不是自己拼 pyautogui 动作，
更可控、更可靠。
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

from .registry import SkillRegistry


def build_default_registry() -> SkillRegistry:
    """构建包含内置技能的注册表。"""
    reg = SkillRegistry()

    @reg.register(
        "open_app",
        "打开指定应用（macOS 用 open，其他平台按名称搜索）",
        params={"app_name": "应用名称，如 'Safari'、'微信'、'访达'"},
        returns="是否成功",
    )
    def open_app(app_name: str) -> str:
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", app_name], check=False)
        else:
            subprocess.run(["open", app_name], check=False)
        time.sleep(1.0)
        return f"已尝试打开 {app_name}"

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
