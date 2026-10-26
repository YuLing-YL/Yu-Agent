"""OCR 坐标锚定：模型直接给像素坐标、但附近没有已标记元素时的最后一道兜底。

为什么需要：模型按原图估坐标常差几十到上百像素（点偏一点就点空）。已有两条
纠正路径——编号表、编号语义匹配；但当目标在 AX 里压根不存在（Electron / Canvas /
图片按钮），那两条都用不上，这时用 OCR 把「模型说它想点的文字」在屏幕上找出来，
用文字的真实位置替代它估的坐标。

只在**兜底路径**上调用（一次 ~0.3s），正常用编号时完全不触发，不影响主流程耗时。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from ..perception import ocr


@dataclass
class Anchor:
    x: int
    y: int
    text: str
    confidence: float


# 「点击『搜索』」「点一下"发送"按钮」这类引号里的就是目标名
_QUOTED = re.compile(r"[「『\"“']([^」』\"”']{2,40})[」』\"”']")
# 动词/介词前缀，去掉后剩下的更像目标名
_LEAD = re.compile(
    r"^(?:请|然后|接着|再)?\s*(?:点击|点一下|点|单击|双击|右键|按下|选择|打开|切换到|输入|"
    r"填写|搜索|查看|click|press|open|type|select|choose|tap)\s*",
    re.IGNORECASE,
)
_STOP = {
    "按钮", "图标", "区域", "位置", "地方", "这里", "那里", "这个", "那个", "一下",
    "button", "icon", "area", "field", "the", "and", "then",
}


def target_candidates(reason: str, limit: int = 5) -> List[str]:
    """从「模型说它要干什么」里抽出可能的目标文字，按可信度排序。"""
    text = (reason or "").strip()
    if not text:
        return []

    out: List[str] = []
    for m in _QUOTED.finditer(text):
        word = m.group(1).strip()
        if word and word.lower() not in _STOP:
            out.append(word)

    cleaned = _LEAD.sub("", text)
    for part in re.split(r"[，,。;；：:、\s（）()\[\]【】]+", cleaned):
        part = part.strip().strip("。.!！?？")
        if 2 <= len(part) <= 24 and part.lower() not in _STOP:
            out.append(part)

    seen = set()
    uniq: List[str] = []
    for w in out:
        key = w.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(w)
    return uniq[:limit]


def ocr_anchor(
    reason: str,
    img: Any,
    near: Optional[Tuple[float, float]] = None,
    max_distance: float = 320.0,
    enabled: bool = True,
) -> Optional[Anchor]:
    """把 reason 里提到的文字在图上找出来，返回它的中心点。

    near: 模型给的坐标。命中位置离它太远（> max_distance）就放弃——
    屏幕另一头有同样的字，多半不是它想点的那个。
    """
    if not enabled or img is None:
        return None
    if not ocr.backend_name():
        return None

    for cand in target_candidates(reason):
        try:
            box = ocr.find_text(img, cand, min_score=0.78)
        except Exception:
            continue
        if box is None:
            continue
        if near is not None:
            dist = ((box.cx - near[0]) ** 2 + (box.cy - near[1]) ** 2) ** 0.5
            if dist > max_distance:
                continue
        return Anchor(
            x=int(round(box.cx)),
            y=int(round(box.cy)),
            text=box.text,
            confidence=float(box.confidence),
        )
    return None
