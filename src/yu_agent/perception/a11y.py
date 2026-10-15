"""辅助功能树（a11y）：通过 macOS AXUIElement API 枚举可交互 UI 元素。

用于 SOM（Set-of-Mark）标记：拿到每个元素的位置/大小/角色/标题，
在截图上画编号框，模型输出编号即可精确定位，避免 VLM 直接估算像素坐标的偏差。

⚠️ 曾经踩过的坑（都已修，改这个文件时别退回去）：
1. 早期对容器角色（AXGroup / AXScrollArea / AXSplitGroup）直接 `continue`，
   等于把整棵子树跳过 —— 浏览器网页内容挂在 AXGroup / AXScrollArea 下面，
   于是网页里的输入框永远拿不到标记。现在容器只是「不打标签」，仍然向下遍历。
2. 早期只枚举「前台应用」一个 app。前台恰好是 Yu-Agent 自己时（自身 pid 在排除
   列表里），就只剩程序坞可选，模型只好一直点屏幕底部图标。
   现在改成：枚举屏幕上**所有可见应用**（按前后顺序），自身窗口只当作遮挡物。
3. 程序坞早期被硬编码截断到 14 个图标，用户程序坞有 26 个时右半截完全没有标记。
   现在上限可配置（默认 30），并且用递归遍历替代「Dock->AXList->children」的脆弱路径。
4. 早期会把「覆盖整屏/整条菜单栏」的巨型容器也标成可点元素（截图上出现一个巨大的
   红框，白占编号还误导模型）。现在会过滤掉超大元素与菜单栏上的状态区容器。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class UIElement:
    """一个可交互 UI 元素。"""

    id: int
    role: str = ""            # 角色：AXButton / AXLink / AXTextField ...
    title: str = ""           # 标题 / 名称
    description: str = ""     # 描述
    x: float = 0.0            # 屏幕坐标（逻辑像素，左上角）
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    app: str = ""             # 所属应用名（多应用枚举时用于提示模型）
    kind: str = "app"         # "app" = 应用窗口内元素；"dock" = 程序坞图标

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def label(self) -> str:
        """给模型看的描述，例如：`终端 | 菜单栏项 | 显示` / `程序坞图标 | 访达`。"""
        parts: List[str] = []
        if self.kind == "app" and self.app:
            parts.append(self.app)
        parts.append(_ROLE_ZH.get(self.role, self.role))
        if self.title:
            parts.append(self.title)
        if self.description and self.description != self.title:
            parts.append(self.description)
        return " | ".join(p for p in parts if p)


# 可交互角色：这些才是用户会点击的东西
_INTERACTIVE_ROLES = {
    # 输入类
    "AXTextField", "AXTextArea", "AXSearchField", "AXSecureTextField",
    "AXComboBox", "AXPopUpButton", "AXMenuButton",
    # 按钮类
    "AXButton", "AXToolbarButton", "AXRadioButton", "AXCheckBox",
    "AXSwitch", "AXToggle", "AXSegmentedControl", "AXDisclosureTriangle",
    "AXIncrementor", "AXStepper", "AXColorWell",
    # 列表/表格/标签页
    "AXCell", "AXRow", "AXTabGroup", "AXMenuItem", "AXMenuBarItem",
    # 链接 / 图片 / 文字（网页内容里大量是这些）
    "AXLink", "AXImage", "AXStaticText",
}

# 容器/装饰角色：它们本身不点，但**必须继续往下遍历**（子元素才是真正的控件）
_CONTAINER_ROLES = {
    "AXGroup", "AXScrollArea", "AXSplitGroup", "AXLayoutArea", "AXLayoutItem",
    "AXScrollBar", "AXSeparator", "AXProgressIndicator", "AXList", "AXOutline",
    "AXTable", "AXBrowser", "AXWebArea", "AXSheet", "AXDrawer", "AXWindow",
    "AXToolbar", "AXMenuBar", "AXMenu", "AXUnknown", "AXMatte", "AXValueIndicator",
    "AXHelpTag", "AXRuler", "AXGrowArea", "AXPopover", "AXBusyIndicator",
    "AXGrid", "AXColumn", "AXRadioGroup", "AXListItem",
}

# 优先级：数值越小越先保留。输入类排最前（用户任务十有八九要输入）
_ROLE_PRIORITY: Dict[str, int] = {
    "AXTextField": 0, "AXTextArea": 0, "AXSearchField": 0,
    "AXSecureTextField": 0, "AXComboBox": 0,
    "AXButton": 1, "AXPopUpButton": 1, "AXMenuBarItem": 1, "AXMenuItem": 1,
    "AXMenuButton": 1, "AXToolbarButton": 1, "AXRadioButton": 1, "AXCheckBox": 1,
    "AXSwitch": 1, "AXToggle": 1, "AXSegmentedControl": 1,
    "AXIncrementor": 2, "AXStepper": 2, "AXDisclosureTriangle": 2, "AXColorWell": 2,
    "AXLink": 3,
    "AXCell": 4, "AXRow": 4, "AXTabGroup": 4,
    "AXImage": 5, "AXStaticText": 6,
}

# 角色中文名（写进给模型的编号表，让模型更容易理解）
_ROLE_ZH: Dict[str, str] = {
    "AXButton": "按钮", "AXLink": "链接", "AXMenuItem": "菜单项",
    "AXRadioButton": "单选", "AXCheckBox": "复选框", "AXSwitch": "开关",
    "AXToggle": "开关", "AXSegmentedControl": "分段控件",
    "AXTextField": "输入框", "AXTextArea": "文本域", "AXSearchField": "搜索框",
    "AXSecureTextField": "密码框", "AXSlider": "滑块", "AXComboBox": "下拉框",
    "AXPopUpButton": "弹出菜单", "AXMenuButton": "菜单按钮",
    "AXTabGroup": "标签页", "AXImage": "图片",
    "AXCell": "单元格", "AXRow": "行", "AXStaticText": "文字",
    "AXMenuBarItem": "菜单栏项", "AXToolbarButton": "工具栏按钮",
    "AXIncrementor": "步进器", "AXStepper": "步进器", "AXColorWell": "取色器",
    "AXDisclosureTriangle": "展开三角",
    "AXDockItem": "程序坞图标", "程序坞图标": "程序坞图标",
}

# 程序坞图标的 subrole -> 中文（图标本身没有标题时用它兜底）
_DOCK_SUBROLE_ZH: Dict[str, str] = {
    "AXApplicationDockItem": "应用图标",
    "AXFolderDockItem": "文件夹",
    "AXStackDockItem": "堆栈",
    "AXTrashDockItem": "废纸篓",
    "AXMinimizedWindowDockItem": "最小化窗口",
    "AXSeparatorDockItem": "",
}

# 系统进程：跳过，它们没有可交互元素
_SYSTEM_PROCESSES = {
    "loginwindow", "Window Server", "Dock", "SystemUIServer",
    "ControlCenter", "NotificationCenter", "Spotlight", "WindowManager",
    "universalaccessd", "UserNotificationCenter", "ControlStrip",
}

# 需要排除的进程 pid（由 GUI 设置为本进程，避免 agent 点击自己的界面）
# 注意：自身窗口仍然会作为「遮挡物」参与遮挡计算，只是不作为枚举目标。
_EXCLUDED_PIDS: set = set()

# 上一次枚举的诊断信息（供日志/排障使用）
last_stats: Dict[str, Any] = {}

# 单个元素面积超过屏幕的这个比例 → 视为背景/容器，不标记
_MAX_AREA_FRACTION = 0.85
# 菜单栏高度估算（逻辑像素）：这条带子里超宽又没标题的元素是状态区容器，过滤掉
_MENUBAR_H = 32
_MENUBAR_MAX_ITEM_W = 260
# 最多枚举几个应用（太多会淹没模型，也会拖慢速度）
_MAX_APPS = 6
# 单个元素的遍历深度
_MAX_DEPTH = 18


def set_excluded_pids(pids) -> None:
    """设置要排除的进程 pid（例如 Yu-Agent 自身，避免点击自己的窗口）。"""
    _EXCLUDED_PIDS.clear()
    for p in pids:
        try:
            _EXCLUDED_PIDS.add(int(p))
        except (TypeError, ValueError):
            pass


# ---------------------------------------------------------------------- #
# AX 基础读写
# ---------------------------------------------------------------------- #
def _ax(el, attr: str):
    """读取 AX 属性，返回 (值, 是否成功)。"""
    import ApplicationServices as AX

    err, value = AX.AXUIElementCopyAttributeValue(el, attr, None)
    if err != 0:
        return None, False
    return value, True


def _ax_str(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _role_of(el) -> str:
    v, ok = _ax(el, "AXRole")
    return _ax_str(v) if ok else ""


def _subrole_of(el) -> str:
    v, ok = _ax(el, "AXSubrole")
    return _ax_str(v) if ok else ""


def _title_of(el) -> str:
    """尽量拿到能描述这个元素用途的文字。"""
    for attr in ("AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue", "AXHelp"):
        v, ok = _ax(el, attr)
        if ok and v is not None and str(v).strip():
            s = str(v).strip()
            if s and s.lower() not in ("axvalue", "axdockey"):
                return s[:80]
    return ""


def _pos_size_of(el) -> Tuple[float, float, float, float]:
    """读取元素位置和大小（逻辑像素）。"""
    import ApplicationServices as AX

    pos_v, ok1 = _ax(el, "AXPosition")
    size_v, ok2 = _ax(el, "AXSize")
    if not (ok1 and ok2):
        return 0.0, 0.0, 0.0, 0.0
    try:
        # pyobjc: AXValueGetValue(value, type, None) -> (ok, CGPoint/CGSize)
        ok, pt = AX.AXValueGetValue(pos_v, AX.kAXValueTypeCGPoint, None)
        if not ok:
            return 0.0, 0.0, 0.0, 0.0
        ok2, sz = AX.AXValueGetValue(size_v, AX.kAXValueTypeCGSize, None)
        if not ok2:
            return 0.0, 0.0, 0.0, 0.0
        return float(pt.x), float(pt.y), float(sz.width), float(sz.height)
    except Exception:
        return 0.0, 0.0, 0.0, 0.0


def _children_of(el) -> list:
    v, ok = _ax(el, "AXChildren")
    if not ok or v is None:
        return []
    try:
        return list(v)
    except Exception:
        return []


# ---------------------------------------------------------------------- #
# 屏幕 / 窗口信息
# ---------------------------------------------------------------------- #
def _screen_size() -> Tuple[float, float]:
    """主屏逻辑尺寸（用于过滤超大容器）。"""
    try:
        import Quartz

        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return float(b.size.width), float(b.size.height)
    except Exception:
        return 0.0, 0.0


def _on_screen_windows() -> List[Dict[str, Any]]:
    """按「从前到后」返回屏幕上 layer=0 的窗口信息（含 pid / 应用名 / 边界）。

    这个顺序由系统给出，正好可以用来判断遮挡：排在前面的窗口会盖住后面的。
    """
    try:
        import Quartz

        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    for w in info:
        try:
            if int(w.get("kCGWindowLayer", 99)) != 0:
                continue
            if float(w.get("kCGWindowAlpha", 1.0) or 0.0) <= 0.02:
                continue
            b = w.get("kCGWindowBounds")
            if not b:
                continue
            bw, bh = float(b.get("Width", 0)), float(b.get("Height", 0))
            if bw < 40 or bh < 40:      # 忽略极小辅助窗口
                continue
            pid = w.get("kCGWindowOwnerPID")
            if pid is None:
                continue
            out.append(
                {
                    "pid": int(pid),
                    "name": str(w.get("kCGWindowOwnerName") or ""),
                    "x": float(b["X"]),
                    "y": float(b["Y"]),
                    "w": bw,
                    "h": bh,
                }
            )
        except Exception:
            continue
    return out


def _rect_of(win: Dict[str, Any]) -> Tuple[float, float, float, float]:
    return win["x"], win["y"], win["w"], win["h"]


def _point_in(cx: float, cy: float, rect: Tuple[float, float, float, float]) -> bool:
    x, y, w, h = rect
    return x <= cx <= x + w and y <= cy <= y + h


def _ordered_app_pids(
    windows: List[Dict[str, Any]], max_apps: int = _MAX_APPS
) -> List[int]:
    """从窗口列表里取出去重后的应用 pid，保持前后顺序。"""
    order: List[int] = []
    for w in windows:
        pid = w["pid"]
        if pid in _EXCLUDED_PIDS:
            continue
        if w["name"] in _SYSTEM_PROCESSES:
            continue
        if pid not in order and len(order) < max_apps:
            order.append(pid)
    return order


def _app_name(pid: Optional[int]) -> str:
    if pid is None:
        return ""
    try:
        from AppKit import NSRunningApplication

        r = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        return (r.localizedName() if r else "") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------- #
# 元素遍历
# ---------------------------------------------------------------------- #
def _useful(
    role: str,
    title: str,
    x: float,
    y: float,
    w: float,
    h: float,
    screen: Optional[Tuple[float, float]] = None,
) -> bool:
    """判断一个元素是否值得标记（过滤噪声与容器）。"""
    if w <= 0 or h <= 0:
        return False
    # 1~2 像素宽的分隔条之类的元素点了也不可靠，标出来只会浪费编号
    if w < 3 or h < 3:
        return False
    if screen and screen[0] and screen[1]:
        # 覆盖绝大部分屏幕的巨型元素 = 背景/容器，标出来只会误导模型
        if (w * h) >= _MAX_AREA_FRACTION * screen[0] * screen[1]:
            return False
        # 菜单栏带里超宽且无标题的元素 = 状态区容器（不是一个可点的控件）
        if y + h <= _MENUBAR_H and w > _MENUBAR_MAX_ITEM_W and not title:
            return False
    return True


def _walk(
    el,
    out: List[UIElement],
    depth: int,
    limit: int,
    screen: Optional[Tuple[float, float]] = None,
    app_name: str = "",
) -> None:
    """递归遍历 AX 树，收集可交互元素。

    注意：容器角色只是不打标签，**仍然继续向下遍历**。
    """
    if depth > _MAX_DEPTH or len(out) >= limit:
        return

    for child in _children_of(el):
        if len(out) >= limit:
            return
        role = _role_of(child)
        if role in _INTERACTIVE_ROLES:
            x, y, w, h = _pos_size_of(child)
            title = _title_of(child)
            if _useful(role, title, x, y, w, h, screen):
                out.append(
                    UIElement(
                        id=len(out),
                        role=role,
                        title=title,
                        x=x, y=y, w=w, h=h,
                        app=app_name,
                        kind="app",
                    )
                )
        _walk(child, out, depth + 1, limit, screen, app_name)


def get_frontmost_app_elements(
    max_elements: int = 120, max_depth: int = _MAX_DEPTH
) -> Tuple[List[UIElement], Optional[int]]:
    """兼容旧接口：只枚举「当前该操作的应用」的元素。返回 (元素列表, 应用 pid)。"""
    import ApplicationServices as AX

    windows = _on_screen_windows()
    order = _ordered_app_pids(windows)
    if not order:
        pid = _fallback_frontmost_pid()
        order = [pid] if pid else []
    if not order:
        return [], None

    pid = order[0]
    app = AX.AXUIElementCreateApplication(pid)
    out: List[UIElement] = []
    _walk(app, out, 0, max(max_elements * 6, 400), _screen_size(), _app_name(pid))
    return out, pid


def _fallback_frontmost_pid() -> Optional[int]:
    """窗口列表拿不到时的兜底：NSWorkspace 前台应用。"""
    try:
        from AppKit import NSWorkspace

        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = int(front.processIdentifier())
        if pid not in _EXCLUDED_PIDS:
            return pid
    except Exception:
        pass
    try:
        import ApplicationServices as AX

        sys_el = AX.AXUIElementCreateSystemWide()
        app, ok = _ax(sys_el, "AXFocusedApplication")
        if ok and app is not None:
            pid, ok2 = _ax(app, "AXPID")
            if ok2:
                return int(pid)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------- #
# 程序坞
# ---------------------------------------------------------------------- #
def _dock_pid() -> Optional[int]:
    """获取 Dock 进程的 pid。"""
    import subprocess

    try:
        r = subprocess.run(
            ["pgrep", "-x", "Dock"], capture_output=True, text=True, timeout=2
        )
        pid = r.stdout.strip().splitlines()
        return int(pid[0]) if pid else None
    except Exception:
        return None


def _dock_item_title(el, role: str) -> str:
    """程序坞图标的名字：先试常规属性，再用 subrole 兜底。"""
    t = _title_of(el)
    if t and t != role:
        return t
    sub = _subrole_of(el)
    zh = _DOCK_SUBROLE_ZH.get(sub, "")
    if zh:
        return zh
    return t


def get_dock_elements(max_elements: int = 30) -> List[UIElement]:
    """获取程序坞里的图标元素（含右半部分；上限可配置）。

    程序坞的 AX 结构大致是 Dock(pid) -> AXList -> 各 AXDockItem，
    但不同系统版本/分组方式会有差异，所以这里用递归遍历，遇到图标就收，
    不再依赖「children[0] 一定是 AXList」这条脆弱路径。
    """
    import ApplicationServices as AX

    out: List[UIElement] = []
    pid = _dock_pid()
    if pid is None:
        return out

    try:
        dock = AX.AXUIElementCreateApplication(pid)
    except Exception:
        return out

    def visit(el, depth: int) -> None:
        if depth > 6 or len(out) >= max_elements:
            return
        for child in _children_of(el):
            if len(out) >= max_elements:
                return
            role = _role_of(child)
            if role == "AXDockItem":
                x, y, w, h = _pos_size_of(child)
                if w <= 0 or h <= 0:
                    continue
                sub = _subrole_of(child)
                if sub == "AXSeparatorDockItem":     # 分隔符不是可点图标
                    continue
                title = _dock_item_title(child, role)
                out.append(
                    UIElement(
                        id=len(out),
                        role="程序坞图标",
                        title=title,
                        x=x, y=y, w=w, h=h,
                        app="程序坞",
                        kind="dock",
                    )
                )
            else:
                visit(child, depth + 1)

    visit(dock, 0)
    out.sort(key=lambda el: el.x)
    return out


# ---------------------------------------------------------------------- #
# 汇总
# ---------------------------------------------------------------------- #
def _dedupe(elements: List[UIElement]) -> List[UIElement]:
    """去掉完全重复的元素（AX 树里常见重复包装）。"""
    seen = set()
    out: List[UIElement] = []
    for el in elements:
        key = (el.role, round(el.x), round(el.y), round(el.w), round(el.h), el.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(el)
    return out


def _app_elements(
    pid: int,
    app_name: str,
    occluders: List[Tuple[float, float, float, float]],
    limit: int,
    screen: Tuple[float, float],
    is_frontmost: bool = False,
) -> List[UIElement]:
    """枚举单个应用的元素，并丢掉被前面窗口盖住的（点了也点不到）。

    is_frontmost=False 时会丢掉该应用的「菜单栏项」：macOS 的菜单栏只有前台
    应用的才真实显示，其它应用虽然也有同位置同尺寸的菜单栏元素，但那只是重影，
    标出来会让模型点到前台应用的菜单上。
    """
    import ApplicationServices as AX

    app = AX.AXUIElementCreateApplication(pid)
    raw: List[UIElement] = []
    _walk(app, raw, 0, max(limit * 6, 400), screen, app_name)
    raw = _dedupe(raw)

    kept: List[UIElement] = []
    for el in raw:
        if not is_frontmost and el.role == "AXMenuBarItem":
            continue
        if occluders and any(_point_in(el.cx, el.cy, r) for r in occluders):
            continue        # 被更靠前的窗口遮住 → 不要标记，免得模型点了个点不到的位置
        kept.append(el)

    # 按角色优先级裁剪：输入框/按钮优先于装饰性文字
    kept.sort(key=lambda e: (_ROLE_PRIORITY.get(e.role, 9), e.w * e.h))
    kept = kept[:limit]
    kept.sort(key=lambda e: (round(e.y / 12), e.x))
    return kept


def get_all_interactive_elements(
    max_elements: int = 120,
    auto_calibrate: bool = True,
    dock_max: int = 30,
    max_apps: int = _MAX_APPS,
) -> List[UIElement]:
    """汇总所有可交互元素（屏幕上各应用 + 程序坞），坐标已校准到屏幕点击坐标系。

    策略：
    - 枚举屏幕上**所有可见应用**（按前后顺序），前台应用拿大头预算；
      被更靠前窗口完全遮住的元素会被丢弃（标记了也点不到）。
    - 程序坞单独一批，放在编号表最后（用于"打开/切换某个应用"）。
    - 每个应用内按角色优先级裁剪：输入框/按钮优先于纯装饰文字。
    - 所有元素都带上是哪个应用的，模型能分清"这个按钮属于哪个窗口"。
    """
    windows = _on_screen_windows()
    screen = _screen_size()

    # 自身窗口只作为遮挡物参与计算（GUI 运行时会把主窗口让开）
    own_rects = [_rect_of(w) for w in windows if w["pid"] in _EXCLUDED_PIDS]
    order = _ordered_app_pids(windows, max_apps=max_apps)

    if not order:
        pid = _fallback_frontmost_pid()
        if pid:
            order = [pid]

    dock = get_dock_elements(max(4, dock_max))
    app_budget = max(1, max_elements - len(dock))

    # 预算：前台应用拿一半，其余应用平分另一半
    n = len(order)
    if n <= 1:
        caps = [app_budget] if n else []
    else:
        front_cap = max(15, int(app_budget * 0.5))
        others = max(8, (app_budget - front_cap) // (n - 1))
        caps = [front_cap] + [others] * (n - 1)

    elements: List[UIElement] = []
    occluders = list(own_rects)
    per_app: List[Dict[str, Any]] = []

    for i, pid in enumerate(order):
        name = _app_name(pid) or f"pid{pid}"
        cap = min(caps[i], max(1, app_budget - len(elements)))
        kept = _app_elements(
            pid, name, occluders, cap, screen, is_frontmost=(i == 0)
        )
        per_app.append({"pid": pid, "name": name, "elements": len(kept)})
        elements.extend(kept)
        # 这个应用的所有窗口都会挡住它后面的应用
        occluders.extend(_rect_of(w) for w in windows if w["pid"] == pid)
        if len(elements) >= app_budget:
            break

    elements.extend(dock)

    # 坐标校准：把 AX 坐标平移到与鼠标点击一致的坐标系
    calib = (0.0, 0.0)
    if auto_calibrate and elements:
        dx, dy = _calibrate()
        if dx or dy:
            calib = (dx, dy)
            for el in elements:
                el.x += dx
                el.y += dy

    for i, el in enumerate(elements):
        el.id = i

    last_stats.clear()
    last_stats.update(
        {
            "app_pid": order[0] if order else None,
            "app_name": per_app[0]["name"] if per_app else "",
            "app_elements": per_app[0]["elements"] if per_app else 0,
            "apps": per_app,
            "app_count": len(per_app),
            "dock_elements": len(dock),
            "total": len(elements),
            "calibration": calib,
        }
    )
    return elements


def _calibrate() -> Tuple[float, float]:
    """校准 AX 坐标系 -> 屏幕点击坐标系的偏移。

    原理：用 Quartz 拿某个窗口的边界（与鼠标点击同一坐标系），
    再拿**同一个 pid 的同一个窗口**的 AX 坐标，两者之差即偏移量。
    拿不到或偏移不合理时返回 (0, 0)（大多数机器两者本就一致）。

    ⚠️ 早期直接取 AXWindows[0] 与 Quartz 里第一个 layer=0 的窗口比较，
    两者可能根本不是同一个窗口，会算出一个几百像素的假偏移，
    于是所有标记整体平移，点击必然错位。现在按窗口尺寸配对 + 合理性检查。
    """
    try:
        import Quartz

        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        import ApplicationServices as AX

        for w in info:
            if w.get("kCGWindowLayer", 99) != 0 or not w.get("kCGWindowBounds"):
                continue
            pid = w.get("kCGWindowOwnerPID")
            if pid is None or int(pid) in _EXCLUDED_PIDS:
                continue
            qb = w["kCGWindowBounds"]
            qx, qy = float(qb["X"]), float(qb["Y"])
            qw, qh = float(qb.get("Width", 0)), float(qb.get("Height", 0))

            app = AX.AXUIElementCreateApplication(int(pid))
            windows, ok = _ax(app, "AXWindows")
            if not ok or not windows:
                continue
            best = None
            best_err = None
            for win in windows:
                ax_x, ax_y, ax_w, ax_h = _pos_size_of(win)
                if ax_w <= 0 or ax_h <= 0:
                    continue
                err = abs(ax_w - qw) + abs(ax_h - qh)
                if best_err is None or err < best_err:
                    best_err = err
                    best = (ax_x, ax_y)
            if best is None:
                continue
            dx, dy = best[0] - qx, best[1] - qy
            if abs(dx) > 80 or abs(dy) > 80:
                return 0.0, 0.0
            return dx, dy
    except Exception:
        pass
    return 0.0, 0.0


def get_a11y_tree(max_depth: int = 12, max_elements: int = 400) -> list:
    """兼容旧接口：返回元素描述字符串列表。"""
    elements = get_all_interactive_elements(max_elements)
    return [el.label() for el in elements]
