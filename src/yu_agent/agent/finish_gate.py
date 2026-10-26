"""收尾闸门：模型说「完成」之后，任务到底结不结束。

单独成文件的原因：这是「假完成」的唯一防线，逻辑会随着复核策略变复杂
（打回次数、原因注入、失败如实标注），塞进 loop.py 只会让它更难读。

职责边界：
- 只做**决策**：给一个 Verdict，决定「结束」还是「打回」；
- 不调模型（复核由调用方注入的 check 函数完成）；
- 不碰鼠标键盘、不碰记忆，只改 AgentState 上跟结论有关的字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .verify import Verdict


@dataclass
class FinishResult:
    """一次收尾尝试的结果。"""

    finished: bool  # True = 任务到此为止；False = 已打回，继续跑
    verdict: Verdict
    message: str = ""  # 给用户看的一句话（写日志/界面）


class FinishGate:
    """判定「声称完成」是否成立，并负责打回时的提示生成。"""

    def __init__(
        self,
        check_fn: Callable[[str, str, Any, int], Verdict],
        state,
        emit: Callable[[Dict[str, Any]], None],
        translate: Callable[[str, str], str],
        max_rejects: int = 1,
        dry_run: bool = False,
        logger=None,
    ):
        self._check = check_fn          # (task, claim, obs_img, step) -> Verdict
        self.state = state
        self._emit = emit
        self._t = translate
        self.max_rejects = max_rejects
        self.dry_run = dry_run
        self._log = logger
        self.rejects = 0
        self._hint = ""

    # ---- 提示 ----
    @property
    def hint(self) -> str:
        """当前待注入下一轮 prompt 的复核意见（空 = 没有）。"""
        return self._hint

    def clear_hint(self) -> None:
        self._hint = ""

    def reset(self) -> None:
        self.rejects = 0
        self._hint = ""

    # ---- 主入口 ----
    def finish(
        self,
        task: str,
        claim: str,
        obs_img,
        step: int,
        recent: Optional[List[str]] = None,
    ) -> FinishResult:
        verdict = self._check(task, claim, obs_img, step)

        if (
            verdict.ok is False
            and not self.dry_run
            and self.rejects < self.max_rejects
        ):
            self.rejects += 1
            self._hint = self.build_reject_hint(claim, verdict.reason)
            msg = (
                f"复核没通过：{verdict.reason or '截图上看不到完成状态'}"
                f" —— 已打回继续执行（第 {self.rejects}/{self.max_rejects} 次）"
            )
            if self._log:
                self._log.warning(msg)
            self._emit({"type": "notice", "step": step, "message": msg})
            if recent is not None:
                recent.append(
                    f"步骤{step}: 声称完成但复核未通过（{verdict.reason[:40]}）"
                )
                del recent[:-6]
            return FinishResult(False, verdict, msg)

        return FinishResult(True, verdict, self._settle(claim, verdict))

    # ---- 结束时的状态写入 ----
    def _settle(self, claim: str, verdict: Verdict) -> str:
        """把结论落到 state 上，返回给用户看的结束原因。

        三种情况刻意区分开：
        - 复核通过      → success=True，reason=原话
        - 复核不通过    → success=False，reason 写明「声称完成但复核没过」（不粉饰）
        - 无法复核      → success=True，但 reason 标注「未能复核」（不假装验证过）
        """
        self.state.done = True
        self.state.running = False
        self.state.verified = verdict.ok
        self.state.verify_reason = verdict.reason

        if verdict.ok is False:
            self.state.success = False
            reason = (
                f"模型声称完成，但复核未通过：{verdict.reason or '截图上没有完成证据'}"
            )
        elif verdict.ok is None:
            self.state.success = True
            reason = claim
            if verdict.method not in ("disabled", "dry-run"):
                reason = f"{claim}（结果未能复核：{verdict.reason}）"
        else:
            self.state.success = True
            reason = claim

        self.state.reason = reason
        icon = "❌" if verdict.ok is False else "✅"
        self._emit({"type": "result", "step": self.state.step, "result": f"{icon} {reason}"})
        return reason

    # ---- 打回时的提示词 ----
    def build_reject_hint(self, claim: str, reason: str) -> str:
        """告诉模型「你声称完成了，但屏幕上还看不到」，并逼它找出缺的那一步。"""
        return self._t(
            f"【系统复核】你上一步声称任务已完成（「{claim[:60]}」），"
            f"但对着屏幕截图复核的结果是**还没完成**：{reason or '看不到完成后的状态'}。\n"
            f"不要再用 done 结束任务，也不要重复已经做过的动作。请根据截图判断**还差哪一步**，"
            f"把缺的那一步做完（例如结果区里没有目标内容 → 说明搜索没生效，先确认输入框里是"
            f"新关键词，再按 enter）。只有屏幕上真的出现了完成状态，才输出 done。",
            f'[System verification] You claimed the task was finished ("{claim[:60]}"), '
            f"but the screenshot shows it is **not** done: "
            f"{reason or 'no finished state is visible'}.\n"
            f"Do not finish with done again, and do not repeat steps you already took. "
            f"Work out the ONE missing step from the screenshot and do it (e.g. if the results "
            f"area lacks the target content, the search did not take effect — check that the "
            f"field holds the new keyword, then press enter). Only output done once the "
            f"finished state is really visible.",
        )
