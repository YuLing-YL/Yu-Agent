"""评测判据：全部从系统状态读，不采信模型自己输出的 done。

为什么不用 state.success
----------------------
`AgentLoop` 的 success 有一部分来自「重复动作 + 屏幕无变化 → 判定已完成」这类兜底，
历史上出现过「点了三次搜索框、什么都没输入，却显示 ✅ 完成」的假成功。
评测如果直接用 success，等于让被测对象自己改考卷，所以这里一律读外部状态：

    frontmost_app  前台应用名        （硬判据：应用真的到前台了）
    window_title   前台窗口标题      （硬判据：导航/搜索结果可见）
    file_exists    文件系统           （最硬：副作用真的发生了）
    element_exists 可交互元素文字    （软判据：部分应用 a11y 读不到，不通过降级为人工）
    screen_text    屏幕上出现的文字  （同上）
    manual         无法自动判定

软判据的行为：自动匹配成功 → pass；匹配不到 → **manual（待人工）** 而不是 fail。
这样不会因为「终端里 echo 的文字 a11y 读不出来」而冤枉 agent。
需要严格时在任务里写 `strict: true`，软判据就退化成硬判据。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 软判据：自动匹配不到时降级为「待人工」
_SOFT_KINDS = {"element_exists", "screen_text"}

_KIND_LABEL = {
    "frontmost_app": "前台应用",
    "not_frontmost_app": "前置：不在前台",
    "window_title": "窗口标题",
    "element_exists": "界面元素",
    "screen_text": "屏幕文字",
    "file_exists": "文件存在",
    "not_file_exists": "前置：文件不存在",
    "manual": "人工确认",
}


@dataclass
class CheckResult:
    """一次判定的结果。

    ok: True 通过 / False 未通过 / None 需要人工确认（判据本身给不出结论）
    """

    ok: Optional[bool]
    detail: str = ""

    @property
    def verdict(self) -> str:
        if self.ok is True:
            return "pass"
        if self.ok is False:
            return "fail"
        return "manual"


def describe(spec: Optional[Dict[str, Any]]) -> str:
    """把判据写成一行人类可读的说明，报告里用。"""
    if not spec:
        return "（无判据）"
    kind = str(spec.get("kind") or "")
    label = _KIND_LABEL.get(kind, kind)
    if kind in ("frontmost_app", "not_frontmost_app"):
        return f"{label} ∈ {'/'.join(spec.get('expect') or [])}"
    if kind == "window_title":
        return f"{label} 含 {'/'.join(spec.get('expect') or [])}"
    if kind in ("element_exists", "screen_text"):
        tag = "（软）" if not spec.get("strict") else "（严格）"
        return f"{label} 含 {'/'.join(spec.get('expect') or [])}{tag}"
    if kind in ("file_exists", "not_file_exists"):
        return f"{label} {spec.get('path')}"
    if kind == "manual":
        return "人工确认（跑完看截图）"
    return label


# --------------------------------------------------------------------- #
# 系统状态读取
# --------------------------------------------------------------------- #
def _frontmost() -> Tuple[str, int]:
    """返回 (前台应用名, pid)；拿不到时返回 ("", 0)。"""
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return "", 0
        return str(app.localizedName() or ""), int(app.processIdentifier())
    except Exception:
        return "", 0


def _ax_attr(el, attr: str):
    """读一个 AX 属性，失败返回 None。"""
    try:
        import ApplicationServices as AX

        err, value = AX.AXUIElementCopyAttributeValue(el, attr, None)
        return value if err == 0 else None
    except Exception:
        return None


def _window_titles(pid: int) -> List[str]:
    """读某个应用所有窗口的标题。"""
    if not pid:
        return []
    try:
        import ApplicationServices as AX

        axapp = AX.AXUIElementCreateApplication(pid)
        windows = _ax_attr(axapp, "AXWindows") or []
        titles: List[str] = []
        for w in list(windows):
            t = _ax_attr(w, "AXTitle")
            if t:
                s = str(t).strip()
                if s:
                    titles.append(s)
        return titles
    except Exception:
        return []


def _elements_texts(limit: int = 120) -> List[str]:
    """枚举前台应用的可交互元素，返回它们的描述文本（用于软判据）。"""
    try:
        from yu_agent.perception import a11y

        a11y.set_excluded_pids([os.getpid()])
        elements = a11y.get_all_interactive_elements(
            limit, dock_max=20, max_apps=1, skip_menubar=True
        )
    except Exception:
        return []
    texts: List[str] = []
    for el in elements:
        for attr in ("title", "description"):
            v = getattr(el, attr, "") or ""
            if v:
                texts.append(str(v))
        try:
            texts.append(el.label())
        except Exception:
            pass
    return texts


def _match(expect: List[str], texts: List[str]) -> Optional[Tuple[str, str]]:
    """在 texts 里找 expect 中任一个关键词，返回 (命中关键词, 命中文本)。"""
    for kw in expect:
        k = str(kw)
        if not k:
            continue
        for t in texts:
            if k.lower() in str(t).lower():
                return k, str(t)
    return None


# --------------------------------------------------------------------- #
# 判据分派
# --------------------------------------------------------------------- #
def run_check(spec: Optional[Dict[str, Any]]) -> CheckResult:
    """执行一个判据。spec 为空视为 manual。"""
    if not spec:
        return CheckResult(None, "任务未定义判据")

    kind = str(spec.get("kind") or "manual")
    expect = [str(x) for x in (spec.get("expect") or [])]

    if kind == "manual":
        return CheckResult(None, "需人工确认（看报告里的截图）")

    # ── 硬判据：前台应用 ──
    if kind in ("frontmost_app", "not_frontmost_app"):
        name, pid = _frontmost()
        if not name:
            return CheckResult(False, "拿不到前台应用（屏幕锁定或权限不足）")
        hit = _match(expect, [name]) is not None
        if kind == "frontmost_app":
            return CheckResult(
                hit, f"前台是「{name}」" + ("" if hit else f"，期望 {'/'.join(expect)}")
            )
        # 前置条件：期望「不在前台」才算起点干净
        return CheckResult(
            not hit,
            f"前台是「{name}」（起点{'干净' if not hit else '已满足终点，本次不计分'}）",
        )

    # ── 硬判据：窗口标题 ──
    if kind == "window_title":
        name, pid = _frontmost()
        titles = _window_titles(pid)
        if not titles:
            return CheckResult(False, f"「{name}」没有任何窗口标题可读")
        got = _match(expect, titles)
        if got:
            return CheckResult(True, f"窗口标题命中「{got[0]}」：{got[1][:60]}")
        return CheckResult(
            False, f"窗口标题都没有 {'/'.join(expect)}：{titles[0][:60]}"
        )

    # ── 硬判据：文件系统 ──
    if kind in ("file_exists", "not_file_exists"):
        raw = str(spec.get("path") or "")
        if not raw:
            return CheckResult(False, "判据缺少 path")
        p = Path(os.path.expanduser(raw))
        exists = p.exists()
        if kind == "file_exists":
            return CheckResult(
                exists, f"{p} {'存在' if exists else '不存在'}"
            )
        return CheckResult(
            not exists,
            f"{p} {'已存在（起点已满足终点，本次不计分）' if exists else '不存在（起点干净）'}",
        )

    # ── 软判据：界面元素 / 屏幕文字 ──
    if kind in ("element_exists", "screen_text"):
        texts = _elements_texts()
        strict = bool(spec.get("strict"))
        if not texts:
            return CheckResult(
                False if strict else None,
                "枚举不到任何界面元素（应用可能没有辅助功能权限）",
            )
        got = _match(expect, texts)
        if got:
            return CheckResult(True, f"命中「{got[0]}」：{got[1][:60]}")
        if strict:
            return CheckResult(False, f"{len(texts)} 个元素里都没有 {'/'.join(expect)}")
        return CheckResult(
            None,
            f"{len(texts)} 个元素里没匹配到 {'/'.join(expect)}"
            f"（软判据，自动判不了；请看截图人工确认）",
        )

    return CheckResult(None, f"未知判据类型：{kind}")
