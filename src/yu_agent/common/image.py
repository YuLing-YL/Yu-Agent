"""图像处理工具：缩放、base64、差分、感知哈希。"""
from __future__ import annotations

import base64
import io
from typing import Tuple, cast

from PIL import Image

# Pillow 9 → 10 把重采样常量挪进了 Image.Resampling，顶层 Image.LANCZOS
# 只是兼容别名（且没有类型声明）。项目要求 Pillow>=10，直接用新路径。
_LANCZOS = Image.Resampling.LANCZOS


def scale_image(img: Image.Image, max_side: int) -> Image.Image:
    """等比缩放，最长边不超过 max_side（用于控制 token 成本）。"""
    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img
    ratio = max_side / longest
    new_size = (int(w * ratio), int(h * ratio))
    return img.resize(new_size, _LANCZOS)


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
    g = img.convert("L").resize(size, _LANCZOS)
    pixels = list(g.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for p in pixels:
        bits = (bits << 1) | (1 if p > avg else 0)
    return bits


def is_blank(img: Image.Image, tolerance: int = 8) -> bool:
    """整张图是不是「一片纯色」（通常就是纯黑）。

    为什么需要：屏幕锁定 / 显示器休眠 / 没给录屏权限时，Quartz 抓到的是一张
    **全黑图** —— 不报错、不抛异常，模型看到黑屏然后照着黑屏瞎点。
    用户事后翻 `runs/` 只看到一堆黑图，会以为是保存坏了。
    这里用 `getextrema()`（C 实现，几毫秒）判断，命中就明确告诉用户原因。

    tolerance: 每个通道允许的抖动（压暗的壁纸不是严格 0）。
    """
    if img is None:
        return True
    try:
        # convert("RGB") 之后 getextrema() 一定返回 3 组 (min,max)；
        # stub 把它标成「单通道 tuple 或嵌套 tuple」的联合类型，这里显式收窄。
        ext = cast("Tuple[Tuple[int, int], ...]", img.convert("RGB").getextrema())
    except Exception:
        return False
    # 纯色的判据：每个通道的 max-min 都很小，并且整体落在黑端或白端。
    # （不能用 max-min 大就判"有内容"——那正好是反的，早期写反过一次。）
    for lo, hi in ext:
        if hi - lo > tolerance:
            return False
        if lo > tolerance and hi < 255 - tolerance:
            return False
    return True
