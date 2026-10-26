"""OCR 补标注：AX 读不到的地方，用屏幕上的文字补编号（蓝色框）。

为什么需要（实测出来的「标注不全」根因）：
macOS 的 AX 树只包含「声明过自己是什么」的元素。下面这些界面里，AX 要么整片是空的，
要么只给一个 AXWebArea 空壳：
- Electron / Chromium 系（VS Code、Slack、Discord、飞书、微信小程序窗口、Chrome 网页区）；
- 自绘界面（Canvas、游戏、部分 Java / Qt / Flutter 应用）；
- 没有授予「辅助功能」权限的应用（只暴露极少数元素）。
这时红框几乎一个都画不出来，模型等于瞎了。而 OCR 读的是**画出来的像素**，
不管对方用什么框架实现，只要字显示在屏幕上就能定位——实测蓝色 OCR 框比 AX 框更全。

分工（刻意如此，不要合并成一条路）：
- AX 框（红）：带角色和名字，能覆盖图标按钮、输入框这些**没有文字**的控件；
- OCR 框（蓝）：只覆盖文字，但不受框架限制，AX 盲区全靠它。
两者重叠时保留 AX 框（信息更多），所以 `drop_covered()` 会把被 AX 框盖住的 OCR 文字丢掉。

坐标约定：本模块内部一律用**截图图像像素**（OCR 的输出），
对外的 UIElement 用**逻辑像素**（点击坐标系），转换靠 coord_scale（图像像素/逻辑像素）。
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image

from . import ocr
from .a11y import UIElement
from .vision_ocr import TextBox

# 角色/应用名（写进编号表，让模型知道这一条是 OCR 补出来的）
OCR_ROLE = "OCR文字"
OCR_APP = "屏幕文字"

# 过滤阈值（都是「实测调出来」的经验值，改之前先看后面的注释）
MIN_CONF = 0.30        # Vision 置信度下限。实测真实界面文字常在 0.3~0.95，
                       # 卡到 0.35 会把灰字/小字（占比不小）整片丢掉，反而更不全

MIN_BOX_W = 6.0        # 图像像素：太窄的点不准
MIN_BOX_H = 7.0        # 图像像素：太矮的点不准
MAX_TEXT_CHARS = 28    # 超过这个长度基本是正文段落，不是一个可点的标签。
                       # 实测 48 会把 PDF/文档正文的每一行都框上（图上一片蓝），
                       # 界面上真正的按钮/菜单/字段名很少超过 20 个字。
MAX_BOXES = 30         # 单帧最多补多少个（再多会淹没 AX 的编号）
COVER_RATIO = 0.6      # OCR 框被已有元素覆盖到这个比例，就算重复

# 只有符号/数字编号的识别结果（"—"、"·"、"::::"）点了也没意义；
# 但 3 位以上的数字要保留（"2024" 可能是年份，"100%" 之类）。
_MEANINGLESS = re.compile(r"^[\W_]+$", re.UNICODE)
_DIGITS = re.compile(r"^\d+$")


def _keep_text(text: str) -> bool:
    """这段文字值不值得占一个编号。

    实践中最常见的噪声（都在真实截图里抓到过）：
    - 纯符号 "—" "·"；
    - 角标/时间戳之类的 1~2 位数字（"12"、"37"）——它们不是可点目标，
      却会实打实吃掉 1/3 的 OCR 预算；
    - 单个字符（图标里的字母、分隔符）。
    """
    if _MEANINGLESS.match(text):
        return False
    if len(text) == 1:
        return False
    if _DIGITS.match(text) and len(text) <= 2:
        return False
    return True


def recognize_boxes(img: Image.Image, min_conf: float = MIN_CONF) -> List[TextBox]:
    """跑一次 OCR，返回文字框（图像像素）。任何异常都返回空列表。

    OCR 只是补标注，绝不能因为它让主流程抛错——历史上一次 OCR 崩溃
    会把整步的标注全废掉（连 AX 的框也一起没了），所以这里全部吞掉。
    """
    if img is None or not ocr.backend_name():
        return []
    try:
        return ocr.recognize(img, min_confidence=min_conf)
    except Exception:
        return []


def _clean_text(raw: str) -> str:
    return " ".join(str(raw or "").split())


def elements_from_boxes(
    boxes: Sequence[TextBox],
    coord_scale: float = 1.0,
    max_boxes: int = MAX_BOXES,
    skip_top_px: float = 0.0,
    allow_rects: Optional[Sequence[Tuple[float, float, float, float]]] = None,
) -> List[UIElement]:
    """把 OCR 文字框转成可标注的 UIElement（逻辑像素）。

    skip_top_px: 跳过截图顶部多少**图像像素**（图像像素！不是逻辑像素）。
    用于剔掉菜单栏——文字密集、几乎用不到，还会占掉一大排编号。

    allow_rects: 只保留中心落在这些矩形里的文字（逻辑像素）。
    窗口范围外的文字一律不要——OCR 读的是**整张截图**，不限制的话会把后面
    那些窗口（浏览器、聊天工具）里的文字也编号上，模型会去点背景窗口，
    这就把 `som_frontmost_only` 的降噪效果全废了。
    """
    scale = float(coord_scale or 1.0)
    picked: List[Tuple[float, TextBox]] = []
    seen: set = set()

    for b in boxes:
        text = _clean_text(b.text)
        if not text or len(text) > MAX_TEXT_CHARS:
            continue
        if not _keep_text(text):
            continue
        if b.w < MIN_BOX_W or b.h < MIN_BOX_H:
            continue
        if skip_top_px and (b.y + b.h / 2) < skip_top_px:
            continue
        if allow_rects:
            lx, ly = (b.x + b.w / 2) / scale, (b.y + b.h / 2) / scale
            if not any(
                (r[0] - 8) <= lx <= (r[0] + r[2] + 8) and (r[1] - 8) <= ly <= (r[1] + r[3] + 8)
                for r in allow_rects
            ):
                continue
        # 同一段文字在极小范围内被识别两次（Vision 偶发）→ 只留一个
        key = (text.lower(), round(b.x / 8), round(b.y / 8))
        if key in seen:
            continue
        seen.add(key)
        picked.append((float(b.confidence), b))

    # 超额时先留置信度高的（位置更可信），再按阅读顺序排回去，
    # 让编号表的顺序跟人眼扫屏幕的顺序一致。
    if len(picked) > max_boxes:
        picked.sort(key=lambda t: t[0], reverse=True)
        picked = picked[:max_boxes]

    out: List[UIElement] = []
    for _conf, b in picked:
        out.append(
            UIElement(
                id=0,
                role=OCR_ROLE,
                title=_clean_text(b.text),
                x=b.x / scale,
                y=b.y / scale,
                w=b.w / scale,
                h=b.h / scale,
                app=OCR_APP,
                kind="ocr",
                enabled=True,
            )
        )
    out.sort(key=lambda e: (round(e.y / 10), e.x))
    for i, el in enumerate(out):
        el.id = i
    return out


def _rect(el) -> Tuple[float, float, float, float]:
    return el.x, el.y, el.x + el.w, el.y + el.h


def _overlap_ratio(a: Tuple[float, float, float, float],
                   b: Tuple[float, float, float, float]) -> float:
    """交叠面积 / 较小矩形面积。

    为什么除以「较小」而不是并集：OCR 文字通常在按钮/单元格内部，
    它的面积本来就比容器小得多，用并集算相似度永远到不了阈值。
    """
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(area_a, area_b)


def drop_covered(
    candidates: Sequence[UIElement],
    existing: Sequence[UIElement],
    ratio: float = COVER_RATIO,
) -> List[UIElement]:
    """丢掉「已经有 AX 编号框盖住」的 OCR 文字（同一块地方不重复编号）。"""
    if not existing:
        return list(candidates)
    rects = [_rect(e) for e in existing]
    out: List[UIElement] = []
    for c in candidates:
        cr = _rect(c)
        if any(_overlap_ratio(cr, r) >= ratio for r in rects):
            continue
        out.append(c)
    return out


def fill_titles(
    elements: Sequence[UIElement],
    boxes: Sequence[TextBox],
    coord_scale: float = 1.0,
    pad: float = 8.0,
) -> List[UIElement]:
    """给「AX 里没有名字」的元素用框内的 OCR 文字当名字。

    典型场景：工具栏上的纯图标按钮（AXButton + 无 title）。AX 不知道它是什么，
    但图标下面/旁边就写着「保存」。用 OCR 补上名字，编号表才有信息量
    （否则模型看到的是「#7: （无标签）」，只能靠猜）。

    只在这两种情况下采用，宁可漏也不要标错：
    1. 只有一个文字框落在元素矩形（外扩 pad）里 → 直接用它；
    2. 多个文字框 → 取离元素中心最近的那个。
    """
    if not boxes or not elements:
        return list(elements)
    scale = float(coord_scale or 1.0)
    out: List[UIElement] = []
    for el in elements:
        if (el.title or "").strip() or el.kind != "app":
            out.append(el)
            continue
        x0, y0 = el.x - pad, el.y - pad
        x1, y1 = el.x + el.w + pad, el.y + el.h + pad
        inside = [
            b for b in boxes
            if x0 <= (b.x + b.w / 2) / scale <= x1 and y0 <= (b.y + b.h / 2) / scale <= y1
        ]
        if not inside:
            out.append(el)
            continue
        ecx, ecy = el.cx, el.cy
        best = min(
            inside,
            key=lambda b: ((b.x + b.w / 2) / scale - ecx) ** 2
            + ((b.y + b.h / 2) / scale - ecy) ** 2,
        )
        text = _clean_text(best.text)
        if not text:
            out.append(el)
            continue
        out.append(replace(el, title=text))
    return out


def count_ocr(elements: Iterable[UIElement]) -> int:
    """数一下这批元素里有多少是 OCR 补的（日志/事件用）。"""
    return sum(1 for el in elements if el.kind == "ocr")
