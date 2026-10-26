"""动作校验：执行后对比屏幕变化，判断动作是否生效。"""
from __future__ import annotations

from PIL import Image

from ..common.image import image_diff_ratio


def verify_screen_changed(before: Image.Image, after: Image.Image, threshold: float = 0.002) -> bool:
    """判断动作后屏幕是否发生变化（变化像素占比 > threshold）。"""
    ratio = image_diff_ratio(before, after)
    return ratio > threshold
