"""感知包：屏幕截图与观察。"""
from __future__ import annotations

from .observation import Observation
from .screenshot import capture_screen, get_screen_size

__all__ = ["Observation", "capture_screen", "get_screen_size"]
