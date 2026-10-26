"""视觉校验：用**截图像素 + OCR** 去校验 DOM/AX 说的那件事到底成不成立。

三模态里视觉是"最终真值"，但不是"看到有框就信"——视觉要回答的是四个具体问题：

1. **这个矩形里真的有东西吗**（空白 → DOM 报了但页面没渲染出来，或被同色盖住）；
2. **字对不对得上**（OCR 出的文字 vs AX 名 / DOM 文本；`aria-label` 常年和可见文字不一致）；
3. **是不是被弹窗/浮层挡住了**（DOM 的 z 序优先，DOM 拿不到时退回"空白但有名字"的推断）；
4. **是不是置灰禁用**（低对比 + 低饱和；只作为佐证，不单独定罪）。

## 坐标换算（最容易错的一步）

DOM 给的是 **viewport CSS px**；截图是**物理/逻辑像素**。中间隔着三层：
`内容区原点`（浏览器窗口的页面区域在屏幕上的位置）→ `页面缩放 zoom` → `截图缩放 image_scale`。
少乘一层就"稳定偏 N 像素"，看起来像模型不准，其实是换算错了。

## 为什么不用 OCR 硬做 grounding
OCR 只认字：图标按钮、空白区域的 checkbox 它看不见；而且字号小于 ~10px 时识别率断崖下跌。
所以 OCR 在这里是**校验者**，不是唯一真相 —— 这也是禁止"只靠 OCR"的原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageStat

from ..content import content_info
from .align import Aligned, similarity

# 文字匹配阈值：低于它认为"对不上"（注意这不是"错了"，只是需要人工/反思看一眼）
TEXT_MATCH_MIN = 0.55
# 判定"这片区域基本没内容"的对比度阈值（灰度标准差）
FLAT_STD = 9.0
# 置灰观感：饱和度和对比同时偏低
GRAY_SAT = 22.0
GRAY_STD = 26.0


@dataclass
class VisualVerdict:
    """视觉校验结论。

    `text_match` 和 `dom_match` 必须分开：它们回答的是两个不同的问题。
    - text_match：画面上的字 与 **AX 名**（aria/无障碍名称）是否一致 —— 这是"无障碍标注可信吗"；
    - dom_match：画面上的字 与 **DOM 里的文字**是否一致 —— 这是"渲染出来的和 DOM 说的一样吗"。
    把两者混成一个分数，就分不清"AX 名写错了"还是"页面渲染错了"，冲突归因会全乱。
    """

    ok: bool = True
    text_match: Optional[float] = None   # 对 AX 名；None = 没有 AX 名可比
    dom_match: Optional[float] = None    # 对 DOM 文字；None = DOM 本来就没文字
    ocr_text: str = ""
    occluded: bool = False
    blank: bool = False
    disabled_look: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def vision_confirmed(self) -> bool:
        """视觉是否确认了某个名字（任一路匹配上即可）。"""
        return any(
            v is not None and v >= TEXT_MATCH_MIN for v in (self.text_match, self.dom_match)
        )

    def __str__(self) -> str:  # 便于日志
        def _f(v: Optional[float]) -> str:
            return "-" if v is None else f"{v:.2f}"
        bits = [f"ax={_f(self.text_match)}", f"dom={_f(self.dom_match)}"]
        for name in ("occluded", "blank", "disabled_look"):
            if getattr(self, name):
                bits.append(name)
        return "Visual(" + " ".join(bits) + ")"


def css_rect_to_image(
    rect: Tuple[float, float, float, float],
    *,
    origin: Tuple[float, float] = (0.0, 0.0),
    zoom: float = 1.0,
    image_scale: float = 1.0,
) -> Tuple[float, float, float, float]:
    """CSS viewport 矩形 → 截图图像像素矩形。

    rect 是 (x, y, w, h)；origin 是页面内容区左上角在屏幕上的**逻辑像素**坐标。
    """
    x, y, w, h = rect
    ox, oy = origin
    z = zoom or 1.0
    s = image_scale or 1.0
    return (
        (ox + x * z) * s,
        (oy + y * z) * s,
        (ox + (x + w) * z) * s,
        (oy + (y + h) * z) * s,
    )


def _overlap(a: Sequence[float], b: Sequence[float]) -> float:
    """交叠面积 / 较小矩形面积（文字框通常比容器小，用并集会一直不达标）。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    aa = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    ab = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(aa, ab)


def _ocr_in(rect: Tuple[float, float, float, float], boxes: Iterable) -> str:
    """收集落在矩形里的 OCR 文字（按 y 排序拼成一行）。"""
    hits = []
    for b in boxes:
        br = (getattr(b, "x", 0), getattr(b, "y", 0),
              getattr(b, "x", 0) + getattr(b, "w", 0), getattr(b, "y", 0) + getattr(b, "h", 0))
        if _overlap(rect, br) >= 0.25:
            hits.append((getattr(b, "y", 0), getattr(b, "x", 0), str(getattr(b, "text", ""))))
    hits.sort()
    return " ".join(t for _, _, t in hits if t).strip()


def _looks_disabled(img: Image.Image, rect: Tuple[float, float, float, float]) -> bool:
    """置灰观感：**几乎没对比度 + 几乎没颜色**才算。

    早期阈值（std<26 且 sat<22）会把白底细边框的输入框全判成禁用 —— 实测误报率极高。
    现在收紧到"整块接近纯灰一片"（std<10 且 sat<14），只用来给 DOM 的 disabled
    做**佐证/反证**，不单独下结论：
    - DOM 说禁用、像素却清晰可读 → 报冲突（aria-disabled 与样式脱节）；
    - 反过来"像素像禁用"不作为证据（深浅主题、彩色按钮都会骗过像素判断）。
    """
    try:
        crop = img.crop((int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])))
        if crop.width < 3 or crop.height < 3:
            return False
        gray = crop.convert("L")
        std = float(ImageStat.Stat(gray).stddev[0])
        sat = float(ImageStat.Stat(crop.convert("HSV")).mean[1])
    except Exception:
        return False
    return std < 10.0 and sat < 14.0


def _page_bg(img: Image.Image) -> Tuple[float, float, float]:
    """页面背景色的粗估：取四角 8×8 的均值。

    为什么不用"元素外圈"：实心按钮的外圈经常也是同一个颜色（按钮自己就是那个色块），
    拿外圈当背景会把「蓝底按钮」判成"和背景一样所以是空白"。
    """
    w, h, k = img.width, img.height, 8
    patches = [(0, 0), (w - k, 0), (0, h - k), (w - k, h - k)]
    vals = []
    for x, y in patches:
        try:
            vals.append(ImageStat.Stat(img.crop((x, y, x + k, y + k)).convert("RGB")).mean)
        except Exception:
            pass
    if not vals:
        return (255.0, 255.0, 255.0)
    return tuple(sum(v[i] for v in vals) / len(vals) for i in range(3))  # type: ignore[return-value]


def _region_is_blank(
    img: Image.Image, rect: Tuple[float, float, float, float], bg: Optional[Sequence[float]] = None
) -> bool:
    """这块区域是不是"什么都看不见"。

    不能只看"框里有没有强边缘"：**纯色实心按钮本来就没有内部边缘**，
    光看边缘会把好好的按钮判成空白（真实页面里大量按钮就是色块+白字，
    截图分辨率一低，白字的边缘也会掉到阈值以下）。

    三条任一成立就算"看得见"：
    1. 框里有笔画/图标级别的边缘（content_info）；
    2. 框内颜色与**页面背景**差异明显（实心色块也是看得见的东西）；
    3. 框内有明显的颜色起伏（std ≥ 6，比如渐变、图片）。
    """
    info = content_info(img, rect)
    if info is not None and info.has_content:
        return False

    try:
        x0, y0, x1, y1 = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        inner = img.crop((x0, y0, x1, y1)).convert("RGB")
        if inner.width < 2 or inner.height < 2:
            return True
        st = ImageStat.Stat(inner)
        mean = st.mean
        std = st.stddev
    except Exception:
        return False

    bg = bg or _page_bg(img)
    color_dist = sum(abs(a - b) for a, b in zip(mean, bg))
    if color_dist >= 12:
        return False
    if max(std) >= 6:
        return False
    return True


def check(
    target: Aligned,
    img: Optional[Image.Image],
    *,
    origin: Tuple[float, float] = (0.0, 0.0),
    zoom: float = 1.0,
    image_scale: float = 1.0,
    ocr_boxes: Iterable = (),
    covering: Sequence[Tuple[float, float, float, float, int]] = (),
) -> VisualVerdict:
    """对一个已对齐的目标做视觉校验。

    covering: 页面上"盖在它上面"的矩形 (x, y, w, h, z)（CSS px）。
              来自 DOM 的 z 序（`fusion.covering_rects()` 计算），用来判遮挡。
    """
    v = VisualVerdict()
    if img is None:
        v.notes.append("no_screenshot")
        return v

    rect = css_rect_to_image(target.dom.rect, origin=origin, zoom=zoom, image_scale=image_scale)
    if rect[2] - rect[0] < 2 or rect[3] - rect[1] < 2:
        v.ok = False
        v.blank = True
        v.notes.append("zero_size")
        return v
    if rect[0] < 0 or rect[1] < 0 or rect[2] > img.width or rect[3] > img.height:
        v.notes.append("out_of_frame")
        # 出框不代表错，可能是窗口被部分遮挡/截图只截了一块；继续校验能校验的部分

    # ── 1. 有没有内容 ────────────────────────────────────────────
    v.blank = _region_is_blank(img, rect)
    # ── 2. 文字对不对 ────────────────────────────────────────────
    text = _ocr_in(rect, ocr_boxes)
    v.ocr_text = text
    ax_names = [target.name] if (target.ax is not None and target.name) else []
    dom_names = [n for n in (target.dom.label, target.dom.text) if n]
    if text and ax_names:
        v.text_match = max(similarity(text, n) for n in ax_names)
        if v.text_match < TEXT_MATCH_MIN:
            v.notes.append("ax_name_mismatch")
    if text and dom_names:
        v.dom_match = max(similarity(text, n) for n in dom_names)
        if v.dom_match < TEXT_MATCH_MIN:
            v.notes.append("dom_text_mismatch")
    if not text and ax_names and not v.blank and target.role in ("statictext", "link", "button"):
        # 有名字但区域里没有文字：图标按钮很正常；但如果 AX 说是纯文本/链接就可疑
        v.notes.append("text_missing")

    # ── 3. 遮挡 ──────────────────────────────────────────────────
    # 两个必须的细节：
    # ① 用 **严格大于**。祖先容器（body/列表容器）几乎都是 z-index:0，
    #    而目标也是 0；用 >= 会把每个元素都判成"被祖先盖住"（实测全灭）。
    # ② 跳过和自己几乎重合的矩形，否则"自己盖自己"。
    for cx, cy, cw, ch, cz in covering:
        if cz <= target.dom.z:
            continue
        cover_rect = css_rect_to_image((cx, cy, cw, ch), origin=origin, zoom=zoom,
                                       image_scale=image_scale)
        if abs(cover_rect[0] - rect[0]) < 2 and abs(cover_rect[1] - rect[1]) < 2 \
                and abs(cover_rect[2] - rect[2]) < 2 and abs(cover_rect[3] - rect[3]) < 2:
            continue
        if _overlap(rect, cover_rect) >= 0.6:
            v.occluded = True
            v.notes.append(f"covered_by_z{cz}")
            break
    if not v.occluded and v.blank and not v.vision_confirmed and ax_names:
        # DOM 有名字、画面上却是空白、也没找到文字 → 极可能被不透明浮层盖住
        v.occluded = True
        v.notes.append("blank_but_named")

    # ── 4. 置灰 ──────────────────────────────────────────────────
    v.disabled_look = _looks_disabled(img, rect)

    # ── 汇总 ────────────────────────────────────────────────────
    if v.blank and not v.vision_confirmed:
        v.ok = False
    if v.occluded:
        v.ok = False
    return v
