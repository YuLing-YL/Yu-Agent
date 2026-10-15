"""Set-of-Mark：在截图上绘制编号标记，帮助模型定位元素。

当前版本 VLM 直接输出像素坐标，SoM 作为可选增强保留。
"""
from __future__ import annotations

from PIL import Image


def add_marks(img: Image.Image, marks: list[tuple[int, int, int]]) -> Image.Image:
    """在图像上绘制带编号的标记点。marks: [(x, y, id)]"""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    for x, y, mid in marks:
        draw.ellipse([x - 8, y - 8, x + 8, y + 8], outline="red", width=2)
        draw.text((x + 4, y - 4), str(mid), fill="red")
    return img
