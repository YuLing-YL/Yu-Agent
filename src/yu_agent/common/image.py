"""图像处理工具：缩放、base64、差分、感知哈希。"""
from __future__ import annotations

import base64
import io
from typing import Tuple

from PIL import Image


def scale_image(img: Image.Image, max_side: int) -> Image.Image:
    """等比缩放，最长边不超过 max_side（用于控制 token 成本）。"""
    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img
    ratio = max_side / longest
    new_size = (int(w * ratio), int(h * ratio))
    return img.resize(new_size, Image.LANCZOS)


def scale_factor(img: Image.Image, scaled: Image.Image) -> Tuple[float, float]:
    """计算缩放比例：原图尺寸 / 缩放图尺寸，用于把模型坐标还原回原图坐标。"""
    return img.size[0] / scaled.size[0], img.size[1] / scaled.size[1]


def image_to_base64(img: Image.Image, fmt: str = "png") -> str:
    """PIL 图像 -> base64 字符串（不含 data: 前缀）。"""
    buf = io.BytesIO()
    img.save(buf, format=fmt.upper())
    return base64.b64encode(buf.getvalue()).decode("ascii")


def image_to_data_url(img: Image.Image, fmt: str = "png") -> str:
    """PIL 图像 -> data URL（可直接喂给 VLM）。"""
    b64 = image_to_base64(img, fmt)
    return f"data:image/{fmt.lower()};base64,{b64}"


def bytes_to_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def image_diff_ratio(a: Image.Image, b: Image.Image) -> float:
    """两张同尺寸图像的变化像素占比（0~1），用于校验动作是否生效。"""
    if a.size != b.size:
        return 1.0
    pa = a.convert("L")
    pb = b.convert("L")
    da = pa.getdata()
    db = pb.getdata()
    diff = sum(1 for x, y in zip(da, db) if abs(x - y) > 12)
    return diff / (a.size[0] * a.size[1])


def average_hash(img: Image.Image, size: Tuple[int, int] = (16, 16)) -> int:
    """感知哈希（用于记忆里去重 / 判断是否回到相同界面）。"""
    g = img.convert("L").resize(size, Image.LANCZOS)
    pixels = list(g.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for p in pixels:
        bits = (bits << 1) | (1 if p > avg else 0)
    return bits
