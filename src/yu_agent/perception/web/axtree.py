"""AXTree 解析：CDP `Accessibility.getFullAXTree` → 语义节点。

## 为什么必须用 CDP 的 getFullAXTree

Playwright 的 `page.accessibility.snapshot()` 默认 **`interestingOnly=True`**：
它会主动丢掉"看起来没意思"的节点（generic 容器、纯文本、被忽略的包装层）。
结果是：
- 一大片 DOM 元素在 AX 里"消失"了 → 对齐率低，只能靠几何猜；
- 反过来，有些按钮因为被包在 generic 里，拿到的 name 是空字符串。

`Accessibility.getFullAXTree` 拿的是**未经筛减**的完整树（`ignored` 字段自己判），
对齐才谈得上准确。这个坑值得单独写一行注释：默认参数会把你的数据吃掉一半。

## 坑

1. `ignored: true` 不代表"不存在"：可能只是 `aria-hidden` 的应用。它依然占位，
   要保留在树里（父节点信息需要），但**不能当作可点目标**。
2. `name` 有三种来源（`sources`：attribute / contents / relatedElement）。
   `aria-label` 会盖掉可见文字 —— 视觉校验时如果"AX 名"和"页面上的字"不一致，
   优先相信页面上看到的字（用户就是这么认的）。
3. `backendDOMNodeId` 是**唯一稳定的跨模态锚点**（DOMSnapshot 的 backendNodeId 同源）。
   没有它的节点是浏览器合成的（如 iframe 的 document、伪元素、表格行），
   只能挂到最近的祖先上继承几何 —— 这就是"AX 树与 DOM 不是一一对应"的具体表现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# 这些 role 基本不可能是点击目标（结构/装饰），对齐时降权
STRUCTURAL_ROLES = {
    "generic", "none", "presentation", "group", "document", "rootwebarea",
    "main", "region", "article", "list", "listitem", "row", "cell", "table",
    "paragraph", "statictext", "inline", "linebreak", "separator",
}

# 有语义、值得作为目标展示的 role
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "switch",
    "combobox", "listbox", "option", "menuitem", "menuitemcheckbox", "tab",
    "slider", "spinbutton", "treeitem", "gridcell", "cell", "img",
}


@dataclass
class AXNode:
    node_id: str = ""
    parent_id: str = ""
    backend_node_id: int = 0
    role: str = ""
    name: str = ""
    ignored: bool = False
    disabled: bool = False
    focusable: bool = False
    hidden: bool = False
    checked: Optional[bool] = None
    expanded: Optional[bool] = None
    value: str = ""
    children: List[str] = field(default_factory=list)

    @property
    def interactive(self) -> bool:
        return (not self.ignored) and (self.role.lower() in INTERACTIVE_ROLES)

    def __str__(self) -> str:  # 便于日志
        flag = " (ignored)" if self.ignored else ""
        return f"[{self.role or '?'}] {self.name[:24]!r} <- dom#{self.backend_node_id}{flag}"


def _val(node: dict, key: str, default: Any = "") -> Any:
    """AX 节点的字段统一是 {"type": ..., "value": ...} 包装。"""
    raw = node.get(key)
    if isinstance(raw, dict):
        return raw.get("value", default)
    return default if raw is None else raw


def _props(node: dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for p in node.get("properties") or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name") or ""
        val = p.get("value")
        out[name] = val.get("value") if isinstance(val, dict) else val
    return out


def parse_ax_tree(payload: dict) -> Dict[str, AXNode]:
    """解析 getFullAXTree 的返回，返回 nodeId → AXNode。

    同时填好 parent_id（原始返回只有 childIds，反向关系要自己建）。
    """
    nodes: Dict[str, AXNode] = {}
    raw_nodes = (payload or {}).get("nodes") or []
    parent_of: Dict[str, str] = {}

    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        nid = str(raw.get("nodeId") or raw.get("nodeId".lower()) or "")
        if not nid:
            continue
        props = _props(raw)
        role = str(_val(raw, "role") or "").lower()
        children = [str(c) for c in (raw.get("childIds") or [])]
        for c in children:
            parent_of[c] = nid
        nodes[nid] = AXNode(
            node_id=nid,
            backend_node_id=int(raw.get("backendDOMNodeId") or 0),
            role=role,
            name=str(_val(raw, "name") or "").strip()[:120],
            ignored=bool(raw.get("ignored")),
            disabled=bool(props.get("disabled") or props.get("readonly") and False),
            focusable=bool(props.get("focusable")),
            hidden=bool(props.get("hidden", False)),
            checked=props.get("checked") if isinstance(props.get("checked"), bool) else None,
            expanded=props.get("expanded") if isinstance(props.get("expanded"), bool) else None,
            value=str(_val(raw, "value") or "")[:120],
            children=children,
        )

    for nid, node in nodes.items():
        node.parent_id = parent_of.get(nid, "")
    return nodes


def by_backend(nodes: Dict[str, AXNode]) -> Dict[int, List[AXNode]]:
    """backendDOMNodeId → AX 节点（一个 DOM 节点可能对应多个 AX 节点）。

    实测："被合并"的语义（一个 checkbox 同时有 checkbox + text 两个 AX 节点）
    和"被拆分"的情况都存在，所以这里必须是 list，取第一个会随机丢语义。
    """
    out: Dict[int, List[AXNode]] = {}
    for n in nodes.values():
        if n.backend_node_id:
            out.setdefault(n.backend_node_id, []).append(n)
    return out


def best_semantics(cands: Iterable[AXNode]) -> Optional[AXNode]:
    """同一 DOM 节点对应多个 AX 节点时，挑语义信息最全的那个。

    优先：非 ignored > 有名字 > 交互 role > 无。
    """
    best: Optional[AXNode] = None
    best_score = -1.0
    for n in cands:
        s = 0.0
        if not n.ignored:
            s += 2
        if n.name:
            s += 2
        if n.role in INTERACTIVE_ROLES:
            s += 1
        if n.disabled:
            s += 0.5
        if s > best_score:
            best, best_score = n, s
    return best


def ancestor_with_backend(
    node: AXNode, nodes: Dict[str, AXNode], max_depth: int = 6
) -> Optional[AXNode]:
    """往上找最近的、带 backendDOMNodeId 的祖先。

    用途：浏览器合成的节点（iframe document、伪元素、表格行）没有 backend id，
    几何只能从祖先继承 —— 不继承就会得到一堆 0 尺寸的"幽灵目标"。
    """
    cur = node
    for _ in range(max_depth):
        if cur.backend_node_id:
            return cur
        parent = nodes.get(cur.parent_id)
        if parent is None:
            return None
        cur = parent
    return None
