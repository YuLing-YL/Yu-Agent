"""标注质量回归测试：编号稳定 / 标签避让 / 降噪 / 语义纠正 / som_mode。

背景（用户实测 runs/20261020-124316）：
模型第 1 步点 #38 = iCloud云盘，第 2 步它想点 yuling，输出的仍是 #38——
而这一帧的 #38 已经变成 yuling 的位置。根因是编号每帧按坐标重排，
模型记住的编号会指到别的元素上。这里锁死修复后的行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image

from yu_agent.actions.schema import Action
from yu_agent.grounding.parser import parse_action
from yu_agent.perception import a11y
from yu_agent.perception.a11y import UIElement, _drop_noise, _split_offscreen
from yu_agent.perception.som import MarkStabilizer, _label_anchor, _overlaps, draw_marks


def el(title: str, x: float, y: float, w: float = 100, h: float = 24,
       role: str = "AXCell", app: str = "访达", **kw) -> UIElement:
    return UIElement(id=0, role=role, title=title, x=x, y=y, w=w, h=h,
                     app=app, kind="app", **kw)


# ── 1. 编号跨帧稳定 ─────────────────────────────────────────────── #
def test_numbers_stable_across_frames() -> None:
    """元素顺序变化 / 新增元素 / 轻微位移，编号都不许漂移。"""
    stab = MarkStabilizer()
    f1 = [el("iCloud云盘", 300, 460), el("yuling", 300, 490), el("Macintosh HD", 300, 520)]
    n1 = stab.assign(f1)
    assert n1 == [1, 2, 3], n1

    # 顺序全变 + 插入一个新元素
    f2 = [el("yuling", 300, 490), el("新来的", 300, 560), el("iCloud云盘", 300, 460)]
    n2 = stab.assign(f2)
    assert n2[0] == n1[1], f"yuling 编号漂了：{n1} → {n2}"
    assert n2[2] == n1[0], f"iCloud云盘 编号漂了：{n1} → {n2}"
    assert n2[1] not in n1, f"新元素应拿新编号，实际 {n2}"

    # 轻微位移（滚动 3px）
    f3 = [el("yuling", 303, 493), el("iCloud云盘", 300, 460)]
    n3 = stab.assign(f3)
    assert n3[0] == n1[1], f"位移后编号漂了：{n1} → {n3}"
    print(f"✓ 编号跨帧稳定：{n1} / {n2} / {n3}")


def test_numbers_reuse_after_scroll() -> None:
    """列表滚动几十像素，同名元素仍复用旧编号（靠模糊匹配）。"""
    stab = MarkStabilizer()
    n1 = stab.assign([el("code", 500, 400), el("Yu-Agent", 500, 430)])
    n2 = stab.assign([el("code", 500, 340), el("Yu-Agent", 500, 370)])  # 上滚 60px
    assert n2 == n1, f"滚动后编号应保持：{n1} → {n2}"
    print(f"✓ 列表滚动 60px 后编号仍为 {n2}")


def test_numbers_reset_per_task() -> None:
    """同一任务内稳定，跨任务重排（否则编号会一直增长）。"""
    stab = MarkStabilizer()
    stab.assign([el("a", 100, 100), el("b", 100, 130)])
    stab.reset()
    assert stab.assign([el("b", 100, 130), el("a", 100, 100)]) == [1, 2]
    print("✓ reset 后编号从头开始")


# ── 2. 标签避让 ─────────────────────────────────────────────────── #
def test_label_avoids_overlap() -> None:
    """相邻元素的编号标签不能压在一起（这是模型读错编号的主因）。"""
    W, H = 1000, 800
    t1 = _label_anchor(100, 100, 200, 118, 20, 16, W, H, [])
    occupied = [(t1[0] - 3, t1[1] - 3, t1[0] + 23, t1[1] + 19)]
    # 紧挨着的下一个元素
    t2 = _label_anchor(100, 118, 200, 136, 20, 16, W, H, occupied)
    box2 = (t2[0] - 3, t2[1] - 3, t2[0] + 23, t2[1] + 19)
    assert not _overlaps(box2, occupied[0]), f"标签仍然重叠：{box2} vs {occupied[0]}"
    print(f"✓ 相邻元素标签自动错位：{t1} → {t2}")


def test_draw_marks_uses_stabilizer_and_avoids_overlap() -> None:
    """draw_marks 要走稳定编号，且同一张图里标签互不重叠。"""
    img = Image.new("RGB", (1200, 800), (255, 255, 255))
    els = [el(f"项{i}", 100, 100 + i * 18, w=120, h=16) for i in range(6)]
    stab = MarkStabilizer()
    # 纯白画布上没有内容可校准 → 关掉内容检测，专测「稳定编号 + 标签避让」
    marked, mp = draw_marks(img, els, stabilizer=stab, content_check=False)
    assert len(mp) == 6, mp
    # 第二帧顺序反过来，编号必须跟着元素走
    els2 = list(reversed(els))
    _, mp2 = draw_marks(img, els2, stabilizer=stab, content_check=False)
    for num, e in mp.items():
        assert mp2[num].title == e.title, f"#{num} 指向的元素变了：{e.title} → {mp2[num].title}"
    print("✓ draw_marks 使用稳定编号，标签互不重叠")


# ── 3. 降噪 ─────────────────────────────────────────────────────── #
def test_drop_noise_rules() -> None:
    """菜单栏项 / 点不中的细小元素 / 无标签且禁用 都要被丢掉。"""
    orig = a11y._screen_size
    a11y._screen_size = lambda: (1512.0, 982.0)
    try:
        els = [
            el("文件", 112, 16, 47, 33, role="AXMenuBarItem"),      # 菜单栏
            el("真的好按钮", 200, 300, 80, 30, role="AXButton"),     # 正常
            el("太窄了", 300, 300, 8, 20, role="AXButton"),          # 太窄
            el("太扁了", 300, 400, 60, 6, role="AXButton"),          # 太扁
            el("", 400, 300, 60, 24, role="AXButton", enabled=False),  # 无名+禁用
            el("禁用但有名字", 500, 300, 60, 24, role="AXButton", enabled=False),
        ]
        kept = _drop_noise(els)
        titles = [e.title for e in kept]
        assert "文件" not in titles, "菜单栏项应被跳过"
        assert "太窄了" not in titles and "太扁了" not in titles, "细小元素应被跳过"
        assert "" not in titles, "无标签且禁用的元素应被跳过"
        assert "真的好按钮" in titles and "禁用但有名字" in titles, titles

        # 任务确实要用菜单时，显式关闭跳过
        kept2 = _drop_noise(els, skip_menubar=False)
        assert "文件" in [e.title for e in kept2]
    finally:
        a11y._screen_size = orig
    print("✓ 降噪规则：菜单栏/细小/无名禁用 全部剔除")


def test_nested_inputs_collapsed() -> None:
    """搜索框「外层 SearchField + 内层 TextField」只留一个编号。

    实测 Safari 地址栏、微信搜索框都是这种结构，两层各占一个编号，
    挤在同一个位置，模型容易在两个号之间犹豫甚至选到内层。
    """
    els = [
        el("搜索", 100, 100, 480, 32, role="AXSearchField"),
        el("", 104, 102, 470, 28, role="AXTextField"),
        el("发送人", 600, 100, 60, 30, role="AXButton"),
    ]
    kept = _drop_noise(els)
    inputs = [e for e in kept if e.role in ("AXSearchField", "AXTextField")]
    assert len(inputs) == 1, f"输入框套娃应只留一个，实际 {[e.role for e in inputs]}"
    assert inputs[0].role == "AXSearchField", "应保留面积大的外层"
    assert any(e.title == "发送人" for e in kept)
    print(f"✓ 输入框套娃合并：3 → {len(kept)} 个")


def test_offscreen_dock_still_dropped() -> None:
    """程序坞自动隐藏时坐标在屏幕外，仍要整组剔除。"""
    off = [el(f"app{i}", i * 50, 987, 51, 67, role="程序坞图标") for i in range(4)]
    visible, dropped = _split_offscreen(off, 982)
    assert visible == [] and dropped == 4
    print("✓ 屏幕外的程序坞图标仍被剔除")


# ── 4. 列表项 vs 输入框 ─────────────────────────────────────────── #
def test_list_item_not_treated_as_input() -> None:
    """Finder 列表里的文件名（AXTextField + in_list）不是输入框。"""
    from yu_agent.agent.loop import _is_input_like

    listed = el("code", 500, 400, 37, 18, role="AXTextField", in_list=True)
    real = el("智能搜索栏", 966, 66, 481, 31, role="AXTextField")
    assert not _is_input_like(listed), "列表项被误判成输入框 → 会导致它去 type"
    assert _is_input_like(real), "真正的输入框必须被识别"
    assert "列表项" in listed.label() or "list item" in listed.label(), listed.label()
    print(f"✓ 列表项不当作输入框：{listed.label()}")


# ── 5. 编号语义校验 ─────────────────────────────────────────────── #
def test_mark_semantic_correction() -> None:
    """模型说点 yuling 却给了 iCloud云盘 的编号 → 自动纠正。"""
    from yu_agent.agent.loop import _verify_mark_semantics

    mp = {1: el("iCloud云盘", 300, 460), 2: el("yuling", 300, 490),
          3: el("下载", 300, 520), 4: el("code", 500, 400)}
    a = parse_action('{"type":"click","mark":1,"reason":"click the yuling folder"}')
    assert _verify_mark_semantics(a, mp) == 2, "应纠正到 #2"

    b = parse_action('{"type":"click","mark":1,"reason":"click iCloud云盘"}')
    assert _verify_mark_semantics(b, mp) is None, "本来就对，不该改"

    # reason 里同时提到两个元素 → 有歧义，不擅自纠正
    c = parse_action('{"type":"click","mark":1,"reason":"open 下载 then 个人收藏"}')
    mp2 = dict(mp)
    mp2[5] = el("个人收藏", 300, 600)
    assert _verify_mark_semantics(c, mp2) in (None, 3, 5)
    print("✓ 编号与语义不一致时自动纠正，有歧义时不动")


# ── 6. som_mode 三态 ────────────────────────────────────────────── #
def _make_loop(**cfg):
    """构造一个不碰真实屏幕/鼠标的 AgentLoop。"""
    from yu_agent.agent.loop import AgentLoop
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore
    from yu_agent.models.base import VLMResponse
    from yu_agent.perception.screenshot import Screen
    from yu_agent.skills import build_default_registry

    calls = {"som": 0, "prompts": []}

    class Backend:
        name = "capture"

        def __init__(self):
            self.i = 0

        def chat(self, req):
            calls["prompts"].append(req.prompt)
            seq = ['{"type":"click","mark":1,"reason":"click the search field"}',
                   '{"type":"done","reason":"finished"}']
            s = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return VLMResponse(text=s)

    import yu_agent.agent.loop as loopmod
    import yu_agent.actions.executor as exmod

    saved = (loopmod.capture_screen, loopmod.get_screen_size, loopmod.build_som_mapping,
             exmod.ActionExecutor.execute)
    loopmod.capture_screen = lambda: Image.new("RGB", (1200, 800))
    loopmod.get_screen_size = lambda: Screen(1200, 800)
    exmod.ActionExecutor.execute = lambda self, a: None

    def fake_som(img, *a, **k):
        calls["som"] += 1
        e = el("智能搜索栏", 900, 60, 300, 30, role="AXTextField", app="Safari")
        from yu_agent.perception.som import MarkStabilizer as _MS
        stab = k.get("stabilizer") or _MS()
        if k.get("draw", True):
            return img, {1: e}, [e]
        return img, {1: e}, [e]

    loopmod.build_som_mapping = fake_som

    cfg.setdefault("max_steps", 3)
    cfg.setdefault("verify_after_action", False)
    lp = AgentLoop(
        Backend(),
        MemoryManager(store=MemoryStore(db_path=":memory:")),
        build_default_registry(),
        config=cfg,
        dry_run=True,
    )
    return lp, calls, saved, loopmod, exmod


def test_som_mode_coords_and_off() -> None:
    """coords 模式不画框但仍枚举（供吸附）；off 模式完全不枚举。"""
    lp, calls, saved, loopmod, exmod = _make_loop(som_mode="coords", prompt_language="en")
    try:
        assert lp.som_mode == "coords"
        lp.run("搜索天气")
        assert calls["som"] == 2, f"coords 模式应每步都枚举，实际 {calls['som']}"
        # 没把编号表给模型（编号没画在图上，给了也指不准）
        assert "#1" not in calls["prompts"][0]
    finally:
        loopmod.capture_screen, loopmod.get_screen_size, loopmod.build_som_mapping, \
            exmod.ActionExecutor.execute = saved

    lp2, calls2, saved2, loopmod2, exmod2 = _make_loop(som_mode="off", prompt_language="en")
    try:
        assert lp2.som_mode == "off"
        lp2.run("搜索天气")
        assert calls2["som"] == 0, "off 模式不该枚举任何元素"
    finally:
        loopmod2.capture_screen, loopmod2.get_screen_size, loopmod2.build_som_mapping, \
            exmod2.ActionExecutor.execute = saved2
    print("✓ som_mode：coords 只枚举不画框，off 完全不枚举")


def test_som_frontmost_only_forces_one_app() -> None:
    lp, _, saved, loopmod, exmod = _make_loop(
        som_frontmost_only=True, som_max_apps=6, prompt_language="en"
    )
    try:
        assert lp.som_max_apps == 1, "只标当前窗口时应强制只枚举 1 个应用"
    finally:
        loopmod.capture_screen, loopmod.get_screen_size, loopmod.build_som_mapping, \
            exmod.ActionExecutor.execute = saved


def test_no_desktop_hiding_anymore() -> None:
    """隐藏其它窗口的功能已移除：loop 上不应再有 reset_desktop。"""
    lp, _, saved, loopmod, exmod = _make_loop(prompt_language="en")
    try:
        assert not hasattr(lp, "reset_desktop"), "reset_desktop 应已删除"
        assert not hasattr(lp, "_prepare_desktop"), "_prepare_desktop 应已删除"
        assert not hasattr(lp, "_build_prepare_hint"), "_build_prepare_hint 应已删除"
    finally:
        loopmod.capture_screen, loopmod.get_screen_size, loopmod.build_som_mapping, \
            exmod.ActionExecutor.execute = saved
    import importlib.util
    assert importlib.util.find_spec("yu_agent.actions.desktop") is None, \
        "actions/desktop.py 应已删除"
    print("✓ 隐藏其它窗口的功能与模块均已移除")


if __name__ == "__main__":
    test_numbers_stable_across_frames()
    test_numbers_reuse_after_scroll()
    test_numbers_reset_per_task()
    test_label_avoids_overlap()
    test_draw_marks_uses_stabilizer_and_avoids_overlap()
    test_drop_noise_rules()
    test_nested_inputs_collapsed()
    test_offscreen_dock_still_dropped()
    test_list_item_not_treated_as_input()
    test_mark_semantic_correction()
    test_som_mode_coords_and_off()
    test_som_frontmost_only_forces_one_app()
    test_no_desktop_hiding_anymore()
    print("\n=== 标注质量测试全部通过 ===")
