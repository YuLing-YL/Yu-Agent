"""感知层通用性回归测试（不依赖任何具体 App）。

真实设备上踩到的问题：
1. 很多框架（Finder 列表、Electron 列表、网页表格）的元素是「行 → 单元格 → 文字」
   三层套娃，**行/单元格自身的 AXTitle/Description/Value 全为空**，名字只在
   子元素 AXStaticText 的 AXValue 里 → 编号表里只剩「单元格」「行」，模型无法分辨，
   于是反复点同一个位置。
2. 同一矩形套三层 → 同一个位置冒出 2~3 个编号。
3. 工具栏图标大量无标签（AXImage/AXButton），把预算吃光。
4. 禁用元素（AXEnabled=false）被标成可点，点了没反应。

这些都用假 AX 树验证：换任何应用/框架，逻辑都该一致。
"""
import sys

sys.path.insert(0, "src")

from yu_agent.perception import a11y  # noqa: E402

SCREEN = (1512, 982)


def node(role, rect=(0, 0, 0, 0), children=None, **attrs):
    """构造一个假 AX 节点。attrs 里的键就是 AX 属性名对应的值。"""
    a = {"AXRole": role}
    a.update(attrs)
    return {"role": role, "rect": rect, "children": children or [], "attrs": a}


def patch_ax():
    """把 _ax / _pos_size_of 换成读假节点，并固定标签语言为中文（测试断言用中文）。"""
    orig = (a11y._ax, a11y._pos_size_of, a11y.label_language())
    a11y.set_label_language("zh")

    def fake_ax(el, attr):
        if attr == "AXChildren":
            return el.get("children") or [], True
        attrs = el.get("attrs") or {}
        if attr in attrs and attrs[attr] is not None:
            return attrs[attr], True
        return None, False

    a11y._ax = fake_ax
    a11y._pos_size_of = lambda el: el["rect"]
    return orig


def unpatch(orig):
    a11y._ax, a11y._pos_size_of = orig[0], orig[1]
    a11y.set_label_language(orig[2])


def walk(root, limit=60):
    out = []
    a11y._walk(root, out, 0, limit, SCREEN, "TestApp")
    return out


def test_name_from_child_text():
    """行/单元格自己没有属性，名字在子 AXStaticText 的 AXValue 里。"""
    orig = patch_ax()
    try:
        text = node("AXStaticText", (100, 200, 80, 18), AXValue="code")
        cell = node("AXCell", (90, 195, 400, 24), children=[text])
        row = node("AXRow", (90, 195, 400, 24), children=[cell])
        root = node("AXWindow", (0, 0, 1400, 900), children=[row])
        out = walk(root)
    finally:
        unpatch(orig)

    assert out, "没枚举出任何元素"
    assert out[0].title == "code", f"没从子元素补到名字：{out[0].title!r}"
    print(f"✓ 行/单元格从子元素补名：{out[0].label()}")


def test_nested_row_cell_merged():
    """行 + 单元格在同一矩形上 → 只保留一个（否则同一位置多个编号）。"""
    orig = patch_ax()
    try:
        text = node("AXStaticText", (100, 200, 80, 18), AXValue="code")
        cell = node("AXCell", (90, 195, 400, 24), children=[text])
        row = node("AXRow", (90, 195, 400, 24), children=[cell])
        root = node("AXWindow", (0, 0, 1400, 900), children=[row])
        out = walk(root)
        merged = a11y._dedupe_by_rect(out)
    finally:
        unpatch(orig)

    assert len(out) >= 2, f"前提：应该枚举到多个套娃元素，实际 {len(out)}"
    assert len(merged) == 1, f"同一矩形应合并成 1 个，实际 {len(merged)}"
    assert merged[0].title == "code"
    print(f"✓ 套娃合并：{len(out)} → {len(merged)}（保留 {merged[0].role}）")


def test_title_fallback_chain():
    """AXTitle 没有时用 AXDescription，再没有用 AXValue。"""
    orig = patch_ax()
    try:
        a = node("AXButton", (10, 10, 60, 24), AXDescription="下载")
        b = node("AXButton", (10, 50, 60, 24), AXValue="保存")
        c = node("AXButton", (10, 90, 60, 24), AXPlaceholderValue="搜索")
        root = node("AXWindow", (0, 0, 1400, 900), children=[a, b, c])
        out = walk(root)
    finally:
        unpatch(orig)
    titles = [e.title for e in out]
    assert titles == ["下载", "保存", "搜索"], titles
    print(f"✓ 属性回退链：{titles}")


def test_role_description_not_used_as_name():
    """AXRoleDescription 只是角色名（"图像"），不能当名字——否则一堆元素都叫"图像"。"""
    orig = patch_ax()
    try:
        img = node("AXImage", (10, 10, 20, 20), AXRoleDescription="图像")
        root = node("AXWindow", (0, 0, 1400, 900), children=[img])
        out = walk(root)
        assert out and out[0].title == "", f"不该拿角色名当名字：{out[0].title!r}"
        # label() 依赖全局标签语言，必须在 patch 生效期间取
        label = out[0].label()
    finally:
        unpatch(orig)
    assert "无标签" in label, label
    print("✓ 不用角色名冒充标题，并标注（无标签）")


def test_unnamed_icons_capped():
    """无标签图标不能无限多，每个应用最多保留 8 个。"""
    orig = patch_ax()
    try:
        icons = [
            node("AXImage", (100 + i * 30, 60, 24, 24)) for i in range(20)
        ]
        root = node("AXWindow", (0, 0, 1400, 900), children=icons)
        out = walk(root, limit=200)
        kept = a11y._cap_unnamed_icons(out)
    finally:
        unpatch(orig)

    assert len(out) == 20, f"前提：应枚举到 20 个图标，实际 {len(out)}"
    unnamed = [e for e in kept if not e.title]
    assert len(unnamed) <= a11y._UNNAMED_ICON_MAX, (
        f"无标签图标应被限制在 {a11y._UNNAMED_ICON_MAX} 个，实际 {len(unnamed)}"
    )
    print(f"✓ 无标签图标限流：20 → {len(unnamed)}")


def test_disabled_element_marked():
    """AXEnabled=false 的元素要标成「不可用」，模型才知道别点。"""
    orig = patch_ax()
    try:
        btn = node("AXButton", (10, 10, 80, 24), AXTitle="下一步", AXEnabled=False)
        root = node("AXWindow", (0, 0, 1400, 900), children=[btn])
        out = walk(root)
        assert out and out[0].enabled is False, out
        label = out[0].label()
    finally:
        unpatch(orig)
    assert "不可用" in label, label
    print(f"✓ 禁用元素标注：{label}")


def test_deep_text_not_grabbed():
    """太长的正文不该被当控件名（避免抓到段落文字）。"""
    long_para = "这是一段很长很长很长的正文内容，用来测试过滤器" * 6  # >60 字
    assert len(long_para) > 60
    orig = patch_ax()
    try:
        # 注意别放在 y<32 的菜单栏带里（那里宽度>260 的无名元素会被当成状态区容器过滤）
        long_text = node("AXStaticText", (200, 300, 400, 18), AXValue=long_para)
        cell = node("AXCell", (200, 300, 400, 18), children=[long_text])
        root = node("AXWindow", (0, 0, 1400, 900), children=[cell])
        out = walk(root)
    finally:
        unpatch(orig)
    assert out and out[0].title == "", f"超长正文不该当名字：{out[0].title[:30]!r}"
    print("✓ 超长正文不会被当控件名")


def test_offscreen_dock_dropped():
    """程序坞自动隐藏时坐标在屏幕外（y=987 > 屏高 982），要整组丢掉并上报状态。"""
    from yu_agent.perception.a11y import UIElement, _split_offscreen

    offscreen = [UIElement(id=i, role="程序坞图标", title=f"app{i}",
                           x=i * 50, y=987, w=51, h=67) for i in range(5)]
    visible, dropped = _split_offscreen(offscreen, 982)
    assert visible == [] and dropped == 5, (len(visible), dropped)

    onscreen = [UIElement(id=i, role="程序坞图标", title=f"app{i}",
                          x=i * 50, y=911, w=51, h=67) for i in range(5)]
    visible2, dropped2 = _split_offscreen(onscreen, 982)
    assert len(visible2) == 5 and dropped2 == 0
    print("✓ 自动隐藏的程序坞（屏幕外坐标）会被整组剔除并统计")


if __name__ == "__main__":
    test_offscreen_dock_dropped()
    test_name_from_child_text()
    test_nested_row_cell_merged()
    test_title_fallback_chain()
    test_role_description_not_used_as_name()
    test_unnamed_icons_capped()
    test_disabled_element_marked()
    test_deep_text_not_grabbed()
    print("\n=== 感知通用性测试全部通过 ===")
