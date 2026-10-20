"""Set-of-Mark（SOM）：在截图上绘制带编号的元素框，帮助模型精确指代。

模型看到带编号的截图后，输出 "点击 #5" 之类的指令，
程序通过映射表把编号翻译成精确的屏幕坐标，从而绕开 VLM 估算像素坐标的误差。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

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


class MarkStabilizer:
    """让同一个元素在多帧之间保持同一个编号。

    为什么需要：原来的编号是「本帧按坐标排序后从 1 开始重排」，
    只要元素有增减或位置微移，整张表的编号就会整体漂移。
    实测踩过：模型第 1 步点 #38（iCloud云盘），第 2 步它想点 yuling
    仍然输出 #38，而这一帧的 #38 已经变成别的元素 → 点错。

    编号稳定后，模型记住的编号始终指同一个元素，跨帧指代才可靠；
    编号表的语义校验（见 agent/loop.py 的 _verify_mark_semantics）也才成立。

    编号**不再保证连续**（元素消失会留下空号），这是刻意的：
    稳定 > 好看。空号太多时才整体重排。
    """

    def __init__(self, ttl_frames: int = 10, drift_px: float = 160.0,
                 max_number: int = 999):
        self.ttl_frames = ttl_frames
        self.drift_px = drift_px
        self.max_number = max_number
        self._map: Dict[Tuple[Any, ...], int] = {}   # key -> 编号
        self._age: Dict[Tuple[Any, ...], int] = {}   # key -> 最后出现的帧号
        self._next = 1
        self._frame = 0

    # -- key ---------------------------------------------------------- #
    @staticmethod
    def _key(el: UIElement) -> Tuple[Any, ...]:
        """元素身份：应用 + 角色 + 标题 + 16px 量化的中心点。

        位置做量化是为了容忍渲染抖动（±几像素很常见），
        又不至于把「相邻两行」认成同一个。
        """
        return (
            el.app or "",
            el.role or "",
            (el.title or "")[:40],
            int(round(el.cx / 16)),
            int(round(el.cy / 16)),
        )

    def reset(self) -> None:
        self._map.clear()
        self._age.clear()
        self._next = 1
        self._frame = 0

    # -- 主逻辑 ------------------------------------------------------- #
    def assign(self, elements: List[UIElement]) -> List[int]:
        """给本帧的元素分配编号（尽量复用上一帧的），返回等长编号列表。"""
        self._frame += 1
        nums: List[Optional[int]] = [None] * len(elements)
        used: set = set()

        # 1) 精确身份命中：位置没怎么变 → 沿用原编号
        for i, el in enumerate(elements):
            k = self._key(el)
            num = self._map.get(k)
            if num is not None and num not in used:
                nums[i] = num
                used.add(num)
                self._age[k] = self._frame

        # 2) 模糊命中：同应用同角色同名字，但位置漂了（滚动、列表刷新）
        for i, el in enumerate(elements):
            if nums[i] is not None:
                continue
            best: Optional[Tuple[float, int, Tuple[Any, ...]]] = None
            for k, num in self._map.items():
                if num in used:
                    continue
                if k[0] != (el.app or "") or k[1] != (el.role or ""):
                    continue
                if k[2] != (el.title or "")[:40]:
                    continue
                d = ((k[3] * 16 - el.cx) ** 2 + (k[4] * 16 - el.cy) ** 2) ** 0.5
                if d <= self.drift_px and (best is None or d < best[0]):
                    best = (d, num, k)
            if best is not None:
                _, num, _old = best
                nums[i] = num
                used.add(num)
                k = self._key(el)
                self._map[k] = num
                self._age[k] = self._frame

        # 3) 新元素：发新号
        for i, el in enumerate(elements):
            if nums[i] is not None:
                continue
            while self._next in used:
                self._next += 1
            nums[i] = self._next
            used.add(self._next)
            self._next += 1
            k = self._key(el)
            self._map[k] = nums[i]  # type: ignore[assignment]
            self._age[k] = self._frame

        # 4) 清掉长时间没出现的元素身份（释放内存，编号不回收）
        if len(self._map) > 2000 or self._next > self.max_number:
            self.reset()
            return list(range(1, len(elements) + 1))

        stale = [
            k for k, age in self._age.items()
            if self._frame - age > self.ttl_frames
        ]
        for k in stale:
            self._map.pop(k, None)
            self._age.pop(k, None)

        return [int(n) for n in nums]

    def known_number(self, el: UIElement) -> Optional[int]:
        """这个元素上次的编号（没出现过则为 None）。"""
        return self._map.get(self._key(el))


def _label_anchor(
    x0: float, y0: float, x1: float, y1: float,
    tw: float, th: float, W: float, H: float,
    occupied: List[Tuple[float, float, float, float]],
) -> Tuple[float, float]:
    """给编号标签找一个不和别人重叠的位置。

    编号表挤成一团是「模型读错编号」的主因（尤其窄侧边栏、工具栏）。

    ⚠️ 候选顺序刻意把「框内」排在前面：
    列表/网格每行只有 20 来个像素，把标签甩到框外（上一行/下一行）
    必然压住邻居的框，比框内稍微盖住一点文字更糟。
    """
    margin = 1
    cands = [
        # 框外左侧优先：列表里的「文本字段」框只盖住文件名本身，
        # 标签放左边正好压在项目图标上，不挡文件名（模型靠名字认元素）。
        (x0 - tw - margin, y0),
        (x0 + margin, y0 + margin),                       # 框内左上
        (x1 + margin, y0),                                # 框右外
        (x1 - tw - margin, y0 + margin),                  # 框内右上
        (x0, y0 - th - margin),                           # 正上方
        (x0, y1 + margin),                                # 正下方
    ]
    best = cands[0]
    for cx, cy in cands:
        if cx < 0 or cy < 0 or cx + tw > W or cy + th > H:
            continue
        box = (cx - 1, cy - 1, cx + tw + 1, cy + th + 1)
        if not any(_overlaps(box, o) for o in occupied):
            return cx, cy
        best = (cx, cy)
    return best


def _overlaps(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def draw_marks(
    img: Image.Image,
    elements: List[UIElement],
    coord_scale: float = 1.0,
    stabilizer: Optional[MarkStabilizer] = None,
) -> Tuple[Image.Image, Dict[int, UIElement]]:
    """在图像上绘制编号框，返回 (标注后的图, 编号->元素映射)。

    编号优先取自 stabilizer（跨帧稳定），没有 stabilizer 时按顺序 1..N。
    超出截图范围的元素会被过滤掉。

    coord_scale: 元素坐标(逻辑像素) -> 本图像素 的比例。
                 图像未被缩放时为 1.0；被缩放为 f 倍时应传 f。
    """
    W, H = img.size
    # 先过滤掉完全在截图外的元素
    visible = [
        el for el in elements
        if el.x * coord_scale < W and el.y * coord_scale < H
        and (el.x + el.w) * coord_scale > 0 and (el.y + el.h) * coord_scale > 0
    ]
    numbers = stabilizer.assign(visible) if stabilizer else list(range(1, len(visible) + 1))

    marked = img.copy()
    draw = ImageDraw.Draw(marked)
    # 字号跟着分辨率走；但每个元素再按自己的高度收一次（见下），
    # 否则列表/网格每行只有 ~20px，标签会溢出到邻行把别人的框压住。
    base_size = max(16, img.size[0] // 70)
    font_cache = {}

    def _font(size: int):
        size = max(10, int(size))
        if size not in font_cache:
            font_cache[size] = _load_font(size)
        return font_cache[size]

    mapping: Dict[int, UIElement] = {}
    occupied: List[Tuple[float, float, float, float]] = []

    for i, el in enumerate(visible):
        num = numbers[i] if i < len(numbers) else i + 1
        # 裁剪到截图范围内
        x0 = max(0.0, el.x * coord_scale)
        y0 = max(0.0, el.y * coord_scale)
        x1 = min(float(W), (el.x + el.w) * coord_scale)
        y1 = min(float(H), (el.y + el.h) * coord_scale)
        if x1 <= x0 or y1 <= y0:
            continue

        # 边框：细一点，别把元素本身盖住
        color = (230, 30, 30)
        draw.rectangle([x0, y0, x1, y1], outline=color, width=2)

        # 编号标签：字号不超过元素高度的 0.7 倍 → 标签一定能塞在自己这一行里
        h_px = y1 - y0
        font = _font(min(base_size, max(10, int(h_px * 0.7))))
        label = str(num)
        tw = draw.textlength(label, font=font)
        th = font.size
        lx, ly = _label_anchor(x0, y0, x1, y1, tw, th, W, H, occupied)
        box = (lx - 1, ly - 1, lx + tw + 1, ly + th + 1)
        draw.rectangle([box[0], box[1], box[2], box[3]], fill=color)
        draw.text((lx, ly - 1), label, fill=(255, 255, 255), font=font)
        occupied.append(box)

        mapping[num] = el

    return marked, mapping


def build_som_mapping(
    img: Image.Image,
    max_elements: int = 60,
    coord_scale: float = 1.0,
    dock_max: int = 20,
    max_apps: int = 1,
    language: str = "",
    stabilizer: Optional[MarkStabilizer] = None,
    skip_menubar: bool = True,
    draw: bool = True,
) -> Tuple[Image.Image, Dict[int, UIElement], List[UIElement]]:
    """完整流程：枚举元素 -> 画标记 -> 返回 (标记图, 映射, 元素列表)。

    language: 编号表元素名的语言（"en" 时角色名用英文）。
    stabilizer: 传入同一个实例即可让编号跨帧稳定。
    skip_menubar: 是否跳过菜单栏项（绝大多数任务用不到，且极占编号）。
    draw: False 时只枚举 + 分配编号，不往图上画（用于「纯坐标模式」的坐标吸附）。
    """
    if language:
        set_label_language(language)
    elements = get_all_interactive_elements(
        max_elements, dock_max=dock_max, max_apps=max_apps, skip_menubar=skip_menubar
    )
    if not draw:
        visible = [
            el for el in elements
            if el.x * coord_scale < img.size[0] and el.y * coord_scale < img.size[1]
        ]
        numbers = stabilizer.assign(visible) if stabilizer else list(range(1, len(visible) + 1))
        return img, {n: el for n, el in zip(numbers, visible)}, elements
    marked, mapping = draw_marks(img, elements, coord_scale=coord_scale, stabilizer=stabilizer)
    return marked, mapping, elements


def _short_label(text: str, limit: int = 48) -> str:
    """把元素描述压成一行短文本。

    网页/文档标题经常是整段文本（带换行），原样塞进编号表会：
    把一行的 token 顶到几十个、换行还会打乱「一行一个编号」的排版，
    模型很容易串行读错。所以统一去换行 + 截断。
    """
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


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
        # 编号跨帧稳定后不再连续，用数量表述（区间会骗人）
        nums_txt = ", ".join(f"#{n}" for n in nums[:6]) + ("..." if len(nums) > 6 else "")
        if name == dock_key:
            lines.append(
                f"[dock] {len(nums)} icons: {nums_txt} "
                f"(click only to open/switch apps)"
                if en else f"【程序坞】{len(nums)} 个图标：{nums_txt}"
                          f"（仅在需要打开/切换应用时用）"
            )
        else:
            lines.append(
                f"[{name}] {len(nums)}: {nums_txt}" if en
                else f"【{name}】{len(nums)} 个：{nums_txt}"
            )
    lines.append("")

    for num in sorted(mapping.keys()):
        el = mapping[num]
        label = _short_label(el.label())
        empty = "(no title)" if en else "（无标题元素）"
        lines.append(f"#{num}: {label}" if label else f"#{num}: {empty}")
    return "\n".join(lines)
