"""CDP 采集 + 融合编排：一次握手拿齐三路信号，再交给 `fusion`。

采集顺序刻意固定为 **DOM 快照 → AX 树 → 截图**：

- 先布局后语义：DOMSnapshot 和 getFullAXTree 属于同一个渲染器进程，两次调用之间的
  时间差通常在 10~30ms，页面不做动画时完全一致；
- 截图最后：它一定"晚于"上面两份数据，所以当"DOM 说有、截图没有"时，
  更可能是页面在两次调用之间刚刚重排（刷新/动画），而不是截图坏了 —— 这条判断规则
  写在 `fusion` 里（视觉优先），这里只负责把时间戳记下来，便于复现。

## 为什么不在这一步逐元素 querySelector

一百个控件逐个 `getBoundingClientRect` 要 1~2 秒，页面早变了；而且每个元素一次往返，
拿到的坐标彼此都不是同一时刻的。`captureSnapshot` 是单次调用返回全量，天然一致。

## interestingOnly 的坑

Playwright 的 `page.accessibility.snapshot()` 默认 `interestingOnly=True`，会主动丢掉
generic 容器与部分文本节点 —— 对齐率直接腰斩。这里走 CDP 的 `Accessibility.getFullAXTree`，
默认就是全量（`ignored` 自己判）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PIL import Image

from .. import ocr as ocr_mod
from ..a11y import UIElement
from ..ocr_marks import elements_from_boxes
from .align import align
from .axtree import parse_ax_tree
from .fusion import FusionResult, append_ocr_only, covering_rects, fuse, to_ui_elements
from .layout import candidates, parse_snapshot
from .visual import check

# 只取这几条计算样式：可见性/层级判断够用，整表拉回来会让 payload 涨好几倍
COMPUTED_STYLES = [
    "display", "visibility", "opacity", "z-index", "pointer-events",
    "overflow-x", "overflow-y", "position",
]

DEFAULT_MAX_TARGETS = 120


@dataclass
class WebPerception:
    """一帧网页感知的完整产物。"""

    result: FusionResult
    elements: List[UIElement]
    viewport: Tuple[float, float] = (0.0, 0.0)
    meta: Dict[str, Any] = field(default_factory=dict)


def collect_raw(session, *, include_paint_order: bool = True) -> Dict[str, Any]:
    """从 CDP 会话采集原始三路数据（不做解析）。

    session: Playwright 的 `CDPSession`（`page.context.new_cdp_session(page)`），
             或任何有 `.send(method, params)` 的同类对象（pychrome / 裸 WebSocket 客户端）。
    """
    session.send("DOM.enable")
    try:
        session.send("Accessibility.enable")
    except Exception:
        pass  # 老版本 Chrome 不需要显式 enable
    session.send("DOM.getDocument", {"depth": -1, "pierce": True})

    params: Dict[str, Any] = {
        "computedStyles": COMPUTED_STYLES,
        "includeDOMRects": True,
    }
    if include_paint_order:
        params["includePaintOrder"] = True

    t0 = time.time()
    snapshot = session.send("DOMSnapshot.captureSnapshot", params)
    t1 = time.time()
    ax = session.send("Accessibility.getFullAXTree")
    t2 = time.time()

    metrics: Dict[str, Any] = {}
    try:
        metrics = session.send("Page.getLayoutMetrics")
    except Exception:
        pass

    return {
        "snapshot": snapshot,
        "ax": ax,
        "metrics": metrics,
        "timing": {"snapshot_ms": round((t1 - t0) * 1000), "ax_ms": round((t2 - t1) * 1000)},
    }


def build(
    raw: Dict[str, Any],
    *,
    screenshot: Optional[Image.Image] = None,
    ocr_boxes: Iterable = (),
    origin: Tuple[float, float] = (0.0, 0.0),
    zoom: float = 1.0,
    image_scale: float = 1.0,
    app: str = "浏览器",
    frame_offsets: Optional[Dict[str, Tuple[float, float]]] = None,
    max_targets: int = DEFAULT_MAX_TARGETS,
    keep_structural: bool = False,
) -> WebPerception:
    """把三路原始数据融合成目标列表（纯函数，测试可以直接喂 fixture）。

    origin: 页面内容区左上角在屏幕上的**逻辑像素**坐标（浏览器窗口内容区，不含标题栏/工具栏）。
    zoom: 浏览器页面缩放（100% = 1.0）。DOM 的 CSS px 乘它才是逻辑像素。
    image_scale: 截图图像像素 / 逻辑像素（Retina 2x 时为 2.0）。
    """
    viewport = _viewport(raw.get("metrics"))
    nodes = parse_snapshot(raw.get("snapshot") or {}, viewport=viewport or None,
                           frame_offsets=frame_offsets)
    ax_nodes = parse_ax_tree(raw.get("ax") or {})
    aligned = align(candidates(nodes), ax_nodes, keep_structural=keep_structural)

    cover = covering_rects(nodes)
    verdicts = {}
    for t in aligned.targets:
        verdicts[id(t.dom)] = check(
            t, screenshot,
            origin=origin, zoom=zoom, image_scale=image_scale,
            ocr_boxes=ocr_boxes, covering=cover,
        )
    fused = fuse(aligned.targets, verdicts)

    elements = to_ui_elements(fused, origin=origin, zoom=zoom, app=app)
    if elements:
        # 只保留置信度最高的一批（低分的先扔，编号太多模型看不过来）
        elements = sorted(elements, key=lambda e: (e.y, e.x))[:max_targets]

    # 视觉独有目标（canvas / shadow 深层 / ARIA 被屏蔽的浮层）：OCR 补最后一块拼图
    if ocr_boxes:
        extra = elements_from_boxes(list(ocr_boxes), coord_scale=image_scale, max_boxes=30)
        elements = append_ocr_only(elements, extra)

    meta = {
        "viewport": viewport,
        "origin": origin,
        "zoom": zoom,
        "image_scale": image_scale,
        "timing": raw.get("timing") or {},
        "layout_nodes": len(nodes),
        "ax_nodes": len(ax_nodes),
        **{f"align_{k}": v for k, v in aligned.stats.items()},
        **{f"fuse_{k}": v for k, v in fused.stats.items()},
        "ocr_backend": ocr_mod.backend_name(),
    }
    return WebPerception(result=fused, elements=elements, viewport=viewport, meta=meta)


def perceive(
    page,
    *,
    origin: Tuple[float, float],
    zoom: float = 1.0,
    image_scale: float = 1.0,
    app: str = "浏览器",
    screenshot: Optional[Image.Image] = None,
    ocr: bool = True,
    max_targets: int = DEFAULT_MAX_TARGETS,
) -> WebPerception:
    """对 Playwright 页面跑一次完整的三模态感知。

    `origin` 必须由调用方给出（页面内容区在屏幕上的位置）：CDP 只知道"视口内坐标"，
    不知道浏览器窗口在屏幕的哪儿。给错就是"稳定偏移 N 像素"，别在这里猜。
    """
    session = page.context.new_cdp_session(page)
    try:
        raw = collect_raw(session)
        shot = screenshot if screenshot is not None else page.screenshot(type="png")
        img = shot if isinstance(shot, Image.Image) else _bytes_to_image(shot)
        boxes: List[Any] = []
        if ocr and ocr_mod.backend_name():
            boxes = ocr_mod.recognize(img, min_confidence=0.30)
        return build(
            raw, screenshot=img, ocr_boxes=boxes,
            origin=origin, zoom=zoom, image_scale=image_scale,
            app=app, max_targets=max_targets,
        )
    finally:
        try:
            session.detach()
        except Exception:
            pass


def _viewport(metrics: Optional[dict]) -> Tuple[float, float]:
    if not metrics:
        return (0.0, 0.0)
    vp = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
    try:
        return (float(vp.get("clientWidth") or 0), float(vp.get("clientHeight") or 0))
    except (TypeError, ValueError):
        return (0.0, 0.0)


def _bytes_to_image(data: bytes) -> Image.Image:
    import io

    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGB")


def dump_table(perception: WebPerception, limit: int = 40) -> List[str]:
    """把融合结果打成便于人看的表（排查"为什么这个元素没标上"）。"""
    lines = []
    stats = perception.meta
    lines.append(
        f"DOM {stats.get('layout_nodes')} 节点 / AX {stats.get('ax_nodes')} 节点 → "
        f"对齐 {stats.get('align_targets')} 个（no_ax {stats.get('align_no_ax')}，"
        f"aria_hidden {stats.get('align_aria_hidden')}）→ 融合 {stats.get('fuse_fused')} 个，"
        f"淘汰 {stats.get('fuse_dropped')} 个，冲突 {stats.get('fuse_conflicts')} 条"
    )
    for el in perception.elements[:limit]:
        lines.append(
            f"  #{el.id:<3} {el.role:<14} {el.title[:28]:<30} "
            f"@({int(el.x)},{int(el.y)}) {int(el.w)}x{int(el.h)}"
        )
    for c in perception.result.conflicts[:limit]:
        lines.append(f"  ! {c['kind']}: {c['target'][:24]} — {c['detail'][:40]}")
    return lines
