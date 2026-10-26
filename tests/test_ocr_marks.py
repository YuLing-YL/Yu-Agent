"""OCR 补标注回归测试：蓝色框的过滤 / 去重 / 补名字 / 预算分配。

背景（用户实测）：AX 只认得「声明过自己是什么」的元素，Electron/网页/自绘界面
里红框几乎画不出来，图上一片空白；而 OCR 从像素里读出来的蓝框位置是准的。
这里锁死四件事：

1. `elements_from_boxes` 会把噪声（超长正文、纯符号、太小、菜单栏、低置信度）挡掉，
   并把坐标从图像像素换回逻辑像素（否则 Retina 上点两倍远）；
2. 已经被 AX 红框盖住的文字不再重复编号（否则同一个按钮出现两个编号）；
3. AX 里没名字的图标按钮，用框内的 OCR 文字补名字；
4. 只枚举一个应用时，元素预算**全部**给这个应用（历史 bug：只给一半 → 标注不全）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from yu_agent.perception import ocr_marks
from yu_agent.perception.a11y import UIElement, _app_budgets
from yu_agent.perception.som import describe_marks
from yu_agent.perception.vision_ocr import TextBox


def box(text: str, x: float, y: float, w: float = 80, h: float = 24,
        conf: float = 0.9) -> TextBox:
    return TextBox(text=text, x=x, y=y, w=w, h=h, confidence=conf)


def app_el(title: str, x: float, y: float, w: float = 100, h: float = 30,
           role: str = "AXButton") -> UIElement:
    return UIElement(id=0, role=role, title=title, x=x, y=y, w=w, h=h, app="访达", kind="app")


# ── 1. 过滤 + 坐标换算 ──────────────────────────────────────────── #
def test_filters_noise_and_converts_coords() -> None:
    """超长正文 / 纯符号 / 太小的框 / 菜单栏 都要挡掉；坐标按 coord_scale 换回逻辑像素。"""
    boxes = [
        box("搜索", 200, 300, 60, 24),                      # ✓ 正常
        box("这是一段很长的正文" * 6, 100, 400, 400, 24),    # 超长正文
        box("——", 100, 500, 40, 20),                        # 纯符号
        box("设置", 100, 600, 4, 24),                       # 太窄
        box("文件", 100, 0, 60, 20),                        # 菜单栏（skip_top 命中，图像像素）
        box("确定", 300, 700, 60, 24, conf=0.10),           # 低置信度（调用方传 min_conf 前已过滤）
    ]
    els = ocr_marks.elements_from_boxes(
        boxes, coord_scale=0.5, skip_top_px=26 * 0.5, max_boxes=30
    )
    texts = [e.title for e in els]
    assert "搜索" in texts, texts
    assert not any("很长的正文" in t for t in texts), texts
    assert "——" not in texts, texts
    assert "设置" not in texts, "太窄的框应该被丢掉"
    assert "文件" not in texts, "菜单栏文字应该被跳过"

    hit = next(e for e in els if e.title == "搜索")
    assert (hit.x, hit.y, hit.w, hit.h) == (400, 600, 120, 48), (hit.x, hit.y, hit.w, hit.h)
    assert all(e.kind == "ocr" and e.role == ocr_marks.OCR_ROLE for e in els)
    print(f"✓ 过滤后剩 {len(els)} 个，坐标已换算：{hit.x},{hit.y}")


def test_cap_keeps_high_confidence() -> None:
    """数量超额时先保住置信度高的，再按阅读顺序排。"""
    boxes = [box(f"按钮{i}", 10, 10 + i * 30, 60, 22, conf=0.5 + i * 0.01) for i in range(10)]
    els = ocr_marks.elements_from_boxes(boxes, max_boxes=3)
    assert len(els) == 3, len(els)
    assert {e.title for e in els} == {"按钮7", "按钮8", "按钮9"}, {e.title for e in els}
    print("✓ 超额时保留置信度最高的 3 个")


# ── 2. 与 AX 框去重 ────────────────────────────────────────────── #
def test_drop_covered_by_a11y_box() -> None:
    """文字落在已有 AX 框里 → 不重复编号；落在空白处 → 保留。"""
    existing = [app_el("登录按钮", 100, 100, 120, 40)]
    cands = [
        ocr_marks.elements_from_boxes([box("登录", 120, 110, 60, 20)])[0],   # 在按钮内
        ocr_marks.elements_from_boxes([box("忘记密码", 100, 400, 80, 20)])[0],
    ]
    kept = ocr_marks.drop_covered(cands, existing)
    assert [k.title for k in kept] == ["忘记密码"], [k.title for k in kept]
    print("✓ 被 AX 框盖住的 OCR 文字已去重")


# ── 3. 给无名元素补名字 ─────────────────────────────────────────── #
def test_fill_titles_for_unnamed_icons() -> None:
    """工具栏纯图标按钮（AX 无 title）用框内文字补名字；有名字的不动。"""
    els = [
        app_el("", 200, 200, 40, 40),        # 图标按钮，下面写着「保存」
        app_el("打印", 300, 200, 40, 40),    # 已有名字
    ]
    boxes = [box("保存", 200, 236, 40, 16)]  # 图标下方，落在 pad=8 的外扩范围内
    out = ocr_marks.fill_titles(els, boxes)
    assert out[0].title == "保存", out[0].title
    assert out[1].title == "打印", out[1].title
    # 不能改到原对象（上层还在用同一份列表做审计）
    assert els[0].title == "", "fill_titles 不应该原地修改元素"
    print("✓ 无名图标补到名字：保存")


# ── 4. 预算分配（标注不全的根因） ──────────────────────────────── #
def test_app_budget_all_goes_to_single_app() -> None:
    """只枚举 1 个应用时，预算全部给它——不是一半。"""
    assert _app_budgets(60, 1) == [60], _app_budgets(60, 1)
    assert _app_budgets(40, 1) == [40], _app_budgets(40, 1)
    multi = _app_budgets(90, 3)
    assert len(multi) == 3 and multi[0] > multi[1], multi
    assert sum(multi) >= 90 * 0.8, multi
    assert _app_budgets(10, 0) == []
    print(f"✓ 单应用预算=全部；多应用分配={multi}")


# ── 5. 编号表分组 ──────────────────────────────────────────────── #
def test_describe_marks_separates_ocr_group() -> None:
    """OCR 补的元素要单独成组，模型才知道「蓝色框只有文字」。"""
    mapping = {
        1: app_el("搜索框", 100, 100, 200, 30, role="AXTextField"),
        2: ocr_marks.elements_from_boxes([box("登录", 300, 300, 60, 20)])[0],
    }
    text = describe_marks(mapping, "zh")
    assert "屏幕文字(OCR)" in text, text
    assert "#2: 文字(OCR) | 登录" in text, text
    assert "蓝色框" in text, text
    print("✓ 编号表把 OCR 单独分组")


if __name__ == "__main__":
    test_filters_noise_and_converts_coords()
    test_cap_keeps_high_confidence()
    test_drop_covered_by_a11y_box()
    test_fill_titles_for_unnamed_icons()
    test_app_budget_all_goes_to_single_app()
    test_describe_marks_separates_ocr_group()
    print("all ocr-marks tests passed")
