"""辅助功能树（a11y）：通过 macOS AXUIElement API 枚举可交互 UI 元素。

用于 SOM（Set-of-Mark）标记：拿到每个元素的位置/大小/角色/标题，
在截图上画编号框，模型输出编号即可精确定位，避免 VLM 直接估算像素坐标的偏差。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class UIElement:
    """一个可交互 UI 元素。"""

    id: int
    role: str = ""            # 角色：AXButton / AXLink / AXMenuItem / AXImage ...
    title: str = ""           # 标题 / 名称
    description: str = ""     # 描述
    x: float = 0.0            # 屏幕坐标（逻辑像素，左上角）
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def label(self) -> str:
        parts = [self.role]
        if self.title:
            parts.append(self.title)
        if self.description and self.description != self.title:
            parts.append(self.description)
        return " | ".join(p for p in parts if p)


# 可交互角色：这些才是用户会点击的东西
_INTERACTIVE_ROLES = {
    "AXButton", "AXLink", "AXMenuItem", "AXRadioButton", "AXCheckBox",
    "AXTextField", "AXTextArea", "AXSlider", "AXComboBox", "AXPopUpButton",
    "AXTabGroup", "AXImage", "AXCell", "AXRow", "AXStaticText",
    "AXMenuBarItem", "AXToolbarButton", "AXMenuItem",
}

# 排除的噪声角色（滚动条、分隔符等不可点击/无意义）
_SKIP_ROLES = {
    "AXScrollBar", "AXSeparator", "AXGroup", "AXScrollArea", "AXSplitGroup",
    "AXProgressIndicator", "AXLayoutArea", "AXLayoutItem",
}


def _ax(application, attr: str):
    """读取 AX 属性，返回 (值, 是否成功)。"""
    import ApplicationServices as AX

    err, value = AX.AXUIElementCopyAttributeValue(application, attr, None)
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


def _title_of(el) -> str:
    for attr in ("AXTitle", "AXValue", "AXDescription", "AXHelp"):
        v, ok = _ax(el, attr)
        if ok and v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _pos_size_of(el) -> tuple[float, float, float, float]:
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


def _walk(el, out: List[UIElement], depth: int, max_depth: int, limit: int):
    """递归遍历 AX 树，收集可交互元素。"""
    if depth > max_depth or len(out) >= limit:
        return
    children, ok = _ax(el, "AXChildren")
    if not ok or children is None:
        return
    import ApplicationServices as AX

    for child in children:
        if len(out) >= limit:
            return
        role = _role_of(child)
        if role in _SKIP_ROLES:
            continue
        if role in _INTERACTIVE_ROLES:
            x, y, w, h = _pos_size_of(child)
            if w > 0 and h > 0:
                out.append(
                    UIElement(
                        id=len(out),
                        role=role,
                        title=_title_of(child),
                        x=x, y=y, w=w, h=h,
                    )
                )
        # 继续深入（按钮里可能没有子元素，但容器里可能有）
        _walk(child, out, depth + 1, max_depth, limit)


# 系统进程：跳过，它们没有可交互元素
_SYSTEM_PROCESSES = {
    "loginwindow", "Window Server", "Dock", "Finder", "SystemUIServer",
    "ControlCenter", "NotificationCenter",
}

# 需要排除的进程 pid（由 GUI 设置为本进程，避免 agent 点击自己的界面）
_EXCLUDED_PIDS: set = set()


def set_excluded_pids(pids) -> None:
    """设置要排除的进程 pid（例如 Yu-Agent 自身，避免点击自己的窗口）。"""
    _EXCLUDED_PIDS.clear()
    for p in pids:
        try:
            _EXCLUDED_PIDS.add(int(p))
        except (TypeError, ValueError):
            pass


def _pid_of_frontmost_app() -> Optional[int]:
    """获取当前前台应用的 pid（跳过系统进程）。"""
    # 首选 NSWorkspace（更可靠）
    try:
        from AppKit import NSWorkspace

        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        name = front.localizedName() or ""
        if name and name not in _SYSTEM_PROCESSES:
            return int(front.processIdentifier())
    except Exception:
        pass

    # 兜底：AXFocusedApplication
    import ApplicationServices as AX

    sys_el = AX.AXUIElementCreateSystemWide()
    app, ok = _ax(sys_el, "AXFocusedApplication")
    if ok and app is not None:
        pid, ok2 = _ax(app, "AXPID")
        if ok2:
            return int(pid)
    return None


def _dock_pid() -> Optional[int]:
    """获取 Dock 进程的 pid。"""
    import subprocess

    try:
        r = subprocess.run(["pgrep", "-x", "Dock"], capture_output=True, text=True, timeout=2)
        pid = r.stdout.strip()
        return int(pid) if pid else None
    except Exception:
        return None


def get_dock_elements() -> List[UIElement]:
    """获取 Dock 里的应用图标元素。

    Dock 的 AX 结构：Dock(pid) -> AXChildren[0] = AXList -> AXChildren = 各图标项。
    """
    import ApplicationServices as AX

    out: List[UIElement] = []
    pid = _dock_pid()
    if pid is None:
        return out

    try:
        dock = AX.AXUIElementCreateApplication(pid)
        children, ok = _ax(dock, "AXChildren")
        if not ok or not children:
            return out
        # 第一个 child 通常是 AXList
        axlist = children[0]
        items, ok2 = _ax(axlist, "AXChildren")
        if not ok2 or not items:
            return out

        for item in items:
            role = _role_of(item)
            title = _title_of(item)
            x, y, w, h = _pos_size_of(item)
            if w <= 0 or h <= 0:
                continue
            # Dock 图标通常 role 是 AXApplication 或 AXButton 或 AXImage
            out.append(
                UIElement(
                    id=len(out),
                    role=role or "AXDockItem",
                    title=title,
                    x=x, y=y, w=w, h=h,
                )
            )
    except Exception:
        pass
    return out


def get_frontmost_app_elements(max_elements: int = 120, max_depth: int = 14) -> List[UIElement]:
    """获取前台应用的可交互元素。"""
    import ApplicationServices as AX

    pid = _pid_of_frontmost_app()
    if pid is None or pid in _EXCLUDED_PIDS:
        return []
    app = AX.AXUIElementCreateApplication(pid)
    out: List[UIElement] = []
    _walk(app, out, 0, max_depth, max_elements)
    return out


def _calibrate() -> tuple[float, float]:
    """校准 AX 坐标系 -> 屏幕点击坐标系的偏移。

    原理：用 Quartz 拿前台窗口的边界（与鼠标点击同一坐标系），
    再拿同一窗口的 AX 坐标，两者之差即偏移量。
    拿不到时返回 (0, 0)（大多数机器两者本就一致）。
    """
    try:
        import Quartz

        # 找一个普通层级的窗口（layer 0），取它的 Quartz 边界
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        target = None
        for w in info:
            if w.get("kCGWindowLayer", 99) == 0 and w.get("kCGWindowBounds"):
                target = w
                break
        if target is None:
            return 0.0, 0.0

        qb = target["kCGWindowBounds"]
        qx, qy = float(qb["X"]), float(qb["Y"])
        pid = target.get("kCGWindowOwnerPID")

        # 拿该窗口的 AX 坐标
        import ApplicationServices as AX

        app = AX.AXUIElementCreateApplication(pid)
        windows, ok = _ax(app, "AXWindows")
        if not ok or not windows:
            return 0.0, 0.0
        ax_x, ax_y, _, _ = _pos_size_of(windows[0])
        if ax_x == 0 and ax_y == 0:
            return 0.0, 0.0
        return ax_x - qx, ax_y - qy
    except Exception:
        return 0.0, 0.0


def get_all_interactive_elements(
    max_elements: int = 160, auto_calibrate: bool = True
) -> List[UIElement]:
    """汇总所有可交互元素（前台应用 + Dock），坐标已校准到屏幕点击坐标系。"""
    elements: List[UIElement] = []
    # 前台应用元素优先（任务通常在这里操作）
    elements.extend(get_frontmost_app_elements(max_elements // 2))
    # Dock 元素补充
    dock = get_dock_elements()
    for el in dock:
        if len(elements) >= max_elements:
            break
        elements.append(el)

    # 坐标校准：把 AX 坐标平移到与鼠标点击一致的坐标系
    if auto_calibrate and elements:
        dx, dy = _calibrate()
        if dx or dy:
            for el in elements:
                el.x += dx
                el.y += dy

    # 重新编号
    for i, el in enumerate(elements):
        el.id = i
    return elements


def get_a11y_tree(max_depth: int = 12, max_elements: int = 400) -> list:
    """兼容旧接口：返回元素列表（dict 形式）。"""
    elements = get_all_interactive_elements(max_elements)
    return [el.label() for el in elements]
