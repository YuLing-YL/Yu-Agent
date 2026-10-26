"""DOM 布局快照解析：CDP `DOMSnapshot.captureSnapshot` → 候选交互节点。

## 为什么用 DOMSnapshot 而不是 querySelector + getBoundingClientRect

- **一次往返**：`captureSnapshot` 把整棵（含 shadow DOM 的）扁平树、每个节点的布局盒、
  计算样式、可见文字一次性拿回来。逐元素 `Runtime.evaluate` 一百个控件要 1~2 秒，
  而这期间页面还可能重排 —— 拿到的坐标互相不是同一个瞬间的。
- **浏览器自己算的 isClickable**：命中了 :hover/:active/pointer 事件监听的节点会被标出来，
  比"按 tag 白名单猜"准得多。
- **能看见裁剪和层级**：`overflow:hidden` 裁掉的部分、`z-index` 叠放顺序都在里面，
  这是判断"元素是不是被弹窗盖住"的依据（截图只能判"看起来被盖住"，判不出是被谁盖住）。

## 坑（都在代码里处理了）

1. `layout.bounds` 是 **[x, y, width, height]**，单位是**该 frame 视口的 CSS px**，
   不是屏幕坐标。iframe 里的节点要累加父 frame 的偏移，Retina 还要乘 DPR 才能变成
   截图像素、换算成点击坐标。坐标写错的表现是"稳定地偏移 N 像素"，很容易误判成模型不准。
2. `nodes` 里包含 `display:none` / `visibility:hidden` / `opacity:0` / 尺寸为 0 的节点。
   不按计算样式过滤，就会拿到一堆点不到的"幽灵元素"（这类元素在截图上根本不存在）。
3. shadow DOM 不会单独出现：`captureSnapshot` 给的就是拍平后的树（`pierce` 语义），
   但 Chrome 老版本要 `DOM.getDocument(depth=-1, pierce=True)` 才会带上 shadow 里的节点。
4. 虚拟列表里"滚出视口"的节点仍可能出现在 nodes 里（回收前的残留），
   必须按 bbox 与视口求交判断，否则会点到一个已经不存在的行。
5. `strings` 是全局字符串池，`nodeName` / `attributes` / `text` 全是**下标**，
   直接当字符串用会静默拿到 `"17"` 这种数字。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# tag → 语义角色提示（AX 语义树缺失时至少知道"它大概是个什么东西"）
TAG_ROLE_HINT = {
    "a": "link",
    "button": "button",
    "input": "textbox",
    "textarea": "textbox",
    "select": "combobox",
    "option": "option",
    "label": "label",
    "img": "image",
    "svg": "image",
    "li": "listitem",
    "tr": "row",
    "td": "cell",
    "th": "columnheader",
    "summary": "button",
}

# 这些 input type 是"可输入的"；checkbox/radio/file 归到别的语义
_INPUT_TYPE_ROLE = {
    "checkbox": "checkbox",
    "radio": "radio",
    "range": "slider",
    "file": "button",
    "submit": "button",
    "reset": "button",
    "button": "button",
    "search": "searchbox",
    "email": "textbox",
    "password": "textbox",
    "number": "spinbutton",
}

# 只取这几条计算样式：可见性判断够用，再把整表都拉回来只会让 payload 涨几倍
_NEEDED_STYLES = (
    "display", "visibility", "opacity", "z-index", "pointer-events",
    "overflow-x", "overflow-y", "position",
)

# 可交互的 tag（DOM 里没有 isClickable 标记时的最小兜底）
_INTERACTIVE_TAGS = {
    "a", "button", "input", "textarea", "select", "option", "summary",
    "label", "details", "audio", "video",
}


@dataclass
class LayoutNode:
    """一个 DOM 候选节点（坐标是 viewport CSS px，已含 frame 偏移）。"""

    backend_node_id: int = 0
    tag: str = ""
    role_hint: str = ""
    text: str = ""
    aria_label: str = ""
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    visible: bool = True
    clickable: bool = False
    disabled: bool = False
    z: int = 0
    opacity: float = 1.0
    pointer_events: bool = True
    inside_shadow: bool = False
    frame_id: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)

    # -- 便捷属性 ------------------------------------------------------ #
    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def rect(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    @property
    def label(self) -> str:
        return (self.aria_label or self.text or self.attrs.get("placeholder")
                or self.attrs.get("title") or self.attrs.get("name") or "").strip()

    def __str__(self) -> str:  # 便于日志
        return f"<{self.tag or '?'} #{self.backend_node_id} {self.label[:24]!r} @({self.x:.0f},{self.y:.0f})>"


def _sparse(data: Optional[dict]) -> Dict[int, Any]:
    """CDP 的稀疏数组：{"index": [...], "value": [...]} → {index: value}。

    `RareBooleanData` 只有 index 没有 value（表示"这一项为 true"）。
    """
    if not data:
        return {}
    idx = data.get("index") or []
    val = data.get("value")
    if val is None:
        return {int(i): True for i in idx}
    return {int(i): v for i, v in zip(idx, val)}


def _bounds_of(raw: Any) -> Tuple[float, float, float, float]:
    """把 layout.bounds 的一行转成 (x, y, w, h)。

    CDP 文档说 `bounds` 是 "array of numbers, 4 per node"，但不同版本/绑定
    （Playwright、pychrome、裸 CDP）有的给扁平数组、有的给 [[x,y,w,h], ...]，
    两种都得认——只认一种，遇到另一种就是**静默拿到空 rect**，元素全部变成 0 尺寸。
    """
    if not raw:
        return (0.0, 0.0, 0.0, 0.0)
    if isinstance(raw[0], (int, float)):
        if len(raw) >= 4:
            return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        return (0.0, 0.0, 0.0, 0.0)
    if len(raw[0]) >= 4:
        return tuple(float(v) for v in raw[0][:4])  # type: ignore[return-value]
    return (0.0, 0.0, 0.0, 0.0)


def _attr_map(flat: Sequence[Any], strings: Sequence[str]) -> Dict[str, str]:
    """attributes 是 [nameIdx, valueIdx, nameIdx, valueIdx, ...]。"""
    out: Dict[str, str] = {}
    if not flat:
        return out
    items = flat[0] if isinstance(flat[0], (list, tuple)) else flat
    for i in range(0, len(items) - 1, 2):
        k = _s(strings, items[i])
        v = _s(strings, items[i + 1])
        if k:
            out[k] = v
    return out


def _s(strings: Sequence[str], idx: Any) -> str:
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return ""
    return strings[i] if 0 <= i < len(strings) else ""


def _style_map(flat: Any, strings: Sequence[str]) -> Dict[str, str]:
    """layout.styles 每项是 [nameIdx, valueIdx, nameIdx, valueIdx, ...]。"""
    out: Dict[str, str] = {}
    if not flat:
        return out
    for i in range(0, len(flat) - 1, 2):
        k = _s(strings, flat[i]).strip().lower()
        if k in _NEEDED_STYLES:
            out[k] = _s(strings, flat[i + 1]).strip().lower()
    return out


def parse_snapshot(
    payload: dict,
    *,
    viewport: Optional[Tuple[float, float]] = None,
    frame_offsets: Optional[Dict[str, Tuple[float, float]]] = None,
    max_nodes: int = 4000,
) -> List[LayoutNode]:
    """把 `DOMSnapshot.captureSnapshot` 的返回解析成候选节点列表。

    viewport: 视口大小（CSS px）。给了就按"是否与视口相交"过滤虚拟列表的残留节点。
    frame_offsets: frameId → (dx, dy)，iframe 内文档坐标要加上父层的偏移。
                   不传就假设全部在主 frame 里（单 frame 场景最常见）。
    """
    strings = payload.get("strings") or []
    docs = payload.get("documents") or []
    offsets = frame_offsets or {}
    out: List[LayoutNode] = []

    for doc in docs:
        fid = doc.get("frameId") or ""
        dx, dy = offsets.get(fid, (0.0, 0.0))
        nodes = doc.get("nodes") or {}
        layout = doc.get("layout") or {}
        if not nodes or not layout:
            continue

        clickable = _sparse(nodes.get("isClickable"))
        # shadowRootType 里出现的索引是「shadow host」，它的**后代**才是 shadow 内的节点。
        # 早期把整个文档标成 inside_shadow，等于所有节点都带 shadow 标记（信息全废）。
        shadow_hosts = set(_sparse(nodes.get("shadowRootType")).keys())
        parent_idx = list(nodes.get("parentIndex") or [])

        # 默认参数把当前这一轮的 parent_idx / shadow_hosts 绑死在函数对象上：
        # 不加的话闭包捕获的是循环变量，下一轮改了值就会串（B023）。
        def _in_shadow(
            ni: int, parent_idx=parent_idx, shadow_hosts=shadow_hosts
        ) -> bool:
            cur, hops = ni, 0
            while 0 <= cur < len(parent_idx) and hops < 32:
                p = int(parent_idx[cur])
                if p < 0:
                    break
                if p in shadow_hosts:
                    return True
                cur, hops = p, hops + 1
            return False

        node_index = layout.get("nodeIndex") or []
        bounds = layout.get("bounds") or []
        styles = layout.get("styles") or []
        texts = _sparse(layout.get("text"))

        for k, ni in enumerate(node_index):
            if len(out) >= max_nodes:
                break
            ni = int(ni)
            tag = _s(strings, _nth(nodes.get("nodeName"), ni)).lower()
            if tag in ("#text", "#comment", "#document", "#document-fragment", "", "#other"):
                continue

            attrs = _attr_map(_nth(nodes.get("attributes"), ni) or [], strings)
            st = _style_map(styles[k] if k < len(styles) else None, strings)
            x, y, w, h = _bounds_of(bounds[k] if k < len(bounds) else None)
            x += dx
            y += dy

            display = st.get("display", "")
            visibility = st.get("visibility", "")
            try:
                opacity = float(st.get("opacity", "1") or 1)
            except ValueError:
                opacity = 1.0
            try:
                z = int(float(st.get("z-index", "0") or 0))
            except ValueError:
                z = 0
            pe = st.get("pointer-events", "auto") != "none"

            visible = (
                display not in ("none", "contents")
                and visibility not in ("hidden", "collapse")
                and opacity > 0.05
                and w > 0.5 and h > 0.5
            )
            if viewport and visible:
                vw, vh = viewport
                # 完全在视口外 = 虚拟列表滚动后的残留 / 滚动容器外的内容
                if x + w <= 0 or y + h <= 0 or x >= vw or y >= vh:
                    visible = False

            # ⚠️ layout.text 的 value 是**字符串池下标**，不是字符串本身。
            # 直接把下标当文字用，元素名会变成 "42" 这种数字，而且不会报错 ——
            # 表现就是"名字全错、对齐率看着还行"，极难查。
            text = _s(strings, texts.get(ni)).strip()
            if not text:
                text = _s(strings, _nth(nodes.get("nodeValue"), ni)).strip()

            node = LayoutNode(
                backend_node_id=int(_nth(nodes.get("backendNodeId"), ni) or 0),
                tag=tag,
                role_hint=_role_hint(tag, attrs),
                text=text[:120],
                aria_label=(attrs.get("aria-label") or attrs.get("alt") or "")[:120],
                x=x, y=y, w=w, h=h,
                visible=visible,
                clickable=bool(clickable.get(ni)) or _looks_clickable(tag, attrs),
                disabled=("disabled" in attrs) or attrs.get("aria-disabled") == "true",
                z=z,
                opacity=opacity,
                pointer_events=pe,
                inside_shadow=_in_shadow(ni),
                frame_id=fid,
                attrs=attrs,
            )
            out.append(node)
    return out


def _nth(arr: Optional[Sequence[Any]], i: int) -> Any:
    if not arr or i < 0 or i >= len(arr):
        return None
    return arr[i]


def _role_hint(tag: str, attrs: Dict[str, str]) -> str:
    explicit = (attrs.get("role") or "").strip().lower()
    if explicit:
        return explicit
    if tag == "input":
        return _INPUT_TYPE_ROLE.get((attrs.get("type") or "text").lower(), "textbox")
    return TAG_ROLE_HINT.get(tag, "")


def _looks_clickable(tag: str, attrs: Dict[str, str]) -> bool:
    """isClickable 没标时的兜底：语义上"点了会有反应"的常见情况。"""
    if tag in _INTERACTIVE_TAGS:
        return True
    if attrs.get("role") in ("button", "link", "tab", "menuitem", "checkbox", "radio", "option"):
        return True
    if attrs.get("onclick") or attrs.get("tabindex") is not None:
        return True
    return False


def candidates(nodes: Iterable[LayoutNode]) -> List[LayoutNode]:
    """挑出"值得标记/可点"的节点。

    规则：可见 + 有尺寸 + 可交互（浏览器标记 或 语义可点）+ 没被禁用。
    禁用元素仍然保留（模型需要知道它存在且当前不可点），由上层决定怎么展示。
    """
    out = []
    for n in nodes:
        if not n.visible or n.w < 6 or n.h < 6:
            continue
        if not n.clickable:
            continue
        out.append(n)
    return out
