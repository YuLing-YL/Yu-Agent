"""Mock 环境：离线测试用。"""
from __future__ import annotations

from PIL import Image


class MockEnvironment:
    def capture(self) -> Image.Image:
        return Image.new("RGB", (1280, 800), (30, 30, 46))

    def screen_size(self) -> tuple[int, int]:
        return 1280, 800
