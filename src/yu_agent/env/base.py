"""环境抽象基类。"""
from __future__ import annotations

from typing import Protocol

from PIL import Image


class Environment(Protocol):
    """GUI 环境接口：截屏 + 屏幕尺寸。"""

    def capture(self) -> Image.Image: ...

    def screen_size(self) -> tuple[int, int]: ...
