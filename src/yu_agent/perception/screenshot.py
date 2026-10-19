"""屏幕截图：跨平台实现，优先用平台原生 API 拿屏幕分辨率与像素。"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Tuple

from PIL import Image

from ..common.errors import EnvError
from ..common.logging import get_logger

log = get_logger("yu_agent.screenshot")

# 正常截屏 < 0.3s。超过这个秒数说明系统侧被阻塞（屏幕锁定 / 显示器休眠 / 录屏被限流），
# 打一条明确的警告，免得用户以为 agent 卡死了。
_SLOW_CAPTURE_S = 3.0


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
    """截取当前整个屏幕，返回 RGB PIL 图像。

    重要：返回的是「逻辑像素 1:1」的图像，与 pyautogui.click() 的坐标系一致。
    这样模型看到的图坐标 == 实际点击坐标，避免 Retina 屏 / 缩放导致的偏移。
    """
    started = time.time()
    img = None
    if sys.platform == "darwin":
        img = _capture_macos()
    elapsed = time.time() - started
    if elapsed >= _SLOW_CAPTURE_S:
        log.warning(
            "截屏耗时 %.1fs（异常）：通常是屏幕已锁定/显示器休眠/录屏权限受限。"
            "锁屏期间模拟点击也会失效。", elapsed,
        )
    if img is not None:
        return img
    # 通用路径：pyautogui（screenshot() 返回的就是逻辑像素 1:1）
    try:
        import pyautogui

        return pyautogui.screenshot().convert("RGB")
    except Exception as e:
        raise EnvError(f"截屏失败：{e}。macOS 需在「系统设置→隐私与安全→屏幕录制」授权。") from e


def _capture_macos():
    """macOS 用 Quartz 抓屏，并缩放到「逻辑像素」尺寸。

    Quartz 的 CGWindowListCreateImage 在 Retina 屏上返回物理像素（2x），
    而 pyautogui 的坐标是逻辑像素。这里按缩放因子缩回逻辑像素，保证坐标系一致。
    """
    try:
        import Quartz  # type: ignore
        from PIL import Image as _Image

        main_id = Quartz.CGMainDisplayID()
        rect = Quartz.CGDisplayBounds(main_id)
        logical_w = int(rect.size.width)
        logical_h = int(rect.size.height)

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

        img = _Image.frombuffer(
            "RGBA", (width, height), data, "raw", "BGRA", bytes_per_row, 1
        ).convert("RGB")

        # 若物理像素尺寸 != 逻辑像素尺寸（Retina 2x），缩回逻辑像素
        if (width, height) != (logical_w, logical_h):
            img = img.resize((logical_w, logical_h), _Image.LANCZOS)
        return img
    except Exception:
        return None


def region_to_pixel(x: float, y: float, scale_x: float, scale_y: float) -> Tuple[int, int]:
    """把模型返回的坐标（基于缩放后的图）还原为屏幕逻辑像素坐标。

    scale_x / scale_y = 原图宽度/缩放图宽度（= 还原比例）。
    """
    return int(round(x * scale_x)), int(round(y * scale_y))
