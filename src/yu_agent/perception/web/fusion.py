"""三模态融合：把「DOM 布局 + AX 语义 + 视觉校验」合成一份可点击目标列表。

## 融合策略（顺序很重要）

1. **DOM 提供候选与几何**（谁在那儿、多大、什么层级）；
2. **AX 提供语义**（它是什么、叫什么、能不能点）；
3. **视觉做裁决**（真的在那儿吗、字对得上吗、被盖住了吗、灰了吗）；
4. **冲突时视觉优先**——因为用户和模型看到的都是画面。DOM 说"存在且可见"、
   画面上却是空白/被弹窗盖住，那就是 DOM 过时了（React 虚拟 DOM 与真实渲染之间
   本来就允许短暂不一致）。

冲突不是"处理掉就算了"，而是**必须记录**：每条冲突都带 `kind/target/detail` 交给
Reflector，任务结束后能回答"这次为什么点空"——这正是"只依赖单一信号源"的 Agent
永远查不出来的东西。

## 置信度怎么算（不是玄学，是可解释的加分项）

| 证据 | 变化 |
| --- | --- |
| DOM + AX 都有且 AX 未被忽略（双模态一致） | +0.20 |
| 视觉文字匹配 ≥ 阈值 | +0.20 |
| 浏览器标了 isClickable | +0.10 |
| 只有 DOM、AX 缺失且没 OCR 文字（纯猜） | −0.30 |
| 视觉判为遮挡 / 空白 | 直接淘汰（不是降权） |

阈值只用于"要不要给模型看"，不做安全判断；危险动作的审批在 `safety/` 走另一套。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from ..a11y import UIElement
from .align import Aligned
from .layout import LayoutNode
from .visual import TEXT_MATCH_MIN, VisualVerdict

# 网页 role → 现有编号表用的 AX 角色名（让提示词里的"按钮/输入框"读起来一致）
_AX_ROLE = {
    "button": "AXButton",
    "link": "AXLink",
    "textbox": "AXTextField",
    "searchbox": "AXSearchField",
    "checkbox": "AXCheckBox",
    "radio": "AXRadioButton",
    "switch": "AXCheckBox",
    "combobox": "AXComboBox",
    "listbox": "AXList",
    "option": "AXMenuItem",
    "menuitem": "AXMenuItem",
    "menuitemcheckbox": "AXMenuItem",
    "tab": "AXRadioButton",
    "slider": "AXSlider",
    "spinbutton": "AXIncrementor",
    "treeitem": "AXRow",
    "gridcell": "AXCell",
    "cell": "AXCell",
    "img": "AXImage",
}
# 单个节点覆盖率超过它就算"被盖住了"（部分重叠是正常的，比如悬浮按钮压在内容上）
OCCLUDE_RATIO = 0.6
# 覆盖物自身必须基本不透明，否则是半透明遮罩，仍能看见/点到
OPAQUE_MIN = 0.5


@dataclass
class FusedTarget:
    """三模态合流后的一个目标。"""

    target: Aligned
    verdict: VisualVerdict
    sources: List[str] = field(default_factory=list)
    confidence: float = 0.5
    conflicts: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.target.label

    @property
    def rect(self) -> Tuple[float, float, float, float]:
        return self.target.dom.rect

    def __str__(self) -> str:  # 便于日志
        return (f"<{self.target.role or '?'} {self.name[:20]!r} "
                f"{self.confidence:.2f} {'+'.join(self.sources)}>")


@dataclass
class FusionResult:
    targets: List[FusedTarget] = field(default_factory=list)
    dropped: List[FusedTarget] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)


def covering_rects(nodes: Iterable[LayoutNode]) -> List[Tuple[float, float, float, float, int]]:
    """算出可能遮挡别人的矩形（按 z 从小到大）。

    只收"面积足够大、基本不透明、pointer-events 不是 none"的节点：
    小图标不会挡住整块，半透明遮罩能看见下面，`pointer-events:none` 的装饰层不拦点击。
    """
    out = []
    for n in nodes:
        if not n.visible or n.opacity < OPAQUE_MIN:
            continue
        if n.w * n.h < 400:          # 20×20 以下不可能是浮层
            continue
        out.append((n.x, n.y, n.w, n.h, n.z))
    out.sort(key=lambda r: r[4])
    return out


def fuse(
    aligned: Sequence[Aligned],
    verdicts: Dict[int, VisualVerdict],
    *,
    min_confidence: float = 0.35,
) -> FusionResult:
    """合并并对冲突做裁决。

    verdicts 的 key 是 `id(target.dom)`（同一批对象，避免再定义一套 id）。
    """
    res = FusionResult()
    for target in aligned:
        v = verdicts.get(id(target.dom)) or VisualVerdict(notes=["no_verdict"])
        fused = FusedTarget(target=target, verdict=v)

        # ── 证据与置信度 ──────────────────────────────────────────
        if target.ax is not None and not target.ax.ignored:
            fused.sources.append("dom+ax")
            fused.confidence = 0.5 + 0.20
        else:
            fused.sources.append("dom")
            fused.confidence = 0.5
        if target.dom.clickable:
            fused.confidence += 0.10
        if v.vision_confirmed:
            fused.sources.append("ocr")
            fused.confidence += 0.20
        if v.occluded:
            fused.sources.append("pixel")
        if "no_ax" in target.flags and not v.ocr_text:
            fused.confidence -= 0.30

        # ── 裁决 1：看不见 / 被挡住 → 淘汰 ───────────────────────
        if v.occluded:
            fused.conflicts.append("occluded")
            fused.confidence = min(fused.confidence, 0.2)
            res.dropped.append(fused)
            res.conflicts.append({
                "kind": "occluded",
                "target": target.label,
                "detail": ";".join(v.notes) or "视觉判定被遮挡",
            })
            continue
        if v.blank and not v.ocr_text:
            fused.conflicts.append("blank")
            fused.confidence = min(fused.confidence, 0.2)
            res.dropped.append(fused)
            res.conflicts.append({
                "kind": "blank",
                "target": target.label,
                "detail": "DOM 报可见，但截图上这块没有任何内容",
            })
            continue

        # ── 裁决 2：名字对不上 → 采信画面上的字 ───────────────────
        # 判据是"画面文字和 **DOM 文字** 一致"（不是和 AX 名一致）：
        # 这条冲突的本质就是 AX 名写错了（典型 aria-label="Submit order" 而页面上写着"提交订单"）。
        if "name_mismatch" in target.flags:
            if v.ocr_text and (v.dom_match or 0) >= TEXT_MATCH_MIN:
                fused.conflicts.append("name_mismatch")
                res.conflicts.append({
                    "kind": "name_mismatch",
                    "target": target.label,
                    "detail": f"AX 名与画面文字不一致，已改用「{v.ocr_text[:20]}」",
                })
                target.name = v.ocr_text[:40]
            else:
                fused.conflicts.append("name_mismatch")

        # ── 裁决 3：禁用状态的"反证" ──────────────────────────────
        # 只用像素去**反驳** DOM 的 disabled（很可靠：字清清楚楚还在，说明根本没灰）
        # 反之"像素看着像禁用"不作为证据 —— 深浅主题/彩色按钮都会骗过像素判断。
        dom_disabled = "disabled" in target.flags
        if dom_disabled and not v.disabled_look and (v.text_match or 0) >= TEXT_MATCH_MIN:
            fused.conflicts.append("disabled_conflict")
            res.conflicts.append({
                "kind": "disabled_conflict",
                "target": target.label,
                "detail": "DOM/AX 标了 disabled，但像素上清晰可读（可能 aria-disabled 与样式脱节）",
            })

        if fused.confidence < min_confidence:
            res.dropped.append(fused)
            continue
        res.targets.append(fused)

    res.targets.sort(key=lambda t: (-t.confidence, t.rect[1], t.rect[0]))
    res.stats = {
        "fused": len(res.targets),
        "dropped": len(res.dropped),
        "conflicts": len(res.conflicts),
        "with_ocr": sum(1 for t in res.targets if "ocr" in t.sources),
        "with_ax": sum(1 for t in res.targets if t.target.ax is not None),
    }
    return res


def to_ui_elements(
    result: FusionResult,
    *,
    origin: Tuple[float, float] = (0.0, 0.0),
    zoom: float = 1.0,
    app: str = "浏览器",
) -> List[UIElement]:
    """转成现有 SOM 用的 UIElement（逻辑像素）。

    这一步是**唯一的接入点**：融合结果只要能变成 UIElement，
    就能直接喂给 `som.build_som_mapping(elements=…)`，编号/画框/查表全部复用现成逻辑，
    不需要改主循环里的标注代码。
    """
    out: List[UIElement] = []
    for i, f in enumerate(result.targets):
        x, y, w, h = f.rect
        name = f.name
        if "disabled" in f.target.flags:
            name = f"{name}（已禁用）" if name else "（已禁用）"
        out.append(
            UIElement(
                id=i,
                role=_AX_ROLE.get(f.target.role, "AXGroup"),
                title=name,
                description=f"web:{f.target.role}",
                x=origin[0] + x * zoom,
                y=origin[1] + y * zoom,
                w=w * zoom,
                h=h * zoom,
                app=app,
                kind="web",
                enabled="disabled" not in f.target.flags,
            )
        )
    return out


def append_ocr_only(
    elements: List[UIElement],
    ocr_elements: Iterable[UIElement],
) -> List[UIElement]:
    """把"只有视觉才有"的文字目标补进去（canvas 绘制、shadow 深层、AX 被屏蔽的浮层）。

    ⚠️ `ocr_elements` 必须是**逻辑像素**（`ocr_marks.elements_from_boxes` 的输出就是），
    和 `elements` 同一套坐标系；混进图像像素会让补出来的目标整体偏移（Retina 上偏一倍）。
    """
    existing = [(e.x, e.y, e.x + e.w, e.y + e.h) for e in elements]
    out = list(elements)
    for el in ocr_elements:
        cand = (el.x, el.y, el.x + el.w, el.y + el.h)
        if any(_iou(cand, r) >= 0.5 for r in existing):
            continue
        existing.append(cand)
        el.kind = "web"
        el.id = len(out)
        out.append(el)
    return out


def _iou(a: Sequence[float], b: Sequence[float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    aa = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    ab = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(aa, ab)


def conflict_summary(result: FusionResult, limit: int = 5) -> str:
    """给提示词/日志用的一行摘要（丢掉的元素也让它知道，避免反复点同一个被遮挡的目标）。"""
    if not result.conflicts:
        return ""
    parts = []
    for c in result.conflicts[:limit]:
        parts.append(f"{c['kind']}:{c['target'][:16]}")
    more = "" if len(result.conflicts) <= limit else f" 等 {len(result.conflicts)} 项"
    return "；".join(parts) + more
