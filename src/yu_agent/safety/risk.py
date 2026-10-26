"""风险判定：这一步动作会不会造成不可逆后果。

单独成模块（而不是塞进 policy.py）的原因：policy 管「动作类型允不允许」这种硬规则，
这里管「这次点击的语义危不危险」——要结合目标元素上的文字判断（"删除"按钮和
"删除线"按钮完全是两码事）。两者判断依据、失败后果、需要的人工介入都不同。

分级：
- low      普通操作，直接做
- high     可能改坏数据（覆盖保存、清空、退出未保存、卸载）
- critical 不可逆且影响真实世界（删除、支付、发送消息、格式化）

默认行为：critical 无审批人时**拦下**（宁可不动，也不替用户删东西）；
high 无审批人时放行但打日志（否则无人值守场景下什么都干不了）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# 关键词 → (级别, 说明)。顺序无关，取最高级别。
_PATTERNS = [
    # critical：删了/发了/付了就回不来
    (r"删除|移除|移到废纸篓|清空废纸篓|格式化|抹掉|卸载|移除全部|delete|remove|erase|format|uninstall", "critical", "删除/卸载类操作"),
    (r"支付|付款|转账|下单|购买|确认订单|pay|purchase|checkout|transfer", "critical", "涉及真实资金"),
    (r"发送|发布|上传|提交订单|send|publish|post|submit order", "critical", "对外发送/发布"),
    (r"覆盖|替换全部|恢复出厂|重置|reset|overwrite|factory reset", "high", "可能覆盖已有内容"),
    # high：有代价但可挽回
    (r"清空|清除|重置|close without saving|不保存|放弃|discard|退出|quit|close", "high", "可能丢弃未保存内容"),
]

# 单字符组合键里的危险动作（键盘层面）
_RISKY_KEYS = {
    "cmd+shift+delete": "清空废纸篓",
    "cmd+option+esc": "强制退出",
    "ctrl+alt+delete": "系统级中断",
}

_LEVEL_ORDER = {"low": 0, "high": 1, "critical": 2}


@dataclass
class Risk:
    level: str = "low"
    category: str = ""
    detail: str = ""

    @property
    def needs_approval(self) -> bool:
        return self.level in ("high", "critical")

    def __str__(self) -> str:
        if self.level == "low":
            return "低风险"
        return f"{self.level}风险（{self.category}：{self.detail}）"


def _max(a: str, b: str) -> str:
    return a if _LEVEL_ORDER.get(a, 0) >= _LEVEL_ORDER.get(b, 0) else b


def classify(action_type: str, target_text: str = "", key: str = "") -> Risk:
    """判断一个动作的风险等级。

    target_text：被点击元素上的文字（按钮标题/列表项名）。这一步很关键——
    同一个坐标在不同界面上完全不同，只有文字能说明它是不是"删除"。
    """
    t = (action_type or "").lower()
    text = (target_text or "").strip()
    risk = Risk()

    if key:
        combo = "+".join(k.strip().lower() for k in re.split(r"[+,]", key) if k.strip())
        if combo in _RISKY_KEYS:
            return Risk("critical", "快捷键", _RISKY_KEYS[combo])

    # 只看点击类：输入文字/滚动/等待不会造成不可逆后果
    if t not in ("click", "double_click", "right_click", "middle_click", "triple_click"):
        return risk

    hay = text.lower()
    if not hay:
        return risk

    for pattern, level, category in _PATTERNS:
        m = re.search(pattern, hay, re.IGNORECASE)
        if not m:
            continue
        risk.level = _max(risk.level, level)
        if not risk.category or _LEVEL_ORDER.get(level, 0) >= _LEVEL_ORDER.get(risk.level, 0):
            risk.category = category
        risk.detail = m.group(0)

    # 「删除线」「清除格式」这类其实是编辑功能，不该拦
    if risk.level != "low" and re.search(r"删除线|删除格式|清除格式|clear formatting", hay):
        return Risk()
    return risk


def describe(action_type: str, target_text: str = "", key: str = "") -> str:
    """给人看的一句话，用于审批弹窗。"""
    what = target_text or key or action_type
    return f"{action_type}「{what}」" if what else action_type


def summarize(risk: Optional[Risk]) -> str:
    return "低风险" if risk is None else str(risk)
