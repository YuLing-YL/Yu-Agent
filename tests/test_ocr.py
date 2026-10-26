"""OCR 回归测试：macOS Vision 文字识别 + 坐标锚定。

改造前的现状：OCR 是 pytesseract 桩（本机根本没装），`click_text` 技能
永远返回「未安装」，AX 读不到的界面（Electron/Canvas/图片按钮）完全无解。

这里的断言锁三件事：
1. Vision 真的能识别中英混排，并给出**左上原点**的像素坐标（不是 Vision 的
   左下归一化坐标，那个直接拿去点会点反）；
2. `find_text` 能在「搜索 ⌘K」这种带装饰的文字里命中「搜索」；
3. `ocr_anchor` 只在命中位置离模型给的坐标不远时才采纳（避免点到屏幕另一头
   同样文字的地方）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw, ImageFont

from yu_agent.agent.anchor import ocr_anchor, target_candidates
from yu_agent.perception import ocr

_FONT_CJK = "/System/Library/Fonts/Hiragino Sans GB.ttc"
_FONT_EN = "/System/Library/Fonts/Helvetica.ttc"


def _sample() -> Image.Image:
    """画一张中英混排的「界面」：左下角放设置，右上角放搜索。"""
    im = Image.new("RGB", (900, 500), (252, 252, 253))
    d = ImageDraw.Draw(im)
    cjk = ImageFont.truetype(_FONT_CJK, 34)
    en = ImageFont.truetype(_FONT_EN, 28)
    d.rectangle([40, 40, 860, 120], outline=(210, 210, 215))
    d.text((60, 70), "搜索 今天天气", font=cjk, fill=(20, 20, 20))
    d.text((640, 70), "Settings", font=en, fill=(20, 20, 20))
    d.text((60, 400), "设置", font=cjk, fill=(20, 20, 20))
    d.text((60, 440), "取消   确定", font=cjk, fill=(20, 20, 20))
    return im


def test_ocr_backend_available() -> None:
    name = ocr.backend_name()
    assert name, "这台机器应该能用 macOS Vision OCR"
    print(f"✓ OCR 后端：{name}")


def test_recognize_returns_topleft_pixels() -> None:
    if not ocr.backend_name():
        print("（跳过：没有 OCR 后端）")
        return
    im = _sample()
    boxes = ocr.recognize(im)
    assert boxes, "应识别出文字"
    # 「搜索 今天天气」在上半部分（y≈70），坐标必须是左上原点
    search = next((b for b in boxes if "搜索" in b.text or "天气" in b.text), None)
    assert search is not None, [b.text for b in boxes]
    assert 0 < search.y < 250, f"y 应在上半部分（左上原点），实际 {search.y}"
    assert 0 < search.x < 500, f"x 应在左侧，实际 {search.x}"
    print(f"✓ 识别 {len(boxes)} 块文字，坐标是左上原点像素")


def test_find_text_matches_decorated_label() -> None:
    if not ocr.backend_name():
        return
    im = _sample()
    box = ocr.find_text(im, "今天天气")
    assert box is not None and "天气" in box.text, box
    box2 = ocr.find_text(im, "Settings")
    assert box2 is not None, "英文也要能命中"
    assert ocr.find_text(im, "根本不存在的词") is None
    print("✓ 带装饰的界面文字能命中；不存在的词返回 None")


def test_target_candidates_extraction() -> None:
    cands = target_candidates("点击「发送」按钮，发送这条消息")
    assert "发送" in cands, cands
    cands2 = target_candidates('click the "Send" button')
    assert any("Send" in c for c in cands2), cands2
    assert target_candidates("") == []
    print("✓ 从 reason 里抽目标名（中英引号都认）")


def test_ocr_anchor_respects_distance() -> None:
    if not ocr.backend_name():
        return
    im = _sample()
    # 「设置」在左下 (60,400) 附近：给一个附近的坐标 → 应命中
    near = ocr_anchor("点击「设置」", im, near=(80, 410))
    assert near is not None, "近距离应锚定成功"
    assert near.y > 350, f"应锚定到左下角那个「设置」，实际 {near}"

    # 同上，但模型给的点在屏幕另一头 → 应该放弃（那多半不是它想点的）
    far = ocr_anchor("点击「设置」", im, near=(800, 60), max_distance=200)
    assert far is None, f"距离过远应放弃，实际 {far}"
    print("✓ OCR 锚定：近距离采纳、远距离放弃")


if __name__ == "__main__":
    test_ocr_backend_available()
    test_recognize_returns_topleft_pixels()
    test_find_text_matches_decorated_label()
    test_target_candidates_extraction()
    test_ocr_anchor_respects_distance()
    print("\nALL PASS")
