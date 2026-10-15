"""OCR 文本提取：辅助定位文字（可选，需 pytesseract）。"""
from __future__ import annotations

from PIL import Image


def extract_text(img: Image.Image, lang: str = "eng") -> str:
    """从图像提取文字（需安装 pytesseract + tesseract）。"""
    try:
        import pytesseract
    except ImportError:
        return ""
    try:
        return pytesseract.image_to_string(img, lang=lang)
    except Exception:
        return ""
