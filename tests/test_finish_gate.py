"""收尾闸门回归测试：「模型说完成了」不等于「真的完成了」。

这是「假完成」的唯一防线。历史事故：
- 点了搜索框、一个字都没输，模型输出 done → 被判成功；
- 重复点同一处、屏幕没变化，兜底逻辑把它当成完成。

这里的断言锁住三种情况的语义差异：
- 复核通过      → success=True
- 复核不通过    → success=False，reason 里必须写明「声称完成但复核未通过」（不粉饰）
- 无法复核      → success=True，但 reason 必须标注「未能复核」（不假装验证过）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yu_agent.agent.finish_gate import FinishGate
from yu_agent.agent.state import AgentState
from yu_agent.agent.verify import Verdict


def _translate(zh: str, en: str) -> str:
    return zh


class _Emit:
    def __init__(self) -> None:
        self.events: list = []

    def __call__(self, e) -> None:
        self.events.append(e)


def _gate(verdict: Verdict, *, max_rejects: int = 1, dry_run: bool = False):
    state = AgentState()
    emit = _Emit()
    gate = FinishGate(
        check_fn=lambda task, claim, img, step: verdict,
        state=state,
        emit=emit,
        translate=_translate,
        max_rejects=max_rejects,
        dry_run=dry_run,
    )
    return gate, state, emit


# ── 1. 复核通过 ─────────────────────────────────────────────────── #
def test_verified_finish_settles_as_success() -> None:
    gate, state, emit = _gate(Verdict(ok=True, reason="搜索结果里有「天气」", method="model"))
    r = gate.finish("查天气", "已搜索", None, step=3)

    assert r.finished is True
    assert state.done is True and state.running is False
    assert state.success is True and state.verified is True
    assert state.reason == "已搜索"
    assert any(e.get("type") == "result" for e in emit.events)
    print("✓ 复核通过 → success=True，reason 用原话")


# ── 2. 复核不通过 → 打回一次 ────────────────────────────────────── #
def test_rejected_claim_is_sent_back_once() -> None:
    gate, state, emit = _gate(Verdict(ok=False, reason="结果区还是空的", method="model"))
    recent: list = []
    r = gate.finish("查天气", "已搜索", None, step=3, recent=recent)

    assert r.finished is False, "第一次复核失败应该打回继续跑"
    assert state.done is False and state.success is False
    assert gate.rejects == 1
    assert "结果区还是空的" in gate.hint, gate.hint
    assert "不要再" in gate.hint, "打回提示必须明确要求模型别再输出 done"
    assert recent and "声称完成但复核未通过" in recent[-1]
    assert any(e.get("type") == "notice" for e in emit.events)
    print("✓ 复核不通过 → 打回，并把意见注入下一轮 prompt")


# ── 3. 打完一次还不行 → 如实收尾，不粉饰 ────────────────────────── #
def test_second_rejection_settles_as_failure() -> None:
    gate, state, emit = _gate(Verdict(ok=False, reason="结果区还是空的", method="model"))

    assert gate.finish("查天气", "已搜索", None, step=3).finished is False
    r2 = gate.finish("查天气", "已搜索", None, step=4)

    assert r2.finished is True, "超过 max_rejects 必须收尾，不能无限打回"
    assert state.success is False
    assert "复核未通过" in state.reason, state.reason
    assert "结果区还是空的" in state.reason, "复核理由要写进结论，别只说「失败了」"
    assert state.verify_reason == "结果区还是空的", "原始复核结论要单独留一份"
    print("✓ 打回次数用尽 → success=False，reason 写明复核没过及原因")


# ── 4. 无法复核 ≠ 成功 ─────────────────────────────────────────── #
def test_unverifiable_is_not_reported_as_verified() -> None:
    gate, state, _ = _gate(Verdict(ok=None, reason="截图是纯黑", method="model"))
    r = gate.finish("查天气", "已搜索", None, step=3)

    assert r.finished is True
    assert state.success is True, "无法复核不该判失败——那会让正常任务全废"
    assert state.verified is not True
    assert "未能复核" in state.reason, f"不能假装验证过，实际 {state.reason}"
    print("✓ 无法复核 → 算成功但明确标注「未能复核」")


# ── 5. dry_run 下不复核 ────────────────────────────────────────── #
def test_dry_run_never_sends_back() -> None:
    gate, state, _ = _gate(Verdict(ok=False, reason="x", method="dry-run"), dry_run=True)
    r = gate.finish("查天气", "已搜索", None, step=3)

    assert r.finished is True, "离线测试里打回会导致死循环"
    assert gate.rejects == 0
    print("✓ dry_run 直接收尾，不打回")


# ── 6. reset 后能重新计数 ─────────────────────────────────────── #
def test_reset_clears_reject_counter() -> None:
    gate, _, _ = _gate(Verdict(ok=False, reason="x", method="model"), max_rejects=1)
    gate.finish("t", "c", None, step=1)
    assert gate.rejects == 1 and gate.hint
    gate.reset()
    assert gate.rejects == 0 and gate.hint == ""
    print("✓ reset 清空打回计数与提示")


if __name__ == "__main__":
    test_verified_finish_settles_as_success()
    test_rejected_claim_is_sent_back_once()
    test_second_rejection_settles_as_failure()
    test_unverifiable_is_not_reported_as_verified()
    test_dry_run_never_sends_back()
    test_reset_clears_reject_counter()
    print("\nALL PASS")
