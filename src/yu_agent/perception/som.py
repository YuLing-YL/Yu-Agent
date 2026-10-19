"""Set-of-Mark（SOM）：在截图上绘制带编号的元素框，帮助模型精确指代。

模型看到带编号的截图后，输出 "点击 #5" 之类的指令，
程序通过映射表把编号翻译成精确的屏幕坐标，从而绕开 VLM 估算像素坐标的误差。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .a11y import UIElement, get_all_interactive_elements, label_language, set_label_language


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
    coord_scale: float = 1.0,
) -> Tuple[Image.Image, Dict[int, UIElement]]:
    """在图像上绘制编号框，返回 (标注后的图, 编号->元素映射)。

    编号从 1 开始（0 保留给"无对应元素"）。
    超出截图范围的元素会被过滤掉，保证编号连续无空洞。

    coord_scale: 元素坐标(逻辑像素) -> 本图像素 的比例。
                 图像未被缩放时为 1.0；被缩放为 f 倍时应传 f。
    """
    W, H = img.size
    # 先过滤掉完全在截图外的元素，保证编号连续
    visible = [
        el for el in elements
        if el.x * coord_scale < W and el.y * coord_scale < H
        and (el.x + el.w) * coord_scale > 0 and (el.y + el.h) * coord_scale > 0
    ]

    marked = img.copy()
    draw = ImageDraw.Draw(marked)
    font = _load_font(max(14, img.size[0] // 90))

    mapping: Dict[int, UIElement] = {}

    for i, el in enumerate(visible, start=1):
        # 裁剪到截图范围内
        x0 = max(0, el.x * coord_scale)
        y0 = max(0, el.y * coord_scale)
        x1 = min(W, (el.x + el.w) * coord_scale)
        y1 = min(H, (el.y + el.h) * coord_scale)

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
    max_elements: int = 120,
    coord_scale: float = 1.0,
    dock_max: int = 30,
    max_apps: int = 6,
    language: str = "",
) -> Tuple[Image.Image, Dict[int, UIElement], List[UIElement]]:
    """完整流程：枚举元素 -> 画标记 -> 返回 (标记图, 映射, 元素列表)。

    language: 编号表元素名的语言（"en" 时角色名用英文）。
    """
    if language:
        set_label_language(language)
    elements = get_all_interactive_elements(
        max_elements, dock_max=dock_max, max_apps=max_apps
    )
    marked, mapping = draw_marks(img, elements, coord_scale=coord_scale)
    return marked, mapping, elements


def describe_marks(mapping: Dict[int, UIElement], language: str = "") -> str:
    """生成编号 -> 元素描述的文本，供 prompt 使用。

    按「哪个应用」分组：屏幕上有多个窗口时，模型必须知道每个编号属于谁，
    否则很容易点错窗口。程序坞图标单独一组排在最后。
    language: "en" 时表头用英文。
    """
    if language:
        # 指定了语言就整表统一（表头 + 元素角色名），避免中英混排
        set_label_language(language)
    en = str(language or label_language()).strip().lower().startswith("en")
    dock_key = "dock" if en else "程序坞"
    if not mapping:
        return "(no markable elements found)" if en else "（当前没有识别到可标记的元素）"

    # 先按分组统计编号区间，让模型一眼看出结构
    groups: List[Tuple[str, List[int]]] = []
    index: Dict[str, int] = {}
    for num in sorted(mapping.keys()):
        el = mapping[num]
        key = dock_key if el.kind == "dock" else (el.app or ("app" if en else "应用"))
        if key not in index:
            index[key] = len(groups)
            groups.append((key, []))
        groups[index[key]][1].append(num)

    if en:
        lines = [f"{len(mapping)} numbered elements, grouped by app:"]
    else:
        lines = [f"共 {len(mapping)} 个编号元素，按应用分组："]
    for name, nums in groups:
        span = f"#{nums[0]}~#{nums[-1]}" if len(nums) > 1 else f"#{nums[0]}"
        if name == dock_key:
            lines.append(
                f"[dock] {span} (app icons at the bottom; click only to open/switch apps)"
                if en else f"【程序坞】{span}（屏幕底部图标，仅在需要打开/切换应用时用）"
            )
        else:
            lines.append(f"[{name}] {span} ({len(nums)})" if en else f"【{name}】{span}（{len(nums)} 个）")
    lines.append("")

    for num in sorted(mapping.keys()):
        el = mapping[num]
        label = el.label()
        empty = "(no title)" if en else "（无标题元素）"
        lines.append(f"#{num}: {label}" if label else f"#{num}: {empty}")
    return "\n".join(lines)
