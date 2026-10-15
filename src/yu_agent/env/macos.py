"""macOS 环境。"""
from __future__ import annotations

from PIL import Image

from ..perception.screenshot import capture_screen, get_screen_size


class MacOSEnvironment:
    """macOS 桌面环境封装。"""

    def capture(self) -> Image.Image:
        return capture_screen()

    def screen_size(self) -> tuple[int, int]:
        s = get_screen_size()
        return s.width, s.height
