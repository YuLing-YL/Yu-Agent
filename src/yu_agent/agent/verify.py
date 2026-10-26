"""任务结果自校验：模型说「做完了」之后，回头看一眼屏幕上是不是真的做完了。

为什么需要它：以前 `done` 是唯一判据——模型说完成就算完成。于是出现过
「点了搜索框、一个字都没输」却被判定成功；也出现过「重复点同一处、屏幕没变化」
被兜底逻辑当成完成。这类假完成比失败更糟：用户以为做完了，不再检查。

这里的做法是**复核**，不是让模型自评：
1. 另起一次调用（低温度、不给历史干扰），把「任务 + 做过的步骤 + 当前截图」给它；
2. 只允许基于截图上**看得见的状态**下结论（搜索了 X → 结果里有 X；打开应用 → 该应用在前台）；
3. 说「没完成」并给出还差什么 → 主循环据此再跑一轮，而不是就此收工；
4. 模型不可用/输出无法解析 → 返回「未验证」（不假装成功，也不无限重试）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Verdict:
    """一次结果校验的结论。"""

    ok: Optional[bool] = None  # True=确认完成 / False=确认没完成 / None=无法验证
    reason: str = ""
    method: str = ""  # model / disabled / dry-run / error
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "method": self.method,
            "confidence": self.confidence,
        }


_SYSTEM_ZH = """你是一个严格的验收员。你的唯一工作是：看着**当前这一张**屏幕截图，
判断用户的任务**此刻是否真的已经完成**。

判定规则（必须遵守）：
1. 只认截图上看得到的状态。看不到证据 = 没完成。
2. 「点过某个按钮」「没有报错」「模型说已经做完」都**不算**完成。
3. 任务含搜索/查询 → 结果区域必须真的出现对应的结果（内容与关键词相关）。
4. 任务含打开某应用/文件 → 该应用窗口必须在截图上前台可见，或内容已经显示出来。
5. 任务含输入/修改 → 输入框里必须是**新内容**（不是旧内容后面拼了新词）。
6. 任务含发送/保存/提交 → 必须有成功后的界面状态（已发送标记、文件已出现在目标位置等）。
7. 如果截图不足以判断（被弹窗挡住、还在加载、需要滚动），判 done=false，
   在 reason 里说清「还差什么」，不要猜成完成。
8. 说完成时，reason 里必须点出你看到的**具体证据**（哪个区域、什么文字）。

只输出 JSON，不要任何解释文字：
{"done": true/false, "confidence": 0.0~1.0, "reason": "一句话，中文"}
"""

_SYSTEM_EN = """You are a strict acceptance checker. Your only job: look at the CURRENT screenshot
and decide whether the user's task is genuinely finished **right now**.

Rules:
1. Only visible evidence counts. Not visible = not done.
2. "A button was clicked", "no error appeared", "the agent said it finished" are NOT completion.
3. Search/query tasks: the results area must actually show relevant results.
4. Open-app/file tasks: that window must be visible in the foreground, or its content shown.
5. Typing/editing tasks: the field must contain the NEW text (not old text with new text appended).
6. Send/save/submit tasks: a post-success state must be visible (sent marker, file present, etc.).
7. If the screenshot is inconclusive (dialog covering it, still loading, needs scrolling),
   answer done=false and say what is still missing. Never guess "done".
8. When answering true, point at the concrete evidence in `reason`.

Output JSON only, no prose:
{"done": true/false, "confidence": 0.0-1.0, "reason": "one short sentence"}
"""


class GoalVerifier:
    """用一次独立调用复核「是否真的完成」。"""

    def __init__(self, backend, language: str = "", max_tokens: int = 300):
        self.backend = backend
        self.language = (language or "").strip().lower()
        self.last_usage = None       # 供 UsageMeter 记账
        self.last_latency_s = 0.0
        self.max_tokens = max_tokens

    @property
    def _en(self) -> bool:
        return self.language.startswith("en")

    @property
    def system_prompt(self) -> str:
        """复核用的系统提示词（判定规则都在这里）。"""
        return _SYSTEM_EN if self._en else _SYSTEM_ZH

    def build_prompt(self, task: str, steps: List[Dict[str, Any]]) -> str:
        """把任务与「做过的步骤」拼成复核请求。只看最近 8 步，避免噪声。"""
        lines: List[str] = []
        for h in steps[-8:]:
            desc = str(h.get("action_desc") or h.get("action") or "")
            result = str(h.get("result") or "")[:80]
            if self._en:
                lines.append(f"- step {h.get('step')}: {desc} -> {result}")
            else:
                lines.append(f"- 步骤{h.get('step')}：{desc} → {result}")
        body = "\n".join(lines)

        if self._en:
            return (
                f"Task: {task}\n\n"
                f"Steps already taken:\n{body or '- (none)'}\n\n"
                "Look at the screenshot above. Is the task fully done? Output the JSON."
            )
        return (
            f"任务：{task}\n\n"
            f"已经做过的步骤：\n{body or '- （无）'}\n\n"
            "请对照截图判断：任务是否真的已经完成？只输出 JSON。"
        )

    def verify(
        self,
        task: str,
        steps: List[Dict[str, Any]],
        screenshot_data_url: str,
    ) -> Verdict:
        """复核一次。任何异常都退化成「未验证」，绝不影响任务收尾。"""
        from ..models.base import VLMRequest

        if not screenshot_data_url:
            return Verdict(None, "没有截图，无法复核", method="error")
        req = VLMRequest(
            system=self.system_prompt,
            prompt=self.build_prompt(task, steps),
            images=[screenshot_data_url],
            temperature=0.0,
            max_tokens=self.max_tokens,
        )
        try:
            resp = self.backend.chat(req)
        except Exception as e:  # 模型挂了不能让任务跟着挂
            return Verdict(None, f"复核调用失败：{e}", method="error")
        self.last_usage = getattr(resp, "usage", None)
        self.last_latency_s = float(getattr(resp, "latency_s", 0.0) or 0.0)
        return parse_verdict(resp.text or "")


def parse_verdict(text: str) -> Verdict:
    """从模型输出里抽 {done, confidence, reason}。抽不到就返回「未验证」。"""
    raw = (text or "").strip()
    if not raw:
        return Verdict(None, "复核输出为空", method="error")

    data: Optional[Dict[str, Any]] = None
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            data = loaded
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                loaded = json.loads(m.group(0))
                if isinstance(loaded, dict):
                    data = loaded
            except json.JSONDecodeError:
                data = None

    if data is None:
        # 有些模型会直接写 "done: true" / "没完成"，做一层兜底识别
        low = raw.lower()
        if re.search(r"\bdone\b\s*[:=]\s*true", low) or raw.startswith("true"):
            return Verdict(True, raw[:120], method="model", confidence=0.5)
        if re.search(r"\bdone\b\s*[:=]\s*false", low) or raw.startswith("false"):
            return Verdict(False, raw[:120], method="model", confidence=0.5)
        return Verdict(None, f"无法解析复核输出：{raw[:120]}", method="error")

    done = data.get("done", data.get("completed", data.get("success")))
    ok: Optional[bool]
    if isinstance(done, bool):
        ok = done
    elif isinstance(done, (int, float)):
        ok = bool(done)
    elif isinstance(done, str):
        ok = done.strip().lower() in ("true", "yes", "1", "done", "完成")
    else:
        ok = None

    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(data.get("reason") or data.get("why") or "").strip()
    if ok is None:
        return Verdict(None, reason or "复核输出里没有 done 字段", method="error")
    return Verdict(ok, reason, method="model", confidence=max(0.0, min(1.0, conf)))
