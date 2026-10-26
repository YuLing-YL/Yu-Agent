"""Set-of-Mark（SOM）：在截图上绘制带编号的元素框，帮助模型精确指代。

模型看到带编号的截图后，输出 "点击 #5" 之类的指令，
程序通过映射表把编号翻译成精确的屏幕坐标，从而绕开 VLM 估算像素坐标的误差。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .a11y import UIElement, get_all_interactive_elements, label_language, set_label_language

# 框的颜色（给模型看的两条通道，颜色不同，含义不同）：
# 红 = AX 元素（有角色/名字，能覆盖图标按钮），蓝 = OCR 补的文字（AX 盲区兜底）。
# 蓝色是用户实测「位置准」的那套，别随便改；改了就同步改提示词里的说明。
_COLOR_APP = (220, 24, 24)
_COLOR_OCR = (24, 92, 220)

# 菜单栏高度（逻辑像素）：OCR 标注时这一段整条跳过，
# 否则「文件 编辑 显示 历史 书签」会占掉十几个编号，实际几乎用不到。
_MENUBAR_SKIP_H = 26.0

# 这些角色的 AX 矩形经常比「眼睛看到的东西」大很多（盒子含 padding、甚至整行宽），
# 用截图像素把它们收缩到真实内容上。按钮/输入框不在其中：它们的 AX 矩形本来就够准，
# 收缩反而会把点击区弄小。
_SHRINKABLE_ROLES = {
    "AXRow", "AXCell", "AXListItem", "AXOutlineRow", "AXColumn", "AXGrid",
    "AXStaticText", "AXImage", "AXGroup", "AXLink", "AXHeading",
    "AXList", "AXTable", "AXOutline", "AXWebArea", "AXTextArea",
}

# 两个编号标签之间至少留的空白（像素）
_LABEL_GAP = 2

# 允许因为「框里什么都没有」而被丢弃的角色。
# 只丢容器/装饰/文字：它们空着就是纯噪声（画出来是罩在空白上的大红框）。
# 按钮、输入框即使检测不到内容也必须保留：白底白界面的输入框是合法控件。
_DROPPABLE_EMPTY_ROLES = {
    "AXRow", "AXCell", "AXListItem", "AXOutlineRow", "AXColumn",
    "AXStaticText", "AXImage", "AXGroup",
}

# 按钮/图标类：只有「没有名字」且框里真的什么都没有时才丢。
# 这类元素在截图上看就是一块空白（工具栏占位、被隐藏的 tab 按钮），
# 模型既认不出是什么、点了也未必有反应，白占一个编号。
# 有名字的按钮一律保留——名字本身就是给模型的线索。
_UNNAMED_EMPTY_OK_ROLES = {"AXButton", "AXToolbarButton"}



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

        return [int(n) for n in nums if n is not None]

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
    实测微信输入栏那排小按钮，标签会互相压住，图上是「11(1 12」这种
    根本读不出来的东西 —— 模型只能瞎猜。

    ⚠️ 候选顺序刻意把「框外左侧」排第一：
    列表/网格每行只有 20 来个像素，把标签甩到框外（上一行/下一行）
    必然压住邻居的框；而框外左侧正好压在项目图标上，不挡名字
    （模型是靠名字认元素的）。
    """
    margin = 1
    cands = [
        # 框外左侧优先：列表里的「文本字段」框只盖住文件名本身，
        # 标签放左边正好压在项目图标上，不挡文件名（模型靠名字认元素）。
        (x0 - tw - margin, y0),
        (x0 + margin, y0 + margin),                       # 框内左上
        (x1 + margin, y0),                                # 框右外
        (x1 - tw - margin, y0 + margin),                  # 框内右上
        (x0 - tw - margin, y1 - th - margin),             # 框外左下
        (x1 + margin, y1 - th - margin),                  # 框外右下
        (x0 + margin, y1 - th - margin),                  # 框内左下
        (x0, y0 - th - margin),                           # 正上方
        (x0, y1 + margin),                                # 正下方
    ]

    def _score(cx: float, cy: float) -> float:
        """越界罚分 + 与已放标签的重叠面积。

        occupied 里存的是「禁入区」（比标签本身大 2px），所以两个标签起码隔开
        2px——否则一排密集按钮上的编号会连着写，图上就是「11|12|13」这种
        根本分不开的一串。
        """
        if cx < 0 or cy < 0 or cx + tw > W or cy + th > H:
            return float("inf")
        box = (cx - 1, cy - 1, cx + tw + 1, cy + th + 1)
        return sum(_overlap_area(box, o) for o in occupied)

    best = cands[0]
    best_score = float("inf")
    for cx, cy in cands:
        s = _score(cx, cy)
        if s <= 0:
            return cx, cy                       # 完全不重叠，直接用
        if s < best_score:
            best, best_score = (cx, cy), s

    # 所有常规位置都会压到别人的标签 → 沿框左边/_ 上下逐步外扩找空位
    for step in range(2, 90, 2):
        for cx, cy in (
            (x0 - tw - margin - step, y0),
            (x1 + margin + step, y0),
            (x0, y0 - th - margin - step),
            (x0, y1 + margin + step),
        ):
            if _score(cx, cy) <= 0:
                return cx, cy
    return best


def _overlap_area(
    a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]
) -> float:
    """两个矩形的交叠面积（不重叠为 0）。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h


def _overlaps(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def draw_marks(
    img: Image.Image,
    elements: List[UIElement],
    coord_scale: float = 1.0,
    stabilizer: Optional[MarkStabilizer] = None,
    content_check: bool = True,
    shrink: bool = True,
) -> Tuple[Image.Image, Dict[int, UIElement]]:
    """在图像上绘制编号框，返回 (标注后的图, 编号->元素映射)。

    编号优先取自 stabilizer（跨帧稳定），没有 stabilizer 时按顺序 1..N。
    超出截图范围的元素会被过滤掉。

    coord_scale: 元素坐标(逻辑像素) -> 本图像素 的比例。
                 图像未被缩放时为 1.0；被缩放为 f 倍时应传 f。
    content_check: 用截图像素反向校验元素矩形（见 perception/content.py）：
       - 框里什么都没有 → 丢掉（这类框画出来就是罩在空白处的大红框）
       - 框明显比内容大 → 收缩到内容上（列表行不再压住上下邻居）
    shrink: 是否做收缩（content_check=False 时无效）。
    """
    from . import a11y as _a11y
    from .content import content_info, shrink_box

    W, H = img.size
    # 先过滤掉完全在截图外的元素
    visible = [
        el for el in elements
        if el.x * coord_scale < W and el.y * coord_scale < H
        and (el.x + el.w) * coord_scale > 0 and (el.y + el.h) * coord_scale > 0
    ]
    if len(visible) < len(elements):
        # 画不出来 ≠ 没识别到：记进审计，解释「识别 36 个为什么图上只有 33 个」
        _a11y.bump_audit("dropped_offscreen", len(elements) - len(visible))
    numbers = stabilizer.assign(visible) if stabilizer else list(range(1, len(visible) + 1))

    # ── 阶段 1：先算好每个元素的最终矩形 ────────────────────────────
    # 必须在动笔之前全部算完：否则先画的框会污染后面元素的内容分析。
    plan: List[Tuple[int, UIElement, Tuple[float, float, float, float], Any]] = []
    analyzed = 0
    with_content = 0
    for i, el in enumerate(visible):
        num = numbers[i] if i < len(numbers) else i + 1
        x0 = max(0.0, el.x * coord_scale)
        y0 = max(0.0, el.y * coord_scale)
        x1 = min(float(W), (el.x + el.w) * coord_scale)
        y1 = min(float(H), (el.y + el.h) * coord_scale)
        if x1 <= x0 or y1 <= y0:
            continue

        info = content_info(img, (x0, y0, x1, y1)) if content_check else None
        if info is not None:
            analyzed += 1
            if info.has_content:
                with_content += 1

        plan.append((num, el, (x0, y0, x1, y1), info))

    # 帧可信度：元素框「落在有画面的地方」的比例。
    # 这一项低，基本只有一个原因——截屏和 AX 枚举不是同一时刻/同一桌面
    # （切了 Space、刚唤醒、截屏被系统卡住 30s 拿到旧帧）。
    # 这时画出来的框会整体错位，必须让上层知道，不能闷头让模型照着点。
    ratio = (with_content / analyzed) if analyzed else 1.0
    _a11y.last_stats["frame_content_ratio"] = round(ratio, 3)
    _a11y.last_stats["frame_reliable"] = bool(ratio >= 0.5)

    marked = img.copy()
    draw = ImageDraw.Draw(marked)
    # 标签字号固定：跟着分辨率走，但**不**跟着元素高度缩，
    # 否则列表行里的编号会小到模型认不出来，或者大到自己压住邻居。
    label_size = max(11, min(14, int(img.size[0] / 120)))
    font = _load_font(label_size)

    mapping: Dict[int, UIElement] = {}
    occupied: List[Tuple[float, float, float, float]] = []

    for num, el, rect, info in plan:
        x0, y0, x1, y1 = rect
        color = _COLOR_OCR if el.kind == "ocr" else _COLOR_APP

        # 框里没有任何画面内容的「容器/文字」元素：画出来只会误导模型
        # （这正是「框罩在空白处、一个叠一个」的主要来源）。
        droppable = el.role in _DROPPABLE_EMPTY_ROLES or (
            not (el.title or "").strip() and el.role in _UNNAMED_EMPTY_OK_ROLES
        )
        if (
            info is not None
            and not info.has_content
            and droppable
            and (x1 - x0) >= 16
            and (y1 - y0) >= 12
        ):
            _a11y.bump_audit("dropped_empty")
            continue

        if shrink and info is not None and el.role in _SHRINKABLE_ROLES:
            # 对「容器/文字」角色不设收缩上限：AX 给列表行的盒子常常是内容的
            # 4~5 倍高，只有收到位，框才不会一个叠一个地压住邻居。
            nx0, ny0, nx1, ny1 = shrink_box(rect, info, max_shrink=1.0)
            if (nx0, ny0, nx1, ny1) != rect:
                _a11y.bump_audit("shrunk")
                x0, y0, x1, y1 = nx0, ny0, nx1, ny1
                # 点击点也跟着落到内容中心，否则「框对了但点在外面」
                el = _element_with_rect(el, x0 / coord_scale, y0 / coord_scale,
                                        (x1 - x0) / coord_scale, (y1 - y0) / coord_scale)

        # 边框：1px 细框，别把元素本身盖住（2px 在密集列表里会糊成一片）
        draw.rectangle([x0, y0, x1, y1], outline=color, width=1)

        label = str(num)
        tw = draw.textlength(label, font=font)
        th = label_size
        lx, ly = _label_anchor(x0, y0, x1, y1, tw, th, W, H, occupied)
        box = (lx - 1, ly - 1, lx + tw + 1, ly + th + 1)
        # 白描边 + 深红底：标签即便挨在一起，模型也能分清是「两个数字」而不是一个
        draw.rectangle([box[0], box[1], box[2], box[3]], fill=color, outline=(255, 255, 255))
        draw.text((lx, ly - 1), label, fill=(255, 255, 255), font=font)
        occupied.append((box[0] - _LABEL_GAP, box[1] - _LABEL_GAP,
                         box[2] + _LABEL_GAP, box[3] + _LABEL_GAP))

        mapping[num] = el

    # 审计快照要在这里刷新：枚举阶段（a11y）存的那份是画框**之前**的，
    # 不含「空框丢弃 / 屏幕外」这些画框时才发生的数量，用户会看到
    # 「保留 36 个，图上只有 33 个，差 3 个没人解释」。
    _a11y.last_stats["audit"] = _a11y.audit()

    return marked, mapping


def _element_with_rect(el: UIElement, x: float, y: float, w: float, h: float) -> UIElement:
    """复制一个元素并替换矩形（收缩后用于画框 + 点击中心）。

    不复用原对象：元素列表还可能被上层用于其它用途（审计、稳定编号），
    改了原对象会让「同一元素」在不同阶段坐标不一致。
    """
    return UIElement(
        id=el.id, role=el.role, title=el.title, description=el.description,
        x=x, y=y, w=w, h=h, app=el.app, kind=el.kind,
        enabled=el.enabled, in_list=el.in_list,
    )


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
    content_check: bool = True,
    shrink: bool = True,
    use_ocr: bool = False,
    ocr_max: int = 30,
    ocr_min_conf: float = 0.30,
    elements: Optional[List[UIElement]] = None,
) -> Tuple[Image.Image, Dict[int, UIElement], List[UIElement]]:
    """完整流程：枚举元素 -> 画标记 -> 返回 (标记图, 映射, 元素列表)。

    language: 编号表元素名的语言（"en" 时角色名用英文）。
    stabilizer: 传入同一个实例即可让编号跨帧稳定。
    skip_menubar: 是否跳过菜单栏项（绝大多数任务用不到，且极占编号）。
    draw: False 时只枚举 + 分配编号，不往图上画（用于「纯坐标模式」的坐标吸附）。
    content_check / shrink: 用截图像素校验并收缩元素框（见 draw_marks）。
    use_ocr: 用 OCR 补标注（蓝色框）。AX 读不到的界面——Electron/网页/自绘/
             没给辅助功能权限的应用——只有这条路能画出框。约 0.3s/帧。
    ocr_max / ocr_min_conf: OCR 补标注的数量上限与置信度下限。
    elements: **外部注入的目标列表**（网页三模态融合的结果，见 perception/web/）。
              给了就不再枚举原生 AX —— 网页里 DOM/AX/OCR 已经在 `fusion` 里合成过了，
              再叠一层原生 AX 只会把浏览器自己的工具栏混进来。
    """
    if language:
        set_label_language(language)
    if elements is None:
        elements = get_all_interactive_elements(
            max_elements, dock_max=dock_max, max_apps=max_apps, skip_menubar=skip_menubar
        )
        if use_ocr:
            elements = _merge_ocr_elements(
                img, elements, coord_scale=coord_scale,
                skip_menubar=skip_menubar, ocr_max=ocr_max, ocr_min_conf=ocr_min_conf,
                max_apps=max_apps,
            )
    else:
        elements = list(elements)[: max_elements or len(elements)]
    if not draw:
        visible = [
            el for el in elements
            if el.x * coord_scale < img.size[0] and el.y * coord_scale < img.size[1]
        ]
        numbers = stabilizer.assign(visible) if stabilizer else list(range(1, len(visible) + 1))
        return img, dict(zip(numbers, visible)), elements
    marked, mapping = draw_marks(
        img, elements, coord_scale=coord_scale, stabilizer=stabilizer,
        content_check=content_check, shrink=shrink,
    )
    return marked, mapping, elements


def _merge_ocr_elements(
    img: Image.Image,
    elements: List[UIElement],
    *,
    coord_scale: float = 1.0,
    skip_menubar: bool = True,
    ocr_max: int = 30,
    ocr_min_conf: float = 0.30,
    max_apps: int = 1,
) -> List[UIElement]:
    """把 OCR 文字补进元素列表（AX 元素在前，OCR 元素在后）。

    ⚠️ 必须在**未画框的干净截图**上跑 OCR：画上去的红色编号会被识别成文字，
    变成「#7」「#12」混进编号表，模型会照着这些假编号点。

    三条规则（都在 ocr_marks 里实现，这里只负责串联）：
    1. AX 元素没名字 → 用框里的 OCR 文字补名字（工具栏纯图标按钮）；
    2. 已经有 AX 框盖住的文字 → 不重复编号；
    3. 剩下的才是 AX 盲区，用蓝色框补上。
    """
    from . import ocr_marks

    boxes = ocr_marks.recognize_boxes(img, min_conf=ocr_min_conf)
    if not boxes:
        return elements
    elements = ocr_marks.fill_titles(elements, boxes, coord_scale=coord_scale)
    extra = ocr_marks.elements_from_boxes(
        boxes,
        coord_scale=coord_scale,
        max_boxes=ocr_max,
        skip_top_px=(_MENUBAR_SKIP_H * coord_scale) if skip_menubar else 0.0,
        allow_rects=_target_window_rects(max_apps),
    )
    extra = ocr_marks.drop_covered(extra, elements)
    return elements + extra


def _target_window_rects(max_apps: int) -> List[Tuple[float, float, float, float]]:
    """OCR 允许出现在哪些窗口里（逻辑像素）。

    `som_frontmost_only` 的语义是「只标最前面的应用」，OCR 也必须守同一条规矩：
    否则背景窗口的标题、聊天记录、网页正文全被编号，模型会跑去点那些点不到的地方。
    拿不到窗口列表时返回空列表 = 不做限制（宁可多标，也别把界面全禁了）。
    """
    from . import a11y as _a11y

    try:
        windows = _a11y._on_screen_windows()
        if not windows:
            return []
        order = _a11y._ordered_app_pids(windows, max_apps=max(1, max_apps))
        keep = set(order[:1]) if max_apps <= 1 else set(order)
        return [_a11y._rect_of(w) for w in windows if w["pid"] in keep]
    except Exception:
        return []


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
    ocr_key = "screen text (OCR)" if en else "屏幕文字(OCR)"
    if not mapping:
        return "(no markable elements found)" if en else "（当前没有识别到可标记的元素）"

    # 先按分组统计编号区间，让模型一眼看出结构
    groups: List[Tuple[str, List[int]]] = []
    index: Dict[str, int] = {}
    for num in sorted(mapping.keys()):
        el = mapping[num]
        if el.kind == "dock":
            key = dock_key
        elif el.kind == "ocr":
            # OCR 单独一组：这些框只有文字、没有控件语义，模型要知道来路
            key = ocr_key
        else:
            key = el.app or ("app" if en else "应用")
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
        elif name == ocr_key:
            lines.append(
                f"[{name}] {len(nums)}: {nums_txt} (blue boxes; text found by OCR on "
                f"the pixels — use them when a control has no red box)"
                if en else f"【{name}】{len(nums)} 个：{nums_txt}"
                          f"（蓝色框，由 OCR 从像素里识别出来的文字；红框没有覆盖的控件用它们点）"
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
