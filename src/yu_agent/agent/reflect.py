"""反思者：任务结束后或动作失败时，总结与修正。"""
from __future__ import annotations

from typing import Any, Dict, List


class Reflector:
    """对执行结果做反思，为记忆蒸馏提供输入。"""

    def summarize(self, task: str, history: List[Dict[str, Any]], success: bool) -> str:
        """生成任务总结文本。"""
        if not history:
            return f"任务『{task}』无执行记录。"
        n = len(history)
        actions = [h.get("action", "") for h in history[-5:]]
        status = "成功" if success else "失败"
        return f"任务『{task}』共 {n} 步，结果{status}。最后几步动作：{'；'.join(actions)}"
