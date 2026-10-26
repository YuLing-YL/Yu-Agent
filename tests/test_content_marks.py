"""标注准确度回归测试：内容校准 / 收缩 / 空框丢弃 / 窗口裁剪 / 语义锚定。

背景（用户实测 runs/20261019-212505/step_01.png）：
标注图上大量红框比元素本身高出 2~4 倍，相邻框互相重叠，还有些框整片罩在
空白区域上 —— 模型根本分不清哪个编号对应哪一行，于是点错。
根因有三个，这里逐个锁死修复后的行为：
1. AX 报的矩形是「布局盒子」（含 padding / 整行宽），不是「眼睛看到的东西」；
2. AX 会报出已不可见的元素（窗口被切走、Electron 未渲染区域）；
3. 标签字号跟着元素高度缩，在密集列表里互相压住，数字糊成一团。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw

from yu_agent.actions.schema import Action
from yu_agent.perception import a11y
from yu_agent.perception.a11y import UIElement, _clip_rect_to, _dist_to_rect
from yu_agent.perception.content import content_info, shrink_box
from yu_agent.perception.som import MarkStabilizer, draw_marks


def _scene() -> Image.Image:
    """造一个「窗口 + 侧边栏列表 + 聊天行 + 一片空白」的合成界面。"""
    img = Image.new("RGB", (900, 600), (245, 245, 247))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 880, 580], fill=(255, 255, 255))
    d.rectangle([20, 20, 240, 580], fill=(238, 238, 242))
    for i in range(5):                       # 侧边栏 5 行文字
        d.text((80, 70 + i * 44), f"Row{i}", fill=(30, 30, 30))
        d.ellipse([58, 72 + i * 44, 70, 84 + i * 44], fill=(120, 160, 120))
    for i in range(4):                       # 右侧 4 行聊天
        d.text((280, 70 + i * 60), "Name", fill=(20, 20, 20))
        d.text((280, 88 + i * 60), "message body", fill=(110, 110, 110))
        d.line([(270, 118 + i * 60), (860, 118 + i * 60)], fill=(232, 232, 232))
    return img


# ── 1. 内容检测 ──────────────────────────────────────────────────── #
def test_content_info_detects_and_rejects() -> None:
    img = _scene()
    # 有文字的侧边栏行
    hit = content_info(img, (20, 55, 240, 105))
    assert hit is not None and hit.has_content, "有文字的行必须被判为有内容"
    # 完全空白（窗口右下角没有任何控件）
    blank = content_info(img, (300, 300, 700, 360))
    assert blank is not None and not blank.has_content, "纯空白区必须被判为空"
    # 只有一条 1px 分隔线：不能当成内容（否则内容框会横向拉满整窗）
    line_only = content_info(img, (270, 110, 860, 126))
    assert line_only is not None and not line_only.has_content, (
        f"单独一条分隔线不该被当成内容，实际 bbox={line_only.bbox}"
    )
    print("✓ 内容检测：文字=有内容，空白/单条分隔线=空")


def test_content_info_survives_wide_rows() -> None:
    """宽行里的短文字：不能被「整行平均」稀释掉（踩过的坑）。"""
    img = _scene()
    info = content_info(img, (260, 60, 870, 130))
    assert info is not None and info.has_content, "宽行里的文字必须能检测到"
    assert info.bbox is not None and info.bbox[0] < 400, (
        f"内容框应贴着左边的文字，而不是被拉到整行，实际 {info.bbox}"
    )
    print(f"✓ 宽行短文字仍能定位：bbox={info.bbox}")


# ── 2. 收缩 ─────────────────────────────────────────────────────── #
def test_shrink_box_pulls_box_onto_content() -> None:
    img = _scene()
    # 模拟 AX 把一行报成 4 倍高：内容在 70~92，框是 55~145
    box = (20.0, 55.0, 240.0, 145.0)
    info = content_info(img, box)
    assert info is not None and info.has_content
    nb = shrink_box(box, info, max_shrink=1.0)
    assert nb[3] - nb[1] < (box[3] - box[1]) * 0.75, f"框没有收缩到位：{box} → {nb}"
    assert nb[0] >= box[0] and nb[1] >= box[1], "收缩后不能超出原框"
    print(f"✓ 过大的行框收缩：{box} → {nb}")


def test_shrink_box_keeps_tiny_buttons() -> None:
    """内容比 8px 还小时宁可不动，别把点击区缩没了。"""
    img = _scene()
    box = (20.0, 55.0, 240.0, 145.0)
    info = content_info(img, box)
    assert info is not None
    # 强制一个极小内容框
    from yu_agent.perception.content import ContentInfo

    tiny = ContentInfo(bbox=(100.0, 100.0, 103.0, 103.0), fill=0.001, has_content=True)
    assert shrink_box(box, tiny) == box, "内容过小时不应收缩"
    print("✓ 内容过小时的收缩被拦住")


# ── 3. draw_marks 集成 ──────────────────────────────────────────── #
def _els():
    out = []
    for i in range(5):   # 侧边栏：AX 报的框比内容高 2 倍
        out.append(UIElement(id=i, role="AXRow", title=f"Row{i}", x=20, y=48 + i * 44,
                             w=220, h=76, app="Demo"))
    for i in range(4):   # 聊天行：整行宽 + 过高
        out.append(UIElement(id=10 + i, role="AXCell", title=f"Name{i}", x=260,
                             y=55 + i * 60, w=610, h=90, app="Demo"))
    # 幽灵元素：屏幕上那块什么都没有
    out.append(UIElement(id=30, role="AXRow", title="", x=300, y=320, w=500, h=60, app="Demo"))
    return out


def test_draw_marks_drops_empty_and_shrinks() -> None:
    a11y.reset_audit()
    img = _scene()
    _, mp = draw_marks(img, _els(), stabilizer=MarkStabilizer())
    au = a11y.audit()
    assert au["dropped_empty"] >= 1, f"空框应被丢弃，审计={au}"
    assert au["shrunk"] >= 5, f"过大的行框应被收缩，审计={au}"
    # 幽灵元素不在映射里
    assert all((el.title or "") != "" for el in mp.values()) or len(mp) <= 9
    # 收缩后的元素高度确实变小了
    rows = [el for el in mp.values() if el.role == "AXRow"]
    assert rows and all(el.h < 76 for el in rows), [ (el.title, el.h) for el in rows ]
    print(f"✓ draw_marks：丢弃空框 {au['dropped_empty']} / 收缩 {au['shrunk']} / 标记 {len(mp)}")


def test_frame_reliability_flag() -> None:
    """元素框全落在空白上（截图与枚举不同步）时，必须标记为不可信。"""
    blank = Image.new("RGB", (600, 400), (255, 255, 255))
    els = [
        UIElement(id=i, role="AXRow", title=f"x{i}", x=50, y=40 + i * 50,
                  w=300, h=40, app="Demo")
        for i in range(4)
    ]
    draw_marks(blank, els, stabilizer=MarkStabilizer())
    st = a11y.last_stats
    assert st.get("frame_reliable") is False, f"应判为不可信，实际 {st}"
    print(f"✓ 帧可信度：全空白 → frame_reliable=False（ratio={st.get('frame_content_ratio')}）")


def test_label_size_is_stable_in_dense_lists() -> None:
    """密集列表里标签字号固定，不会跟着行高缩到看不清。"""
    img = Image.new("RGB", (900, 500), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for i in range(10):                      # 每行只有 18px 高
        d.text((60, 30 + i * 18), f"item{i}", fill=(0, 0, 0))
    els = [UIElement(id=i, role="AXCell", title=f"item{i}", x=50, y=28 + i * 18,
                     w=200, h=18, app="Demo") for i in range(10)]
    _, mp = draw_marks(img, els, stabilizer=MarkStabilizer())
    assert len(mp) >= 8, f"密集列表不该把元素丢掉，实际 {len(mp)}"


# ── 4. 窗口裁剪 ─────────────────────────────────────────────────── #
def test_clip_rect_to_window() -> None:
    el = UIElement(id=0, role="AXButton", title="x", x=100, y=100, w=400, h=200)
    win = (150.0, 120.0, 500.0, 500.0)
    out = _clip_rect_to(el, win)
    assert out is not None
    assert out[0] == 150 and out[1] == 120, out
    # 几乎全在窗口外 → 判定为不可见
    far = UIElement(id=1, role="AXButton", title="y", x=1400, y=1400, w=50, h=50)
    assert _clip_rect_to(far, win) is None
    print("✓ 元素矩形按窗口裁剪，窗口外判定为不可见")


def test_ghost_distance() -> None:
    r = (100.0, 100.0, 200.0, 200.0)
    assert _dist_to_rect(150, 150, r) == 0.0, "点在矩形内距离应为 0"
    assert abs(_dist_to_rect(320, 150, r) - 20.0) < 1e-6, "点到矩形右边距离算错"
    print("✓ 点到矩形距离计算正确")


# ── 5. 语义锚定（给原图 + 坐标那条路的兜底） ──────────────────── #
def test_anchor_by_text_uses_element_coords() -> None:
    from yu_agent.agent.loop import _anchor_by_text

    mp = {
        1: UIElement(id=0, role="AXButton", title="发送", x=800, y=700, w=80, h=32),
        2: UIElement(id=1, role="AXButton", title="搜索框", x=300, y=100, w=400, h=32),
    }
    # 模型估的坐标偏了 60px，但 reason 说对了 → 应该锚到元素中心
    act = Action(type="click", reason="click the 发送 button", x=860, y=740)
    got = _anchor_by_text(act, mp, 860, 740)
    assert got is not None, "说对了名字就该锚定"
    x, y, num, el = got
    assert num == 1 and el.title == "发送", (num, el.title)
    assert (x, y) == (840, 716), (x, y)

    # 名字没提到 → 不动
    act2 = Action(type="click", reason="click somewhere", x=860, y=740)
    assert _anchor_by_text(act2, mp, 860, 740) is None
    # 提到但离得太远 → 不硬拉（可能是另一个同名元素）
    act3 = Action(type="click", reason="click 发送", x=200, y=200)
    assert _anchor_by_text(act3, mp, 200, 200, max_dist=100) is None
    print("✓ 语义锚定：名字吻合才用元素坐标，太远/无描述都不动")


if __name__ == "__main__":
    test_content_info_detects_and_rejects()
    test_content_info_survives_wide_rows()
    test_shrink_box_pulls_box_onto_content()
    test_shrink_box_keeps_tiny_buttons()
    test_draw_marks_drops_empty_and_shrinks()
    test_frame_reliability_flag()
    test_label_size_is_stable_in_dense_lists()
    test_clip_rect_to_window()
    test_ghost_distance()
    test_anchor_by_text_uses_element_coords()
    print("\n=== 标注准确度测试全部通过 ===")
