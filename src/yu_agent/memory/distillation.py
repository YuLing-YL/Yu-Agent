"""记忆蒸馏：一次任务结束后，把轨迹提炼成**长期**记忆。

## 为什么不能只把动作名串起来

以前的实现是 `"轨迹: click;click;type;click"` —— 这种东西下次召回时
对模型没有任何帮助（它不知道点了什么、为什么成功、下次该注意什么）。

现在做一次**轻量的模型总结**（不给图，纯文本，几百 token），产出三样东西：

- `summary`  这次任务干了什么、成没成      → 情节记忆（episodic，可回放）
- `lesson`   下次做同类任务要注意什么       → 语义记忆（semantic，跨任务复用）
- `steps`    成功时的可复用步骤（可选）     → 程序记忆（procedural，模板）

模型总结失败/没接模型时，退化成模板总结 —— **绝不因为总结失败影响主流程**。

## 触发时机

`AgentLoop` 在 `finally` 里调 `finish_task`，所以任务成功、失败、被用户停止
都会落一条记忆。失败的经验往往比成功的更值钱（"这样走不通"）。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..common.jsonx import extract_json_object
from .episodic import EpisodicMemory
from .procedural import ProceduralMemory
from .semantic import SemanticMemory

# 总结用的 system prompt：要求极短、结构化、只讲可复用的东西
_SYSTEM_ZH = """你在给一个操作电脑的 AI 助手整理「长期记忆」。
输入是它刚执行完的一次任务轨迹（精简过的步骤列表）。请提炼成 JSON，只输出 JSON：
{
  "summary": "这次任务做了什么、结果如何，一句话，60 字以内",
  "lesson": "下次做同类任务要注意的一条经验或教训，40 字以内；没有就留空字符串",
  "steps": ["如果任务成功，把可复用的关键步骤按顺序浓缩成 2~6 条；失败就留空数组"]
}
要求：
- 只写**通用**的东西（"先点搜索框再输入"），不要写坐标、编号、截图细节。
- 不要猜测看不到的内容，不要编造成功。
- 不要输出 Markdown，不要输出解释。"""

_SYSTEM_EN = """You distill one finished GUI-automation task into long-term memory.
Input: a compact step list of what the agent did. Output JSON only:
{
  "summary": "what the task did and whether it worked, one sentence, <=30 words",
  "lesson": "one reusable tip or mistake to avoid next time, <=20 words; empty string if none",
  "steps": ["if the task succeeded, 2-6 condensed reusable steps in order; empty array if it failed"]
}
Rules:
- Only general knowledge ("click the search field, then type"), never coordinates/mark ids.
- Never invent success or describe content you cannot see.
- No markdown, no explanation."""


def _clip(s: Any, n: int) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠出 JSON（本地模型经常带前后废话或 ``` 包裹）。

    统一走 common/jsonx，不再各自实现一份提取逻辑。
    """
    if not text:
        return None
    return extract_json_object(text)


class MemoryDistiller:
    """把一次任务的 steps 轨迹蒸馏成长期记忆。"""

    def __init__(
        self,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
    ):
        self.episodic = episodic
        self.semantic = semantic
        self.procedural = procedural

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def distill(
        self,
        task: str,
        steps: List[Dict[str, Any]],
        success: bool,
        summarize: Optional[Callable[[str, str], str]] = None,
        language: str = "",
    ) -> Dict[str, Any]:
        """提炼并落盘。返回 {summary, lesson, steps, used_llm}。

        summarize(system, user) -> text：可选的模型总结函数。
        给了就用（失败自动退回模板），没给就用模板。
        """
        en = str(language or "").strip().lower().startswith("en")
        payload = self._render_trajectory(task, steps, success, en)

        data: Optional[Dict[str, Any]] = None
        used_llm = False
        if summarize is not None:
            try:
                raw = summarize(_SYSTEM_EN if en else _SYSTEM_ZH, payload)
                data = _parse_json(raw)
                used_llm = data is not None
            except Exception:
                data = None

        if data is None:
            data = self._fallback(task, steps, success, en)

        summary = _clip(data.get("summary"), 160)
        lesson = _clip(data.get("lesson"), 100)
        proc_steps = [
            _clip(s, 60) for s in (data.get("steps") or []) if str(s).strip()
        ][:6]

        # ── 落盘 ────────────────────────────────────────────────────
        # 1) 情节记忆：永远记（失败也是经验）
        tag = ("succeeded" if success else "failed") if en else ("成功" if success else "失败")
        episode = f"[{tag}] {task[:80]}：{summary}" if summary else f"[{tag}] {task[:80]}"
        self.episodic.record(task, episode, success=success, dedupe=True)

        # 2) 语义记忆：只有真提炼出经验才写，且去重
        if lesson and len(lesson) >= 4:
            self.semantic.remember(lesson, dedupe=True)

        # 3) 程序记忆：只有**成功**且步骤足够时才存模板 —— 失败路径不能当模板用
        if success and len(proc_steps) >= 2:
            self.procedural.remember(task[:60], proc_steps, dedupe=True)

        return {
            "summary": summary,
            "lesson": lesson,
            "steps": proc_steps,
            "used_llm": used_llm,
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _render_trajectory(
        task: str, steps: List[Dict[str, Any]], success: bool, en: bool
    ) -> str:
        """把轨迹压成紧凑文本（不带坐标、不带截图，控制 token）。"""
        lines: List[str] = []
        lines.append(f"Task: {task}" if en else f"任务：{task}")
        lines.append(
            f"Result: {'success' if success else 'failed'}" if en
            else f"结果：{'成功' if success else '失败'}"
        )
        lines.append("Steps:" if en else "步骤：")
        for s in steps[-14:]:            # 太长没意义，只放最近 14 步
            part = _clip(s.get("action"), 40)
            reason = _clip(s.get("reason"), 40)
            res = _clip(s.get("result"), 40)
            if reason:
                part += f" ({reason})"
            if res:
                part += f" -> {res}"
            lines.append(f"  - {part}")
        if not steps:
            lines.append("  (no steps executed)" if en else "  （没有执行任何步骤）")
        return "\n".join(lines)

    @staticmethod
    def _fallback(
        task: str, steps: List[Dict[str, Any]], success: bool, en: bool
    ) -> Dict[str, Any]:
        """没模型可用时的模板总结 —— 只保证「有记录」，不假装有洞见。"""
        n = len(steps)
        last = steps[-1] if steps else {}
        act_seq = " → ".join(_clip(s.get("action"), 18) for s in steps[-5:])
        if en:
            summary = (
                f"{'Succeeded' if success else 'Failed'} after {n} step(s)."
                + (f" Last actions: {act_seq}" if act_seq else "")
            )
            return {"summary": summary, "lesson": "", "steps": []}
        summary = f"{'成功' if success else '未能完成'}，共 {n} 步。"
        if act_seq:
            summary += f"关键动作：{act_seq}"
        if not success and last.get("result"):
            summary += f"；最后一步：{_clip(last.get('result'), 40)}"
        return {"summary": summary, "lesson": "", "steps": []}
