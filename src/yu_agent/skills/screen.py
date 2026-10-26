"""屏幕文字技能：OCR 定位点击 + 读屏。

AX 读不到的界面（Electron / Canvas / 图片型列表）靠这一组技能兜底。
坐标约定：`capture_screen()` 返回的是**逻辑像素**图，OCR 结果直接可点，
不需要再乘/除缩放比（历史坑：拿 pyautogui 物理像素图去点，正好点偏一倍）。
"""
from __future__ import annotations

from typing import Any

from ..perception import ocr
from ..perception.screenshot import capture_screen


def register(reg) -> None:
    """把屏幕文字相关技能注册进技能表。"""

    @reg.register(
        "click_text",
        "按屏幕上**看得见的文字**点击（OCR 定位）。AX 读不到元素时用它："
        "Electron/网页/画布/图片列表里的按钮文字都能命中",
        params={"text": "要点击的文字（尽量用界面上原样的字）"},
        returns="真实结果：命中的原文与点击坐标；没找到会说明 OCR 是否可用",
    )
    def click_text(text: str) -> str:
        needle = (text or "").strip()
        if not needle:
            return "点击失败：没有给出要点击的文字"
        backend = ocr.backend_name()
        if not backend:
            return (
                "无法按文字点击：这台机器没有可用的 OCR"
                "（macOS 自带 Vision 应该可用，请确认 pyobjc-framework-Vision 已安装）。"
                "请改用编号或坐标点击。"
            )

        img = capture_screen()
        box = ocr.find_text(img, needle)
        if box is None:
            return (
                f"屏幕上没有找到文字「{needle}」"
                f"（OCR 后端 {backend}；可能被遮挡、需要滚动，或文字用图片渲染）"
            )

        import pyautogui

        x, y = int(round(box.cx)), int(round(box.cy))
        pyautogui.click(x, y)
        return f"已点击文字「{box.text}」所在位置 ({x}, {y})，识别置信度 {box.confidence:.2f}"

    @reg.register(
        "read_screen_text",
        "读出当前屏幕上的全部文字（OCR）。用于「这页写了什么 / 列表里有哪些项 / 报错内容是什么」"
        "这类需要阅读的任务，比让模型从截图上认字更准",
        params={"max_chars": "最多返回多少字符（默认 2000）"},
        returns="按行拼接的屏幕文字；OCR 不可用或没识别到会说明原因",
    )
    def read_screen_text(max_chars: int = 2000) -> str:
        if not ocr.backend_name():
            return "无法读取屏幕文字：没有可用的 OCR 后端。"
        img = capture_screen()
        text = ocr.screen_text(img, max_chars=int(max_chars))
        if not text.strip():
            return "OCR 没有识别到任何文字（可能是纯图片界面）。"
        return text

    @reg.register(
        "ocr_status",
        "检查 OCR 是否可用（排查「按文字点击」失败时用）",
        params={},
        returns="可用后端的名字，以及一次试跑识别到的文字块数量",
    )
    def ocr_status() -> str:
        backend = ocr.backend_name()
        if not backend:
            return "OCR 不可用：既没有 macOS Vision，也没有 pytesseract。"
        try:
            img = capture_screen()
            boxes: list[Any] = ocr.recognize(img, fast=True)
        except Exception as e:
            return f"OCR 后端 {backend} 可用，但试跑失败：{e}"
        return f"OCR 后端 {backend} 可用；本次试跑识别到 {len(boxes)} 个文字块。"
