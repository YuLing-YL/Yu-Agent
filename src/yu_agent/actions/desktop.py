"""运行前清场：把桌面收干净，减少 SOM 标记的干扰。

用户反馈：屏幕上的应用太多太杂时，可点击元素的识别会被干扰——
编号表里混进一堆背景窗口的按钮，真正的目标反而不显眼。

这里做两层：

**第一层：真的隐藏其它应用（尽力而为）**
逐个调 `NSRunningApplication.hide()`，等价于给每个应用按 ⌘H——**不退出、不关窗**，
窗口和内容都还在，之后点程序坞图标就能原样恢复。

⚠️ 实测限制：从**后台进程**调用时，macOS 会直接返回 NO（连 AX 写权限都正常也拦）。
从 GUI 里（点「发送」的那一瞬间 Yu-Agent 自己是前台应用）通常可以成功。
所以这一层的结果必须如实上报，不能假装成功。

**第二层：感知收窄（一定生效，兜底）**
不管隐藏成功与否，`AgentLoop` 都会把 SOM 标记限制成「当前最前面的应用 + 程序坞」，
背景应用的按钮一个都不标。窗口藏不掉时，靠这一层保证屏幕再乱也不干扰识别。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

from ..common.logging import get_logger

log = get_logger("yu_agent.desktop")

# 这些应用不要隐藏
ALWAYS_KEEP = {
    "Finder", "访达",
    "Dock", "程序坞",
    "Window Server", "WindowManager",
    "SystemUIServer", "ControlCenter", "NotificationCenter",
    "loginwindow", "Spotlight",
}

# NSApplicationActivationPolicyRegular：有 Dock 图标的那种普通应用
_POLICY_REGULAR = 0


@dataclass
class CleanReport:
    """清场结果。"""

    hidden: List[str] = field(default_factory=list)
    already_hidden: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    windows_before: int = 0
    windows_after: int = 0
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.hidden)

    def summary(self) -> str:
        if self.error:
            return f"清空桌面失败（{self.error}），改为只标记当前应用"
        parts: List[str] = []
        if self.hidden:
            names = "、".join(self.hidden[:5]) + ("…" if len(self.hidden) > 5 else "")
            parts.append(f"已隐藏 {len(self.hidden)} 个应用（{names}）")
        else:
            parts.append("系统不允许隐藏其它应用，改为只标记当前应用 + 程序坞")
        if self.kept:
            parts.append(f"保留：{'、'.join(self.kept[:4])}")
        if self.windows_before or self.windows_after:
            parts.append(f"窗口数 {self.windows_before} → {self.windows_after}")
        return "；".join(parts)


def _count_windows() -> int:
    """屏幕上 layer=0 的窗口数量（用于验证清场效果）。"""
    try:
        import Quartz

        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        n = 0
        for w in info:
            if int(w.get("kCGWindowLayer", 99)) != 0:
                continue
            b = w.get("kCGWindowBounds") or {}
            if float(b.get("Width", 0)) < 40 or float(b.get("Height", 0)) < 40:
                continue
            n += 1
        return n
    except Exception:
        return 0


def clean_desktop(
    task: str = "",
    keep: Sequence[str] = (),
    exclude_pids: Iterable[int] = (),
    settle_s: float = 0.35,
) -> CleanReport:
    """隐藏所有「其它普通应用」（⌘H 语义），把桌面收干净。

    task: 任务描述；里面点名的应用会被保留（避免把任务目标自己藏起来）。
    keep: 额外要保留的应用名。
    exclude_pids: 要保留的进程（例如 Yu-Agent 自身）。
    """
    report = CleanReport()
    try:
        from AppKit import NSWorkspace
    except Exception as e:
        report.error = f"缺少 pyobjc/AppKit（{e}）"
        log.warning(report.error)
        return report

    keep_names = set(ALWAYS_KEEP) | {str(k) for k in keep}
    keep_pids = {int(p) for p in exclude_pids}
    task_lower = (task or "").lower()

    report.windows_before = _count_windows()

    try:
        apps = list(NSWorkspace.sharedWorkspace().runningApplications())
    except Exception as e:
        report.error = f"读取运行中的应用失败（{e}）"
        log.warning(report.error)
        return report

    for app in apps:
        try:
            if int(app.activationPolicy()) != _POLICY_REGULAR:
                continue  # 菜单栏小工具，本来就不占屏幕
            name = str(app.localizedName() or "")
            pid = int(app.processIdentifier())
        except Exception:
            continue
        if not name:
            continue
        # 保留：固定白名单 / 调用方指定 / 任务里点名的应用
        if (
            name in keep_names
            or pid in keep_pids
            or (task_lower and name.lower() in task_lower)
        ):
            report.kept.append(name)
            continue
        try:
            if app.isHidden():
                report.already_hidden.append(name)
                continue
        except Exception:
            pass
        try:
            ok = bool(app.hide())
        except Exception as e:
            log.debug("隐藏 %s 失败：%s", name, e)
            ok = False
        (report.hidden if ok else report.failed).append(name)

    if report.hidden:
        time.sleep(settle_s)  # 等窗口收起动画结束再统计

    report.windows_after = _count_windows()
    log.info("清场：%s", report.summary())
    return report
