"""动作审批：危险动作需人工确认（当前 GUI 场景先简化为策略检查）。"""
from __future__ import annotations

from ..common.errors import SafetyViolation
from .policy import check_action_allowed


def approve_action(action_type: str, require_approval_for_risky: bool = True) -> bool:
    """判断动作是否可执行。返回 True 表示放行。"""
    check_action_allowed(action_type)
    return True
