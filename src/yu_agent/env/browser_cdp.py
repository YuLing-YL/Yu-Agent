"""浏览器 CDP 环境（可选扩展，通过 Playwright 控制浏览器，比点像素更可靠）。"""
from __future__ import annotations

from PIL import Image


class BrowserCDPEnvironment:
    """基于 Playwright 的浏览器环境（可选，需安装 playwright）。"""

    def __init__(self):
        raise NotImplementedError(
            "浏览器 CDP 环境为可选扩展，需安装 playwright（pip install -e '.[browser]'）。"
        )

    def capture(self) -> Image.Image:
        raise NotImplementedError

    def screen_size(self) -> tuple[int, int]:
        raise NotImplementedError
