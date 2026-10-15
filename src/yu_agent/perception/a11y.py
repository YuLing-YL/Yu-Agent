"""辅助功能树（a11y）：可选增强，从系统读取 UI 元素层级。"""
from __future__ import annotations


def get_a11y_tree(max_depth: int = 12, max_elements: int = 400) -> list:
    """获取当前前台应用的辅助功能树（占位，需平台 API）。"""
    # macOS 需 AXUIElement API，暂未实现，返回空列表
    return []
