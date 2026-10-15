"""文本匹配：用于按文字定位元素（辅助定位）。"""
from __future__ import annotations

from difflib import SequenceMatcher


def text_similarity(a: str, b: str) -> float:
    """两段文本的相似度（0~1）。"""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def match_text(target: str, candidates: list[str], threshold: float = 0.62) -> int | None:
    """在候选文本里找最匹配 target 的索引，低于阈值返回 None。"""
    best_idx = None
    best_score = 0.0
    for i, c in enumerate(candidates):
        s = text_similarity(target, c)
        if s > best_score:
            best_score = s
            best_idx = i
    if best_score >= threshold:
        return best_idx
    return None
