"""Set-of-Mark（SOM）：在截图上绘制带编号的元素框，帮助模型精确指代。

模型看到带编号的截图后，输出 "点击 #5" 之类的指令，
程序通过映射表把编号翻译成精确的屏幕坐标，从而绕开 VLM 估算像素坐标的误差。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .a11y import UIElement, get_all_interactive_elements


def _load_font(size: int):
    """加载一个可用的字体（优先系统字体）。"""
    try:
        import sys

        if sys.platform == "darwin":
            for path in (
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/SFNSText.ttf",
                "/System/Library/Fonts/Menlo.ttc",
            ):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    continue
    except Exception:
        pass
    return ImageFont.load_default()


def draw_marks(
    img: Image.Image,
    elements: List[UIElement],
) -> Tuple[Image.Image, Dict[int, UIElement]]:
    """在图像上绘制编号框，返回 (标注后的图, 编号->元素映射)。

    编号从 1 开始（0 保留给"无对应元素"）。
    超出截图范围的元素会被过滤掉，保证编号连续无空洞。
    """
    W, H = img.size
    # 先过滤掉完全在截图外的元素，保证编号连续
    visible = [
        el for el in elements
        if el.x < W and el.y < H and (el.x + el.w) > 0 and (el.y + el.h) > 0
    ]

    marked = img.copy()
    draw = ImageDraw.Draw(marked)
    font = _load_font(max(14, img.size[0] // 90))

    mapping: Dict[int, UIElement] = {}

    for i, el in enumerate(visible, start=1):
        # 裁剪到截图范围内
        x0, y0 = max(0, el.x), max(0, el.y)
        x1 = min(W, el.x + el.w)
        y1 = min(H, el.y + el.h)

        # 边框
        color = (255, 60, 60)  # 红
        draw.rectangle([x0, y0, x1, y1], outline=color, width=3)

        # 编号标签（左上角带背景的小方块）
        label = str(i)
        tw = draw.textlength(label, font=font)
        th = font.size
        pad = 4
        ly0 = y0 + 2
        draw.rectangle(
            [x0 + 2, ly0, x0 + 2 + tw + pad * 2, ly0 + th + pad * 2],
            fill=color,
        )
        draw.text((x0 + 2 + pad, ly0 + pad), label, fill=(255, 255, 255), font=font)

        mapping[i] = el

    return marked, mapping


def build_som_mapping(
    img: Image.Image,
    max_elements: int = 100,
) -> Tuple[Image.Image, Dict[int, UIElement], List[UIElement]]:
    """完整流程：枚举元素 -> 画标记 -> 返回 (标记图, 映射, 元素列表)。"""
    elements = get_all_interactive_elements(max_elements)
    marked, mapping = draw_marks(img, elements)
    return marked, mapping, elements


def describe_marks(mapping: Dict[int, UIElement]) -> str:
    """生成编号 -> 元素描述的文本，供 prompt 使用。"""
    lines = []
    for num in sorted(mapping.keys()):
        el = mapping[num]
        label = el.label()
        if label:
            lines.append(f"#{num}: {label}")
    return "\n".join(lines)
