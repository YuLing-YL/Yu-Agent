"""macOS Vision 原生 OCR：把截图上的文字连同位置一起读出来。

为什么需要它（而不是继续用 pytesseract）：AX 只认得「声明过自己是什么」的元素，
Electron / Canvas / 图片型界面 / 自绘列表它一律读不到，agent 就瞎了。OCR 是这条
盲区的兜底通道。选 macOS 自带的 Vision 而不是 pytesseract：
- 零额外安装（pytesseract 还要装 tesseract 二进制，用户机器上根本没有）；
- 中文识别质量明显更好；
- 实测 ~0.3s/帧，比多一次模型调用便宜一个数量级。

坐标系约定（踩过坑，写清楚）：
Vision 的 boundingBox 是**归一化**且**原点在左下**；这里统一转换成
「图像左上角为原点」的像素坐标，调用方再按 coord_scale 换回逻辑像素。
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import List, Optional, Sequence

from PIL import Image


@dataclass
class TextBox:
    """一段被识别出来的文字及其位置（图像像素坐标，左上原点）。"""

    text: str
    x: float
    y: float
    w: float
    h: float
    confidence: float = 0.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def __str__(self) -> str:  # 便于日志
        return f"「{self.text}」({self.x:.0f},{self.y:.0f},{self.w:.0f}x{self.h:.0f})"


_DEFAULT_LANGS = ("zh-Hans", "en-US")


def available() -> bool:
    """Vision 框架是否可用（macOS + pyobjc-framework-Vision）。"""
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
    except Exception:
        return False
    return True


def _to_cgimage(img: Image.Image):
    """PIL -> CGImage（走 PNG，保持无损；不依赖 Quartz 的屏幕抓取）。"""
    import Quartz

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    provider = Quartz.CGDataProviderCreateWithCFData(data)
    return Quartz.CGImageCreateWithPNGDataProvider(
        provider, None, True, Quartz.kCGRenderingIntentDefault
    )


def recognize(
    img: Image.Image,
    languages: Optional[Sequence[str]] = None,
    fast: bool = False,
    min_confidence: float = 0.2,
) -> List[TextBox]:
    """识别整张图的文字。任何异常都返回空列表（OCR 只是兜底，不能拖垮主流程）。"""
    if not available():
        return []
    try:
        import Vision

        cg = _to_cgimage(img)
        if cg is None:
            return []
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        level = (
            Vision.VNRequestTextRecognitionLevelFast
            if fast
            else Vision.VNRequestTextRecognitionLevelAccurate
        )
        req.setRecognitionLevel_(level)
        # 关闭语言纠错：界面上常是短词/品牌名，纠错反而改错（"访达"→"访 达"）
        req.setUsesLanguageCorrection_(False)
        try:
            req.setRecognitionLanguages_(list(languages or _DEFAULT_LANGS))
        except Exception:
            pass  # 老系统可能不认这个语言组合，用默认即可

        handler.performRequests_error_([req], None)
        results = req.results() or []
    except Exception:
        return []

    W, H = img.size
    boxes: List[TextBox] = []
    for obs in results:
        try:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            text = str(cands[0].string() or "").strip()
            conf = float(cands[0].confidence())
            if not text or conf < min_confidence:
                continue
            bb = obs.boundingBox()
            ox, oy = float(bb.origin.x), float(bb.origin.y)
            bw, bh = float(bb.size.width), float(bb.size.height)
            boxes.append(
                TextBox(
                    text=text,
                    x=ox * W,
                    y=(1.0 - oy - bh) * H,   # 左下原点 → 左上原点
                    w=bw * W,
                    h=bh * H,
                    confidence=conf,
                )
            )
        except Exception:
            continue
    return boxes
