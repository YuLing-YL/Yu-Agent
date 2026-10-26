"""pyautogui 驱动的底层封装（薄封装，供 executor 或技能使用）。"""
from __future__ import annotations


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> None:
    import pyautogui

    pyautogui.click(x, y, button=button, clicks=clicks)


def type_text(text: str, interval: float = 0.02) -> None:
    import pyautogui

    pyautogui.write(text, interval=interval)


def press_keys(*keys: str) -> None:
    import pyautogui

    pyautogui.hotkey(*keys)


def move_to(x: int, y: int, duration: float = 0.1) -> None:
    import pyautogui

    pyautogui.moveTo(x, y, duration=duration)


def scroll(amount: int, x: int | None = None, y: int | None = None) -> None:
    import pyautogui

    if x is not None and y is not None:
        pyautogui.moveTo(x, y)
    pyautogui.scroll(amount)
