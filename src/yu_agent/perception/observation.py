"""观察结果：一次 observe 的完整输出。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from PIL import Image


@dataclass
class Observation:
    """当前屏幕的观察结果。"""

    screenshot: Optional[Image.Image] = None
    screenshot_b64: str = ""  # 缩放后的 base64（喂模型用）
    data_url: str = ""  # data URL 形式
    screen_width: int = 0
    screen_height: int = 0
    a11y_tree: List[Dict[str, Any]] = field(default_factory=list)
    ocr_text: str = ""
    window_title: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
