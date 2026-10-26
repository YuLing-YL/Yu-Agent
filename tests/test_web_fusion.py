"""网页三模态融合回归测试：DOM 布局 + AXTree + 截图 OCR 的对齐与裁决。

不依赖真实浏览器：CDP 的返回结构是稳定的 JSON，这里用**手写的 fixture** 还原
`DOMSnapshot.captureSnapshot` / `Accessibility.getFullAXTree` 的真实形状，
把最难的几种情况固定成用例：

1. `aria-hidden` 的元素 AX 里没有 → 必须靠视觉/OCR，不能当它不存在；
2. AX 合成节点（iframe document）没有 backendDOMNodeId → 几何从祖先继承；
3. 弹窗遮挡 → 视觉优先，目标淘汰并记冲突；
4. `aria-label` 与画面文字不一致 → 采信画面上的字；
5. DOM 说 disabled、像素上却清晰可读 → 记冲突（aria-disabled 常与样式脱节）；
6. 虚拟列表滚出视口的残留节点 → 不进候选；
7. shadow DOM 里的节点要能标出来（CDP 给的是拍平树）；
8. 融合结果能直接喂给 SOM（`build_som_mapping(elements=…)`）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw

from yu_agent.perception.vision_ocr import TextBox
from yu_agent.perception.web import fusion as fusion_mod
from yu_agent.perception.web.align import align
from yu_agent.perception.web.axtree import ancestor_with_backend, parse_ax_tree
from yu_agent.perception.web.collect import build
from yu_agent.perception.web.layout import candidates, parse_snapshot
from yu_agent.perception.web.visual import check, css_rect_to_image

# ── fixture 构造：尽量贴近 CDP 真实返回（字符串池 + 下标）───────────── #
_STRINGS: list = []
_SIDX: dict = {}


def S(s: str) -> int:
    """往字符串池里放一个字符串，返回下标（CDP 就是这么存的）。"""
    if s not in _SIDX:
        _STRINGS.append(s)
        _SIDX[s] = len(_STRINGS) - 1
    return _SIDX[s]


def reset_pool() -> None:
    _STRINGS.clear()
    _SIDX.clear()


def make_snapshot(nodes: list) -> dict:
    """nodes: [(tag, backend_id, x, y, w, h, text, attrs_dict, clickable, style_dict, shadow_host)]

    返回一个 DOMSnapshot.captureSnapshot 形状的 payload（单 frame）。
    """
    reset_pool()
    parent, node_type, name, backend, attrs, clickable_idx = [], [], [], [], [], []
    layout_idx, bounds, styles, text_idx, text_val = [], [], [], [], []
    shadow_idx, shadow_val = [], []

    for i, n in enumerate(nodes):
        tag, bid, x, y, w, h, text, at, click, st, is_shadow = (
            n + (False,) * (11 - len(n))
        )[:11]
        parent.append(i - 1 if i else -1)
        node_type.append(1)
        name.append(S(tag.upper()))
        backend.append(bid)
        flat = []
        for k, v in (at or {}).items():
            flat += [S(k), S(str(v))]
        attrs.append(flat)
        if click:
            clickable_idx.append(i)
        if is_shadow:
            # shadow host：这一项在 shadowRootType 里出现，它的**后代**才算 shadow 内
            shadow_idx.append(i)
            shadow_val.append("open")
        layout_idx.append(i)
        bounds.append([float(x), float(y), float(w), float(h)])
        flat_st = []
        for k, v in (st or {}).items():
            flat_st += [S(k), S(str(v))]
        styles.append(flat_st)
        if text:
            text_idx.append(i)
            text_val.append(S(text))

    return {
        "documents": [
            {
                "frameId": "MAIN",
                "nodes": {
                    "parentIndex": parent,
                    "nodeType": node_type,
                    "nodeName": name,
                    "backendNodeId": backend,
                    "attributes": attrs,
                    "isClickable": {"index": clickable_idx},
                    "shadowRootType": {"index": shadow_idx, "value": shadow_val},
                },
                "layout": {
                    "nodeIndex": layout_idx,
                    "bounds": bounds,
                    "styles": styles,
                    "text": {"index": text_idx, "value": text_val},
                },
            }
        ],
        "strings": _STRINGS,
    }


def make_ax(nodes: list) -> dict:
    """nodes: [(node_id, role, name, backend_id, ignored, props_dict, parent_id)]"""
    out = []
    for n in nodes:
        nid, role, name, bid, ignored, props, parent = (n + ("", None, None, None, False, None, ""))[:7]
        out.append({
            "nodeId": str(nid),
            "ignored": bool(ignored),
            "role": {"type": "role", "value": role},
            "name": {"type": "computedString", "value": name},
            "backendDOMNodeId": bid or 0,
            "properties": [
                {"name": k, "value": {"type": "boolean", "value": v}}
                for k, v in (props or {}).items()
            ],
            "childIds": [],
            "parentId": parent,
        })
    # 建父子关系（真实返回只有 childIds，这里从 parentId 反推）
    by_id = {n["nodeId"]: n for n in out}
    for n in out:
        p = n.get("parentId")
        if p and p in by_id:
            by_id[p]["childIds"].append(n["nodeId"])
    return {"nodes": out}


def page_img(w=1200, h=800) -> Image.Image:
    """画一张假页面：白底 + 一个按钮 + 一个输入框。"""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([40, 40, 240, 90], fill=(30, 90, 220))
    d.rectangle([40, 130, 440, 172], outline=(150, 150, 150))
    return im


def box(text: str, x: float, y: float, w: float = 80, h: float = 24) -> TextBox:
    return TextBox(text=text, x=x, y=y, w=w, h=h, confidence=0.95)


# ── 1. DOM 快照解析：可见性 / 虚拟列表 / shadow ───────────────────── #
def test_dom_snapshot_visibility_and_shadow() -> None:
    snap = make_snapshot([
        ("body", 1, 0, 0, 1200, 800, "", {}, False, {}, False),
        ("button", 2, 40, 40, 200, 50, "登录", {}, True, {"z-index": "1"}, False),
        ("div", 3, 0, 900, 400, 40, "隐藏", {}, False, {"display": "none"}, False),
        ("li", 4, 40, 5000, 200, 30, "滚出视口", {}, True, {}, False),
        # shadow host 在 index 5，子节点 6 在 shadow 里
        ("my-widget", 5, 400, 200, 300, 100, "", {}, False, {"z-index": "2"}, True),
        ("button", 6, 420, 220, 120, 40, "内部按钮", {}, True, {}, False),
    ])
    nodes = parse_snapshot(snap, viewport=(1200.0, 800.0))
    by_id = {n.backend_node_id: n for n in nodes}

    assert by_id[3].visible is False, "display:none 必须判为不可见"
    assert by_id[4].visible is False, "滚出视口的虚拟列表残留必须判为不可见"
    assert by_id[6].inside_shadow is True, "shadow 内节点要标出来"
    assert by_id[2].inside_shadow is False, "普通节点不能被误标成 shadow"
    assert by_id[2].z == 1, by_id[2].z
    cand = candidates(nodes)
    ids = {c.backend_node_id for c in cand}
    assert 2 in ids and 6 in ids and 3 not in ids and 4 not in ids, ids
    print(f"✓ DOM 解析：候选 {sorted(ids)}（隐藏/视口外已剔除，shadow 已标记）")


# ── 2. AX 解析 + 合成节点继承 ─────────────────────────────────────── #
def test_ax_parse_and_synthetic_inheritance() -> None:
    ax = make_ax([
        (1, "RootWebArea", "测试页", 1, False, {}, ""),
        (2, "button", "登录", 2, False, {}, "1"),
        (3, "generic", "", 0, True, {}, "1"),          # ignored 且没有 backend id
        (4, "textbox", "邮箱", 0, False, {}, "2"),     # 合成节点：没有 backend id
    ])
    nodes = parse_ax_tree(ax)
    assert nodes["2"].name == "登录" and nodes["2"].backend_node_id == 2
    assert nodes["3"].ignored is True
    assert nodes["4"].parent_id == "2", nodes["4"].parent_id
    host = ancestor_with_backend(nodes["4"], nodes)
    assert host is not None and host.backend_node_id == 2, "合成节点应从祖先继承几何锚点"
    print("✓ AX 解析：ignored 保留、合成节点从祖先继承 backend id")


# ── 3. 对齐：aria-hidden 与 name_mismatch ─────────────────────────── #
def test_align_flags() -> None:
    snap = make_snapshot([
        ("body", 1, 0, 0, 1200, 800, "", {}, False, {}, False),
        ("button", 2, 40, 40, 200, 50, "提交订单", {}, True, {}, False),
        ("div", 3, 40, 120, 200, 50, "看不见的按钮", {"aria-hidden": "true", "role": "button"},
         True, {}, False),
    ])
    ax = make_ax([
        (1, "RootWebArea", "", 1, False, {}, ""),
        (2, "button", "Submit order", 2, False, {}, "1"),   # 名字与画面文字不一致
        (3, "generic", "", 0, False, {}, "1"),
    ])
    dom = candidates(parse_snapshot(snap, viewport=(1200.0, 800.0)))
    res = align(dom, parse_ax_tree(ax))
    by_id = {t.dom.backend_node_id: t for t in res.targets}

    assert by_id[3].has("no_ax") and by_id[3].has("aria_hidden"), by_id[3].flags
    assert by_id[2].has("name_mismatch"), by_id[2].flags
    assert res.stats["no_ax"] == 1 and res.stats["aria_hidden"] == 1
    print(f"✓ 对齐：{res.stats}")


# ── 4. 视觉校验：遮挡 / 遮不住 / 文字匹配 ─────────────────────────── #
def test_visual_occlusion_and_text() -> None:
    snap = make_snapshot([
        ("button", 2, 40, 40, 200, 50, "登录", {}, True, {"z-index": "1"}, False),
        # 弹窗：z 更高、完全盖住按钮
        ("div", 9, 20, 20, 400, 200, "", {}, False, {"z-index": "99"}, False),
    ])
    nodes = parse_snapshot(snap, viewport=(1200.0, 800.0))
    dom = candidates(nodes)
    ax = parse_ax_tree(make_ax([(1, "button", "登录", 2, False, {}, "")]))
    res = align(dom, ax)

    img = page_img()
    drawn = ImageDraw.Draw(img)
    drawn.rectangle([20, 20, 420, 220], fill=(240, 240, 240))  # 弹窗把按钮盖掉
    cover = fusion_mod.covering_rects(nodes)
    v = check(res.targets[0], img, ocr_boxes=[box("登录", 60, 50)], covering=cover)
    assert v.occluded is True, v.notes
    print(f"✓ 视觉：弹窗遮挡被识别（{v.notes}）")

    # 没有被遮挡时不应误判
    img2 = page_img()
    v2 = check(res.targets[0], img2, ocr_boxes=[box("登录", 60, 50)], covering=[])
    assert v2.occluded is False, v2.notes
    assert (v2.text_match or 0) >= 0.55, v2.text_match
    print(f"✓ 视觉：无遮挡 + 文字一致（text={v2.text_match:.2f}）")


def test_visual_text_mismatch() -> None:
    """AX 名与画面文字不同 → 视觉应报 text_mismatch（不直接判错，交给融合裁决）。"""
    snap = make_snapshot([("button", 2, 40, 40, 200, 50, "提交订单", {}, True, {}, False)])
    ax = parse_ax_tree(make_ax([(1, "button", "Submit order", 2, False, {}, "")]))
    res = align(candidates(parse_snapshot(snap, viewport=(1200.0, 800.0))), ax)
    v = check(res.targets[0], page_img(), ocr_boxes=[box("提交订单", 60, 50)])
    assert v.text_match is not None and v.text_match < 0.6, v.text_match
    print(f"✓ 视觉：名字与画面不符被标记（text={v.text_match:.2f}）")


# ── 5. 融合：视觉优先 + 冲突记录 + 改名 ───────────────────────────── #
def test_fusion_visual_wins_on_occlusion() -> None:
    snap = make_snapshot([
        ("button", 2, 40, 40, 200, 50, "登录", {}, True, {"z-index": "1"}, False),
        ("div", 9, 20, 20, 400, 200, "", {}, False, {"z-index": "99"}, False),
    ])
    nodes = parse_snapshot(snap, viewport=(1200.0, 800.0))
    ax = parse_ax_tree(make_ax([(1, "button", "登录", 2, False, {}, "")]))
    res = align(candidates(nodes), ax)
    img = page_img()
    ImageDraw.Draw(img).rectangle([20, 20, 420, 220], fill=(240, 240, 240))
    v = check(res.targets[0], img, ocr_boxes=[], covering=fusion_mod.covering_rects(nodes))
    fused = fusion_mod.fuse(res.targets, {id(res.targets[0].dom): v})
    assert not fused.targets, "被遮挡的目标必须淘汰"
    assert fused.conflicts and fused.conflicts[0]["kind"] == "occluded", fused.conflicts
    print(f"✓ 融合：遮挡 → 淘汰 + 冲突 {fused.conflicts[0]}")


def test_fusion_prefers_visual_name() -> None:
    """aria-label 与画面文字不一致时，采信画面（用户就是这么认按钮的）。"""
    snap = make_snapshot([("button", 2, 40, 40, 200, 50, "提交订单", {}, True, {}, False)])
    ax = parse_ax_tree(make_ax([(1, "button", "Submit order", 2, False, {}, "")]))
    res = align(candidates(parse_snapshot(snap, viewport=(1200.0, 800.0))), ax)
    v = check(res.targets[0], page_img(), ocr_boxes=[box("提交订单", 60, 50)])
    fused = fusion_mod.fuse(res.targets, {id(res.targets[0].dom): v})
    assert fused.targets, "只是名字不一致，不该淘汰"
    assert fused.targets[0].name == "提交订单", fused.targets[0].name
    kinds = {c["kind"] for c in fused.conflicts}
    assert "name_mismatch" in kinds, kinds
    print(f"✓ 融合：采用画面文字「{fused.targets[0].name}」，冲突已记录")


def test_fusion_disabled_conflict() -> None:
    """DOM 说 disabled、画面上却正常 → 记冲突（aria-disabled 与样式脱节的经典情况）。"""
    snap = make_snapshot([
        ("button", 2, 40, 40, 200, 50, "登录", {"disabled": "", "aria-disabled": "true"},
         True, {}, False),
    ])
    ax = parse_ax_tree(make_ax([(1, "button", "登录", 2, False, {"disabled": True}, "")]))
    res = align(candidates(parse_snapshot(snap, viewport=(1200.0, 800.0))), ax)
    assert "disabled" in res.targets[0].flags, res.targets[0].flags
    v = check(res.targets[0], page_img(), ocr_boxes=[box("登录", 60, 50)])
    fused = fusion_mod.fuse(res.targets, {id(res.targets[0].dom): v})
    kinds = {c["kind"] for c in fused.conflicts}
    assert "disabled_conflict" in kinds, kinds
    print("✓ 融合：disabled 标记与像素不一致 → 记冲突（不直接改判）")


# ── 6. 坐标换算：CSS px → 截图 px ─────────────────────────────────── #
def test_css_to_image_coordinate_mapping() -> None:
    # 内容区原点 (100, 200)，页面缩放 1.25，Retina 2x
    r = css_rect_to_image((10, 20, 100, 40), origin=(100, 200), zoom=1.25, image_scale=2.0)
    assert r == (100 * 2 + 10 * 1.25 * 2,
                 200 * 2 + 20 * 1.25 * 2,
                 100 * 2 + 110 * 1.25 * 2,
                 200 * 2 + 60 * 1.25 * 2), r
    print(f"✓ 坐标换算：{r}")


# ── 7. 端到端 build()：三路合成 + 直接喂 SOM ─────────────────────── #
def test_build_end_to_end_and_som_injection() -> None:
    snap = make_snapshot([
        ("body", 1, 0, 0, 1200, 800, "", {}, False, {}, False),
        ("button", 2, 40, 40, 200, 50, "登录", {}, True, {"z-index": "1"}, False),
        ("input", 3, 40, 130, 400, 42, "", {"placeholder": "邮箱"}, True, {}, False),
    ])
    ax = make_ax([
        (1, "RootWebArea", "", 1, False, {}, ""),
        (2, "button", "登录", 2, False, {}, "1"),
        (3, "textbox", "邮箱", 3, False, {}, "1"),
    ])
    raw = {"snapshot": snap, "ax": ax, "metrics": {"cssLayoutViewport":
                                                  {"clientWidth": 1200, "clientHeight": 800}}}
    perc = build(
        raw,
        screenshot=page_img(),
        ocr_boxes=[box("登录", 60, 50, 60, 24), box("邮箱", 50, 140, 60, 20)],
        origin=(0, 0), zoom=1.0, image_scale=1.0,
    )
    assert len(perc.elements) >= 2, [e.title for e in perc.elements]
    names = [e.title for e in perc.elements]
    assert any("登录" in n for n in names), names
    assert perc.meta["fuse_fused"] >= 2, perc.meta

    # 融合结果直接注入 SOM：编号/画框逻辑完全复用，不需要改标注代码
    from yu_agent.perception.som import build_som_mapping

    marked, mapping, els = build_som_mapping(
        page_img(), max_elements=50, elements=perc.elements, content_check=False,
    )
    assert mapping, "注入模式没有产出编号"
    assert len(mapping) >= 2, len(mapping)
    assert all(mapping[n].kind == "web" for n in mapping)
    print(f"✓ 端到端：融合 {perc.meta['fuse_fused']} 个 → SOM 编号 {len(mapping)} 个")


if __name__ == "__main__":
    test_dom_snapshot_visibility_and_shadow()
    test_ax_parse_and_synthetic_inheritance()
    test_align_flags()
    test_visual_occlusion_and_text()
    test_visual_text_mismatch()
    test_fusion_visual_wins_on_occlusion()
    test_fusion_prefers_visual_name()
    test_fusion_disabled_conflict()
    test_css_to_image_coordinate_mapping()
    test_build_end_to_end_and_som_injection()
    print("all web-fusion tests passed")
