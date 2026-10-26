"""DOM ↔ AXTree 对齐：给布局节点绑上语义，并显式记录"对不上的那些"。

三模态里最容易糊弄过去的一步就是"对齐"：很多实现直接按 DOM 顺序 zip AX 树，
看着能跑，实际上从第一个 `aria-hidden` 之后全部错位。这里按可信度分三级：

1. **backendDOMNodeId 精确命中**（唯一可靠锚点）：DOM 快照和 AX 树出自同一个 Blink 内部节点，
   这个 id 就是同一片 DOM，跨模态稳定，不受重排影响。
2. **合成节点继承**：AX 里没有 backend id 的节点（iframe 的 document、伪元素、表格行），
   沿 AX 父链向上找最近的带 id 祖先，几何继承 —— 几何会偏大，但至少不是 0 尺寸。
3. **对不上就如实标出来**：
   - `no_ax`：DOM 有、AX 没有 —— 典型是 `aria-hidden="true"`、被 `role="presentation"` 抹掉、
     或者浏览器没把它算进语义树（纯装饰）。这类仍然可以点，只是没有语义，交给 OCR 补名字。
   - `aria_hidden`：明确被 aria-hidden 屏蔽 —— 视觉上可能看得见，但屏幕阅读器（和 AX 树）
     当它不存在。**这种元素要点就必须靠视觉/OCR**，是"只信 a11y"最典型的翻车点。
   - `name_mismatch`：AX 名和页面上的字差太远（aria-label 覆盖了可见文字）。
     不直接判错，只打标，让视觉校验最终裁决。

## 坑

- **不能按顺序配对**：AX 树是"语义树"，DOM 是"盒模型树"，中间隔着 display:none、
  aria-hidden、role=presentation、shadow 边界，顺序根本没对齐。
- **一个 DOM 节点可能对应多个 AX 节点**（合并/拆分都常见），必须全收再挑最全的语义。
- **backendNodeId 会随页面重载变化**，跨帧复用必须重新采集，不能缓存成"永久 id"。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .axtree import (
    INTERACTIVE_ROLES,
    STRUCTURAL_ROLES,
    AXNode,
    ancestor_with_backend,
    best_semantics,
    by_backend,
)
from .layout import LayoutNode

_PUNCT = re.compile(r"[\s\u3000\-_/\\,，.。:：;；!！?？'\"“”‘’()（）\[\]【】|·…]+")
# 名称相似度低于这个就算"对不上"（只用来打标，不用于丢元素）
NAME_MATCH_MIN = 0.45


def norm(s: str) -> str:
    """归一化：去掉空白/标点、统一小写。中英文都不分词，直接整串比较。"""
    return _PUNCT.sub("", (s or "").lower())


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class Aligned:
    """一个已绑语义的候选目标。"""

    dom: LayoutNode
    ax: Optional[AXNode] = None
    role: str = ""
    name: str = ""
    flags: List[str] = field(default_factory=list)
    extra_ax: List[AXNode] = field(default_factory=list)

    @property
    def label(self) -> str:
        return (self.name or self.dom.label).strip()

    @property
    def actionable(self) -> bool:
        """结构角色且没有语义 → 不值得作为"目标"展示给模型。"""
        return self.role in INTERACTIVE_ROLES or bool(self.dom.clickable and self.label)

    def has(self, flag: str) -> bool:
        return flag in self.flags

    def __str__(self) -> str:  # 便于日志
        f = f" [{','.join(self.flags)}]" if self.flags else ""
        return f"<{self.role or '?'} {self.label[:24]!r} @({self.dom.x:.0f},{self.dom.y:.0f}){f}>"


@dataclass
class AlignResult:
    targets: List[Aligned] = field(default_factory=list)
    ax_only: List[AXNode] = field(default_factory=list)   # AX 有、DOM 候选里没有
    stats: Dict[str, int] = field(default_factory=dict)


def align(
    dom_nodes: Iterable[LayoutNode],
    ax_nodes: Dict[str, AXNode],
    *,
    keep_structural: bool = False,
) -> AlignResult:
    """把候选 DOM 节点和 AX 树对齐。

    keep_structural: True 时连结构节点（list/row/cell…）一起返回，
    用于调试对齐率；正常标记时为 False（框太多反而干扰模型）。
    """
    dom = list(dom_nodes)
    idx = by_backend(ax_nodes)
    result = AlignResult()

    for node in dom:
        cands = idx.get(node.backend_node_id) or []
        ax = best_semantics(cands)
        extra = [c for c in cands if c is not ax]
        target = Aligned(dom=node, ax=ax, extra_ax=extra)

        if ax is None:
            target.flags.append("no_ax")
            target.role = node.role_hint or ""
            target.name = node.label
        else:
            target.role = ax.role or node.role_hint or ""
            target.name = ax.name or node.label
            if ax.ignored:
                target.flags.append("ax_ignored")
            # aria-label 盖掉可见文字 → 打标，等视觉裁决
            if ax.name and node.label and similarity(ax.name, node.label) < NAME_MATCH_MIN:
                target.flags.append("name_mismatch")
            if ax.disabled or node.disabled:
                target.flags.append("disabled")

        # ⚠️ aria-hidden 与"有没有 AX 节点"无关：被屏蔽的元素恰恰经常**没有** AX 节点，
        # 早期写在 else 分支里，等于最该报警的那一类永远不打标。
        if node.attrs.get("aria-hidden") == "true":
            target.flags.append("aria_hidden")
        if node.inside_shadow:
            target.flags.append("shadow")
        if not node.pointer_events:
            target.flags.append("pointer_none")

        if not target.actionable and not keep_structural:
            continue
        if target.role in STRUCTURAL_ROLES and not target.label:
            continue
        result.targets.append(target)

    # AX 有语义但 DOM 候选里找不到对应节点：多出现在 iframe document / 虚拟化列表
    dom_ids = {n.backend_node_id for n in dom}
    seen_ax = {id(t.ax) for t in result.targets if t.ax is not None}
    for n in ax_nodes.values():
        if not n.interactive or id(n) in seen_ax:
            continue
        if n.backend_node_id and n.backend_node_id in dom_ids:
            continue  # 已经对齐过了
        if not n.backend_node_id:
            host = ancestor_with_backend(n, ax_nodes)
            if host is None:
                continue  # 纯合成节点且无祖先 → 没有几何，无法点
        result.ax_only.append(n)

    result.stats = {
        "dom": len(dom),
        "ax": len([n for n in ax_nodes.values() if not n.ignored]),
        "targets": len(result.targets),
        "no_ax": sum(1 for t in result.targets if t.has("no_ax")),
        "aria_hidden": sum(1 for t in result.targets if t.has("aria_hidden")),
        "name_mismatch": sum(1 for t in result.targets if t.has("name_mismatch")),
        "ax_only": len(result.ax_only),
    }
    return result
