"""坐标处理：归一化坐标与像素坐标互转。"""
from __future__ import annotations

from typing import Tuple


def normalize_to_pixel(
    x: float, y: float, screen_w: int, screen_h: int, mode: str = "normalized_1000"
) -> Tuple[int, int]:
    """把归一化坐标转成像素坐标。

    - normalized_1000: 0~1000 范围
    - normalized_1: 0~1 范围
    - pixel: 直接像素
    """
    if mode == "normalized_1000":
        return int(x / 1000 * screen_w), int(y / 1000 * screen_h)
    if mode == "normalized_1":
        return int(x * screen_w), int(y * screen_h)
    return int(x), int(y)
