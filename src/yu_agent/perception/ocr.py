"""OCR 统一入口：优先 macOS Vision，退而用 pytesseract，都没有就老实说不可用。

对外只暴露四件事：
- `backend_name()`：现在能用哪种 OCR（""=都不可用）；
- `recognize()`：整图识别，拿到「文字 + 位置」；
- `find_text()`：按文字找位置（坐标回退与 click_text 技能都靠它）；
- `screen_text()`：只取文字（给「读屏幕上写了什么」这类任务）。

注意：这里返回的坐标是**图像像素坐标**。调用方若是 Retina（截图 2x），必须
除以缩放比换回逻辑像素再点击——历史上直接拿 pyautogui 截图坐标点击，
点到的位置正好是目标的两倍远。
"""
from __future__ import annotations

import difflib
from typing import List, Optional, Sequence

from PIL import Image

from . import vision_ocr
from .vision_ocr import TextBox


def backend_name() -> str:
    """当前可用的 OCR 后端名；都不可用返回空串。"""
    if vision_ocr.available():
        return "vision"
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return ""
    return "tesseract"


def recognize(
    img: Image.Image,
    lang: str = "",
    fast: bool = False,
    min_confidence: float = 0.2,
) -> List[TextBox]:
    """识别整张图。返回空列表表示没识别到 / 不可用（调用方自行降级）。"""
    if vision_ocr.available():
        # Vision 的语言是 BCP-47（zh-Hans/en-US），传进来的 tesseract 命名不做映射，
        # 直接用默认「中文+英文」——GUI 界面上这两种混排占绝大多数。
        langs: Optional[Sequence[str]] = None
        return vision_ocr.recognize(
            img, languages=langs, fast=fast, min_confidence=min_confidence
        )

    try:
        import pytesseract
    except Exception:
        return []

    try:
        data = pytesseract.image_to_data(
            img, lang=lang or "eng", output_type=pytesseract.Output.DICT
        )
    except Exception:
        return []

    boxes: List[TextBox] = []
    for i, word in enumerate(data.get("text") or []):
        word = str(word or "").strip()
        if not word:
            continue
        try:
            conf = float(data["conf"][i]) / 100.0
        except (TypeError, ValueError, KeyError):
            conf = 0.0
        if conf < min_confidence:
            continue
        boxes.append(
            TextBox(
                text=word,
                x=float(data["left"][i]),
                y=float(data["top"][i]),
                w=float(data["width"][i]),
                h=float(data["height"][i]),
                confidence=conf,
            )
        )
    return boxes


def _norm(s: str) -> str:
    """比对用归一化：只留字母数字并转小写。"""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def find_text(
    img: Image.Image,
    needle: str,
    lang: str = "",
    min_score: float = 0.72,
) -> Optional[TextBox]:
    """在图上找包含 needle 的那段文字，返回最匹配的框。

    匹配顺序：完全相等 > 包含 > 相似度。界面文字常带省略号/图标符号，
    所以不能只做 `in` 判断（"搜索  ⌘K" 里搜 "搜索" 要能命中）。
    """
    target = _norm(needle)
    if not target:
        return None
    boxes = recognize(img, lang=lang)
    if not boxes:
        return None

    best: Optional[TextBox] = None
    best_score = 0.0
    for b in boxes:
        cand = _norm(b.text)
        if not cand:
            continue
        if cand == target:
            return b
        if target in cand:
            score = 0.95 + min(0.04, len(target) / max(1, len(cand)) * 0.04)
        else:
            score = difflib.SequenceMatcher(None, target, cand).ratio()
        # 识别置信度也计入，避免挑到一段模糊乱码
        score *= 0.8 + 0.2 * max(0.0, min(1.0, b.confidence))
        if score > best_score:
            best, best_score = b, score
    return best if best_score >= min_score else None


def screen_text(img: Image.Image, max_chars: int = 4000, lang: str = "") -> str:
    """把整屏文字按「从上到下、从左到右」拼成一段（供模型阅读）。"""
    boxes = recognize(img, lang=lang)
    if not boxes:
        return ""
    rows: List[TextBox] = sorted(boxes, key=lambda b: (round(b.cy / 12), b.x))
    out: List[str] = []
    line: List[str] = []
    last_row = None
    for b in rows:
        row = round(b.cy / 12)
        if last_row is not None and row != last_row:
            out.append(" ".join(line))
            line = []
        line.append(b.text)
        last_row = row
    if line:
        out.append(" ".join(line))
    return "\n".join(out)[:max_chars]


def extract_text(img: Image.Image, lang: str = "eng") -> str:
    """兼容旧接口：只要一段文字。"""
    return screen_text(img, lang=lang)
