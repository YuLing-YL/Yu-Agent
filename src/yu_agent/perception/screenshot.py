"""屏幕截图：跨平台实现，优先用平台原生 API 拿屏幕分辨率与像素。"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Tuple

from PIL import Image

from ..common.errors import EnvError


@dataclass
class Screen:
    width: int
    height: int


def get_screen_size() -> Screen:
    """获取主屏物理分辨率（逻辑像素）。"""
    try:
        import pyautogui

        w, h = pyautogui.size()
        return Screen(int(w), int(h))
    except Exception:
        pass
    # 兜底：PIL 无法直接取屏幕尺寸，走平台 API
    if sys.platform == "darwin":
        try:
            from Quartz import CGDisplayBounds, CGMainDisplayID  # type: ignore

            rect = CGDisplayBounds(CGMainDisplayID())
            return Screen(int(rect.size.width), int(rect.size.height))
        except Exception:
            pass
    raise EnvError("无法获取屏幕尺寸，请确认安装了 pyautogui 且有屏幕访问权限。")


def capture_screen() -> Image.Image:
    """截取当前整个屏幕，返回 RGB PIL 图像。"""
    if sys.platform == "darwin":
        img = _capture_macos()
        if img is not None:
            return img
    # 通用路径：pyautogui
    try:
        import pyautogui

        return pyautogui.screenshot().convert("RGB")
    except Exception as e:
        raise EnvError(f"截屏失败：{e}。macOS 需在「系统设置→隐私与安全→屏幕录制」授权。") from e


def _capture_macos():
    """macOS 用 Quartz 直接抓屏（无需 pyautogui 的 TCC 权限，但仍需屏幕录制授权）。"""
    try:
        import Quartz  # type: ignore

        rect = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        w = int(rect.size.width)
        h = int(rect.size.height)
        image_ref = Quartz.CGWindowListCreateImage(
            rect,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        if image_ref is None:
            return None
        width = Quartz.CGImageGetWidth(image_ref)
        height = Quartz.CGImageGetHeight(image_ref)
        bytes_per_row = Quartz.CGImageGetBytesPerRow(image_ref)
        data_provider = Quartz.CGImageGetDataProvider(image_ref)
        data = Quartz.CGDataProviderCopyData(data_provider)
        # 转 PIL
        import Quartz  # noqa

        from PIL import Image as _Image

        img = _Image.frombuffer(
            "RGBA", (width, height), data, "raw", "BGRA", bytes_per_row, 1
        ).convert("RGB")
        return img
    except Exception:
        return None


def region_to_pixel(x: int, y: int, img: Image.Image) -> Tuple[int, int]:
    """把模型返回的坐标（已按图像坐标系）转回屏幕像素。

    当前约定：模型输出的坐标直接是屏幕像素坐标（与截图 1:1）。
    若截图被缩放过，需在此按比例还原。
    """
    return int(x), int(y)
