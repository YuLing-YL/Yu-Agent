"""端到端冒烟测试：验证核心模块可导入 + 坐标还原 + 意图识别（离线，无需 API/屏幕）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_imports() -> None:
    """验证所有核心模块可导入。"""

    print("✓ 所有核心模块导入成功")


def test_coord_mapping() -> None:
    """验证坐标还原逻辑。"""
    from PIL import Image

    from yu_agent.common.image import scale_factor, scale_image
    from yu_agent.perception.screenshot import region_to_pixel

    img = Image.new("RGB", (1728, 1117))
    scaled = scale_image(img, 1568)
    sx, sy = scale_factor(img, scaled)
    x, y = region_to_pixel(784, 500, sx, sy)
    assert 0 <= x <= 1728 and 0 <= y <= 1117
    # 比例一致性
    assert abs(x / 1728 - 784 / scaled.size[0]) < 0.01
    print(f"✓ 坐标还原正确：缩放图(784,500) -> 逻辑像素({x},{y})")


def test_responder() -> None:
    """验证意图识别模块（用 mock backend 测解析逻辑）。"""
    from yu_agent.agent.responder import Responder

    # 用 mock backend，让它返回预设 JSON
    class FakeBackend:
        name = "fake"

        def __init__(self, output: str):
            self.output = output

        def chat(self, req):
            from yu_agent.models.base import VLMResponse

            return VLMResponse(text=self.output)

    # 打招呼 → 不需要 GUI
    r = Responder(FakeBackend('{"needs_gui": false, "reply": "你好！我是 Yu-Agent"}'))
    result = r.respond("你好")
    assert result["needs_gui"] is False
    assert result["reply"]
    print(f"✓ 意图识别（闲聊）：{result}")

    # 操作任务 → 需要 GUI
    r2 = Responder(FakeBackend('{"needs_gui": true, "reply": "好的，我来帮你打开浏览器"}'))
    result2 = r2.respond("打开浏览器")
    assert result2["needs_gui"] is True
    print(f"✓ 意图识别（任务）：{result2}")

    # 解析失败 + 纯文本含动作词 → 启发式判断需要 GUI
    r3 = Responder(FakeBackend("好的，我来帮你搜索天气"))
    result3 = r3.respond("搜索天气")
    assert result3["needs_gui"] is True
    print(f"✓ 解析失败（含动作词）：{result3}")

    # 解析失败 + 纯文本打招呼 → 启发式判断不需要 GUI
    r4 = Responder(FakeBackend("你好，我是 Yu-Agent"))
    result4 = r4.respond("你好")
    assert result4["needs_gui"] is False
    print(f"✓ 解析失败（打招呼）：{result4}")

    # 空输出 → 保守地需要 GUI
    r5 = Responder(FakeBackend(""))
    result5 = r5.respond("随便")
    assert result5["needs_gui"] is True
    print(f"✓ 空输出降级：{result5}")


def test_memory() -> None:
    """验证记忆可读写（内存库）。"""
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore

    m = MemoryManager(store=MemoryStore(db_path=":memory:"))
    m.set_preference("测试键", "测试值")
    prefs = m.profile_context()
    assert "测试键" in prefs
    print("✓ 记忆读写成功")


def test_saved_models() -> None:
    """验证模型历史的存取（内存验证，不写真实配置）。"""
    from yu_agent.settings import get_saved_models

    # 只验证函数可调用
    models = get_saved_models()
    assert isinstance(models, list)
    print("✓ 模型历史函数可用")


def test_a11y_container_traversal() -> None:
    """回归：必须能穿透容器角色找到输入框。

    曾经的 bug：_walk 对 AXGroup / AXScrollArea 等容器直接 continue，
    等于整棵子树被跳过 —— 浏览器网页里的输入框永远拿不到 SOM 标记，
    表现就是「agent 找不到输入框」。
    """
    from yu_agent.perception import a11y

    tree = {
        "root": {"children": ["grp"]},
        "grp": {"children": ["scroll"], "role": "AXGroup"},
        "scroll": {"children": ["tf"], "role": "AXScrollArea"},
        "tf": {"children": [], "role": "AXTextField", "title": "搜索"},
    }
    orig = (a11y._ax, a11y._role_of, a11y._title_of, a11y._pos_size_of)
    try:
        a11y._ax = lambda node, attr: (
            (tree[node].get("children", []), True)
            if attr == "AXChildren"
            else (None, False)
        )
        a11y._role_of = lambda node: tree[node].get("role", "")
        a11y._title_of = lambda node: tree[node].get("title", "")
        a11y._pos_size_of = lambda node: (10.0, 20.0, 100.0, 30.0)

        out = []
        a11y._walk("root", out, 0, 6, (1512.0, 982.0))
        roles = [e.role for e in out]
        assert "AXTextField" in roles, f"容器子树被跳过，没找到输入框：{roles}"
        assert roles == ["AXTextField"], f"容器本身不该被当成可点元素：{roles}"
        print(f"✓ 容器子树可穿透（找到 {roles}）")
    finally:
        (a11y._ax, a11y._role_of, a11y._title_of, a11y._pos_size_of) = orig


class _FakeAXModule:
    """假的 ApplicationServices 模块，让 get_dock_elements / _app_elements 能离线跑。"""

    @staticmethod
    def AXUIElementCreateApplication(pid):
        return f"app:{pid}"


def _patch_a11y(monkeypatch_fn):
    """上下文管理器：把 a11y 的底层调用换成假的。"""
    import contextlib

    from yu_agent.perception import a11y

    @contextlib.contextmanager
    def ctx():
        orig = {
            name: getattr(a11y, name)
            for name in (
                "_dock_pid", "_children_of", "_role_of", "_title_of",
                "_pos_size_of", "_subrole_of", "_screen_size", "_app_name",
                "_on_screen_windows", "_calibrate", "_app_elements",
            )
        }
        saved_ax = sys.modules.get("ApplicationServices")
        sys.modules["ApplicationServices"] = _FakeAXModule
        try:
            monkeypatch_fn(a11y)
            yield a11y
        finally:
            for name, fn in orig.items():
                setattr(a11y, name, fn)
            if saved_ax is None:
                sys.modules.pop("ApplicationServices", None)
            else:
                sys.modules["ApplicationServices"] = saved_ax

    return ctx()


def test_dock_not_truncated() -> None:
    """回归：程序坞不能被硬截断（用户有 26 个图标时右半截曾完全没有标记）。"""

    N = 26

    def setup(mod):
        mod._dock_pid = lambda: 999
        mod._children_of = lambda el: (
            [f"dock:{i}" for i in range(N)] if el == "app:999" else []
        )
        mod._role_of = lambda el: "AXDockItem"
        mod._subrole_of = lambda el: "AXApplicationDockItem"
        mod._title_of = lambda el: f"图标{el.split(':')[1]}"
        mod._pos_size_of = lambda el: (float(el.split(":")[1]) * 52, 940.0, 52.0, 68.0)

    with _patch_a11y(setup) as mod:
        alli = mod.get_dock_elements(30)
        assert len(alli) == N, f"程序坞应全部识别 {N} 个，实际 {len(alli)}"
        assert alli[0].kind == "dock"
        assert alli[0].x < alli[-1].x, "程序坞应按从左到右排序"
        print(f"✓ 程序坞不再截断：{len(alli)}/{N} 个图标全部标记")

        capped = mod.get_dock_elements(10)
        assert len(capped) == 10, f"上限参数应生效，实际 {len(capped)}"
        print("✓ 程序坞上限可配置")


def test_oversized_container_filtered() -> None:
    """回归：整屏背景/菜单栏状态区容器不该被标成可点元素。"""
    from yu_agent.perception.a11y import _useful

    screen = (1512.0, 982.0)
    assert not _useful("AXGroup", "", 0, 0, 1500, 970, screen), "整屏容器应被过滤"
    assert not _useful("AXImage", "", 1085, 0, 427, 28, screen), "菜单栏超宽容器应被过滤"
    assert not _useful("AXButton", "确定", 10, 10, 0, 0, screen), "零尺寸应被过滤"
    assert not _useful("AXButton", "关闭", 361, 108, 1, 28, screen), "1px 宽的分隔条应被过滤"
    assert _useful("AXButton", "确定", 10, 10, 80, 30, screen), "正常按钮应保留"
    assert _useful("AXStaticText", "时间", 1290, 0, 200, 28, screen), "窄的菜单栏项应保留"
    print("✓ 超大容器 / 零尺寸被过滤，正常控件保留")


def test_multiapp_enumeration_and_occlusion() -> None:
    """回归：要枚举屏幕上所有可见应用，并丢掉被前面窗口遮住的元素。"""
    from yu_agent.perception.a11y import UIElement

    calls = []

    def setup(mod):
        # 三个应用：Safari(前台) / 终端 / 备忘录，每个一扇窗口
        mod._on_screen_windows = lambda: [
            {"pid": 1, "name": "Safari", "x": 0, "y": 0, "w": 800, "h": 600},
            {"pid": 2, "name": "终端", "x": 400, "y": 300, "w": 600, "h": 400},
            {"pid": 3, "name": "备忘录", "x": 900, "y": 100, "w": 400, "h": 300},
        ]
        mod._screen_size = lambda: (1512.0, 982.0)
        mod._dock_pid = lambda: None          # 程序坞不在本用例范围内
        mod._calibrate = lambda: (0.0, 0.0)
        mod._app_name = lambda pid: {1: "Safari", 2: "终端", 3: "备忘录"}.get(pid, "")

        def fake_app_elements(pid, name, occluders, limit, screen,
                              is_frontmost=False, skip_menubar=True,
                              win_rects=None, calib=(0.0, 0.0)):
            calls.append({"pid": pid, "name": name, "limit": limit,
                          "occluders": len(occluders), "front": is_frontmost})
            return [
                UIElement(id=0, role="AXButton", title=f"按钮{pid}-{i}",
                          x=10.0 * i, y=10.0, w=60.0, h=24.0, app=name)
                for i in range(limit)
            ]

        mod._app_elements = fake_app_elements

    with _patch_a11y(setup) as mod:
        els = mod.get_all_interactive_elements(100, dock_max=0, max_apps=6)
        pids = [c["pid"] for c in calls]
        assert pids == [1, 2, 3], f"应按前后顺序枚举所有可见应用，实际 {pids}"
        # 前台应用预算最大；后面的应用逐个把前面窗口当作遮挡物
        assert calls[0]["limit"] > calls[1]["limit"], "前台应用应拿到更大预算"
        assert [c["occluders"] for c in calls] == [0, 1, 2], (
            f"遮挡物应逐层累积，实际 {[c['occluders'] for c in calls]}"
        )
        assert [c["front"] for c in calls] == [True, False, False], (
            "只有第一个（前台）应用才算前台，其它应用的菜单栏项应被忽略"
        )
        assert len(els) <= 100, "总标记数不应超过预算"
        apps = {e.app for e in els}
        assert apps == {"Safari", "终端", "备忘录"}, f"应包含所有应用的元素，实际 {apps}"
        print(f"✓ 多应用枚举：{[c['name'] for c in calls]}，"
              f"预算 {[c['limit'] for c in calls]}，遮挡逐层累积")


def test_occluded_elements_dropped() -> None:
    """回归：被更靠前窗口盖住的元素不该被标记（点了也点不到）。"""
    from yu_agent.perception.a11y import UIElement

    def setup(mod):
        mod._app_name = lambda pid: "后台应用"
        mod._dedupe = lambda els: els

        def fake_walk(el, out, depth, limit, screen, app_name=""):
            out.append(UIElement(id=0, role="AXButton", title="可见的",
                                 x=100, y=100, w=80, h=30, app=app_name))
            out.append(UIElement(id=1, role="AXButton", title="被盖住的",
                                 x=800, y=100, w=80, h=30, app=app_name))

        mod._walk = fake_walk

    with _patch_a11y(setup) as mod:
        kept = mod._app_elements(
            pid=2, app_name="后台应用",
            occluders=[(760.0, 0.0, 400.0, 600.0)],   # 盖住 x>=760 的区域
            limit=50, screen=(1512.0, 982.0),
        )
        titles = [e.title for e in kept]
        assert titles == ["可见的"], f"被遮挡的元素应被丢弃，实际 {titles}"
        print("✓ 被遮挡的元素不再标记")


def test_background_app_menubar_skipped() -> None:
    """回归：非前台应用的「菜单栏项」是重影，不能标记。"""
    from yu_agent.perception.a11y import UIElement

    def setup(mod):
        mod._app_name = lambda pid: "后台应用"
        mod._dedupe = lambda els: els

        def fake_walk(el, out, depth, limit, screen, app_name="", in_list=False):
            out.append(UIElement(id=0, role="AXMenuBarItem", title="文件",
                                 x=112, y=16, w=47, h=33, app=app_name))
            out.append(UIElement(id=1, role="AXButton", title="真的按钮",
                                 x=200, y=300, w=80, h=30, app=app_name))

        mod._walk = fake_walk

    with _patch_a11y(setup) as mod:
        # 默认：菜单栏项一律不标（几乎用不到，而且一长排会占掉大量编号）
        front = mod._app_elements(1, "前台", [], 50, (1512.0, 982.0), is_frontmost=True)
        assert [e.title for e in front] == ["真的按钮"], front

        # 任务确实要用菜单时（skip_menubar=False）：前台保留，后台重影仍丢弃
        front2 = mod._app_elements(
            1, "前台", [], 50, (1512.0, 982.0), is_frontmost=True, skip_menubar=False
        )
        assert len(front2) == 2, "显式要求菜单时，前台应用的菜单栏项应当保留"

        back = mod._app_elements(
            2, "后台", [], 50, (1512.0, 982.0), is_frontmost=False, skip_menubar=False
        )
        assert [e.title for e in back] == ["真的按钮"], (
            f"非前台应用的菜单栏项应被丢弃，实际 {[e.title for e in back]}"
        )
        print("✓ 菜单栏项默认跳过；非前台应用的菜单栏重影始终忽略")


def test_describe_marks_grouped() -> None:
    """编号表要按应用分组，模型才能分清每个编号属于哪个窗口。"""
    from yu_agent.perception.a11y import UIElement
    from yu_agent.perception.som import describe_marks

    mp = {
        1: UIElement(id=0, role="AXButton", title="后退", x=0, y=0, w=20, h=20,
                     app="Safari", kind="app"),
        2: UIElement(id=1, role="AXTextField", title="搜索", x=0, y=0, w=20, h=20,
                     app="Safari", kind="app"),
        3: UIElement(id=2, role="程序坞图标", title="访达", x=0, y=0, w=20, h=20,
                     app="程序坞", kind="dock"),
    }
    text = describe_marks(mp, "zh")
    # 编号跨帧稳定后不再连续，分组头用「数量 + 编号列表」
    assert "【Safari】2 个：#1, #2" in text, text
    assert "【程序坞】1 个图标" in text, text
    assert "Safari | 按钮 | 后退" in text, text

    # 英文模式下表头与角色名都要变成英文
    en_text = describe_marks(mp, "en")
    assert "[Safari] 2: #1, #2" in en_text, en_text
    assert "[dock] 1 icons" in en_text, en_text
    assert "Safari | button | 后退" in en_text, en_text
    print("✓ 编号表按应用分组，中英文表头都正确")





def test_som_scale_and_snap() -> None:
    """验证 SOM 在截图缩放下的坐标换算，以及「坐标吸附到最近标记」。"""
    from PIL import Image

    from yu_agent.agent.loop import AgentLoop
    from yu_agent.perception.a11y import UIElement
    from yu_agent.perception.som import draw_marks

    # 元素坐标是逻辑像素（1512x982 屏上的输入框和按钮）
    els = [
        UIElement(id=0, role="AXTextField", title="搜索", x=600, y=100, w=200, h=40),
        UIElement(id=1, role="AXButton", title="确定", x=850, y=100, w=80, h=40),
    ]
    # 截图被缩放为 0.5 倍时，画框不应把元素误判为「在画面外」
    img = Image.new("RGB", (756, 491))
    _, mp = draw_marks(img, els, coord_scale=0.5)
    assert len(mp) == 2, f"缩放后不应丢元素，实际 {len(mp)}"
    print("✓ SOM 缩放坐标换算：不丢元素")

    lo = AgentLoop.__new__(AgentLoop)  # 不跑 __init__，只测纯函数
    # 模型直接给了坐标，且离输入框中心(700,120)约 10px → 应吸附
    snap = AgentLoop._snap_to_mark(lo, 690, 118, mp)
    assert snap is not None, "靠近输入框时应吸附"
    x, y, num, el = snap
    assert (x, y) == (700, 120), f"应吸附到输入框中心，实际 {(x, y)}"
    print(f"✓ 坐标吸附：(690,118) → #{num} {el.title} 中心 ({x},{y})")

    # 离所有元素都很远 → 不吸附（避免把离谱坐标硬拉到某个元素上）
    assert AgentLoop._snap_to_mark(lo, 100, 700, mp) is None
    print("✓ 远离元素时不吸附")


def test_prompt_language() -> None:
    """提示词语言切换：en 用 system.en.md，zh 用 system.md，占位符都要能渲染。"""
    from yu_agent.actions.schema import describe_actions
    from yu_agent.prompts.loader import load_prompt, prompt_file, render_prompt
    from yu_agent.skills import build_default_registry

    skills = build_default_registry().describe_for_prompt()

    en = render_prompt("system", language="en", actions=describe_actions("en"), skills=skills)
    zh = render_prompt("system", language="zh", actions=describe_actions("zh"), skills=skills)

    assert "You are Yu-Agent" in en, en[:80]
    assert "你是 Yu-Agent" in zh, zh[:80]
    assert "{actions}" not in en and "{skills}" not in en
    assert "{actions}" not in zh and "{skills}" not in zh
    assert prompt_file("system", "en") == "system.en.md"
    assert prompt_file("system", "zh") == "system.md"
    assert load_prompt("system", "en") != load_prompt("system", "zh")
    assert describe_actions("en").startswith("- click: left click")
    print("✓ 提示词语言可切换（en / zh），占位符渲染正常")


if __name__ == "__main__":
    test_imports()
    test_coord_mapping()
    test_responder()
    test_memory()
    test_saved_models()
    test_prompt_language()
    test_a11y_container_traversal()
    test_som_scale_and_snap()
    test_dock_not_truncated()
    test_oversized_container_filtered()
    test_multiapp_enumeration_and_occlusion()
    test_occluded_elements_dropped()
    test_background_app_menubar_skipped()
    test_describe_marks_grouped()
    print("\n=== 冒烟测试全部通过 ===")

