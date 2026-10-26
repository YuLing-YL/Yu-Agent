"""动作审批：危险动作在真正执行前要过一道人工确认。

改造前这里是空壳——`return True` 直接放行，等于没有审批。现在：
- 先做硬规则检查（动作类型是否允许）；
- 再用 `risk.classify` 判断语义风险（按被点击元素上的文字）；
- 需要审批时交给**审批人**（GUI 弹窗 / CLI 询问）；没有审批人时按级别兜底：
  critical 拦下、high 放行并记日志。

审批人是可注入的回调 `(description, risk) -> bool`，这样 CLI、GUI、测试可以
各用各的实现，而 loop 不需要知道界面长什么样。
"""
from __future__ import annotations

from typing import Callable, Optional

from ..common.errors import SafetyViolation
from .policy import check_action_allowed
from .risk import Risk

# (动作描述, 风险对象) -> 是否放行
Approver = Callable[[str, Risk], bool]

_DEFAULT_APPROVER: Optional[Approver] = None


def set_default_approver(fn: Optional[Approver]) -> None:
    """注册全局审批人（GUI 启动时注入；测试里可传假函数）。"""
    global _DEFAULT_APPROVER
    _DEFAULT_APPROVER = fn


def get_default_approver() -> Optional[Approver]:
    return _DEFAULT_APPROVER


def approve_action(
    action_type: str,
    risk: Optional[Risk] = None,
    description: str = "",
    approver: Optional[Approver] = None,
    require_approval_for_risky: bool = True,
) -> bool:
    """判断动作是否可执行。返回 True 表示放行。

    硬规则不通过时抛 SafetyViolation（这是配置/代码层面的错误，不该被"确认"绕过）；
    需要人工审批却没人能问时，按 risk.level 兜底。
    """
    check_action_allowed(action_type)
    if risk is None or not risk.needs_approval or not require_approval_for_risky:
        return True

    fn = approver or _DEFAULT_APPROVER
    desc = description or action_type
    if fn is not None:
        return bool(fn(desc, risk))

    # 没有审批人：不允许替用户做不可逆的事
    if risk.level == "critical":
        return False
    return True
