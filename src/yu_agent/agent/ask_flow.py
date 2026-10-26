"""ask 动作的处理：把「模型想问你」变成一次真实的人机往返。

为什么单独成文件：这段要处理三种情况（有人回答 / 没人回答 / 用户取消），
还要生成下一轮的注入文案，塞进主循环会挤掉真正的流程。

返回的 hint 会进入下一轮 prompt：
- 有回答 → 明确告诉模型「用户这么说的，照这个做，别再问」
- 没人回答 → 明确告诉模型「无人接管，自行决定，别再问」
两者都必须写死「不要重复提问」，否则模型会一直 ask。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List


@dataclass
class AskOutcome:
    hint: str = ""       # 注入下一轮 prompt 的文本
    description: str = ""  # 写日志/历史用
    question: str = ""
    answer: str = ""


def handle_ask(
    action: Any,
    control,
    translate: Callable[[str, str], str],
) -> AskOutcome:
    """执行一次提问流程。control 为 RunControl（ask 可能是空实现）。"""
    question = (getattr(action, "text", "") or getattr(action, "reason", "") or "").strip()
    question = question or "需要补充信息"
    options: List[str] = list((getattr(action, "args", {}) or {}).get("options") or [])

    answer = control.ask(question, options)
    if answer:
        hint = translate(
            f"【用户回答】针对你刚才的问题「{question[:80]}」，"
            f"用户回答：{answer[:200]}。请按这个回答继续，不要重复提问。",
            f'[User answer] For your question "{question[:80]}", the user answered: '
            f"{answer[:200]}. Continue with it; do not ask the same question again.",
        )
        return AskOutcome(
            hint=hint,
            description=f"已询问用户并得到回答：{answer[:60]}",
            question=question,
            answer=answer,
        )

    hint = translate(
        "【系统】你上一步想向用户提问，但没有得到回答（当前无人接管）。"
        "请根据现有信息和屏幕内容自行做出最合理的决定，不要再重复提问。",
        "[System] Your question got no answer (nobody is attending). "
        "Decide yourself from the screen and the task; do not ask again.",
    )
    return AskOutcome(
        hint=hint,
        description="提问无人回答，改为自行决定",
        question=question,
        answer="",
    )
