"""结果自校验（done → 复核）回归测试。

背景：以前 `done` 是唯一判据，模型说完成就算完成。实测出现过「点了搜索框、
一个字都没输」被判成功，也出现过「反复点同一处、屏幕没变化」被兜底当成完成。
假完成比失败更糟——用户以为做完了，不再检查。

这里的断言锁三件事：
1. 复核结论解析要稳（JSON / 夹杂文字 / 乱码都要能处理）；
2. 复核说「没完成」时**不能结束**，要把「还差什么」写进下一轮 prompt 打回重做；
3. 打回次数用完仍不通过时，如实标成失败（success=False），不粉饰。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yu_agent.agent.verify import GoalVerifier, Verdict, parse_verdict


# ── 1. 结论解析 ─────────────────────────────────────────────────── #
def test_parse_verdict_json() -> None:
    v = parse_verdict('{"done": true, "confidence": 0.9, "reason": "结果区出现了天气信息"}')
    assert v.ok is True and v.method == "model" and 0.85 <= v.confidence <= 0.95, v
    assert "天气" in v.reason, v

    v = parse_verdict('{"done": false, "confidence": 0.7, "reason": "搜索框还是空的"}')
    assert v.ok is False and "空的" in v.reason, v
    print("✓ 标准 JSON 两个方向都能解析")


def test_parse_verdict_tolerant() -> None:
    # 模型爱在 JSON 前后加解释
    v = parse_verdict('结论如下：\n{"done": false, "reason": "还没进入结果页"}\n以上。')
    assert v.ok is False and "结果页" in v.reason, v

    # 完全不是 JSON：只有 done: true 这种写法
    v = parse_verdict("done: true")
    assert v.ok is True, v

    # 无法判断 → 必须返回 None，不能瞎猜成成功
    v = parse_verdict("我觉得大概可以了吧")
    assert v.ok is None and v.method == "error", v
    print("✓ 夹带解释 / 非 JSON / 乱码 都按预期降级")


def test_verify_prompt_asks_for_evidence() -> None:
    g = GoalVerifier(backend=None, language="zh")
    prompt = g.build_prompt(
        "在Safari搜索今天天气",
        [
            {"step": 1, "action_desc": "点击「智能搜索栏」", "result": "已执行 click"},
            {"step": 2, "action_desc": "输入「今天天气」", "result": "已执行 type"},
        ],
    )
    assert "在Safari搜索今天天气" in prompt, prompt
    assert "输入「今天天气」" in prompt, prompt
    # 复核必须要求「看得见的证据」，否则模型会照着步骤自己点头
    system = g.system_prompt
    assert "只认截图上看得到的状态" in system and "看不到证据" in system, system
    assert "不算" in system, "必须写明「点过按钮/说完成」都不算完成"
    print("✓ 复核 prompt 带任务与步骤，并强制要求可见证据")


# ── 2. 打回逻辑 ─────────────────────────────────────────────────── #
def _make_loop(seq, verdicts, cfg_extra=None):
    """搭一个不碰真实屏幕/键鼠的 AgentLoop，verifier 用脚本控制。"""
    from PIL import Image

    import yu_agent.actions.executor as exmod
    import yu_agent.agent.loop as loopmod
    from yu_agent.agent.loop import AgentLoop
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore
    from yu_agent.models.base import VLMResponse
    from yu_agent.perception.a11y import UIElement
    from yu_agent.perception.screenshot import Screen
    from yu_agent.skills import build_default_registry

    prompts: list[str] = []

    class Backend:
        name = "capture"

        def __init__(self):
            self.i = 0

        def chat(self, req):
            prompts.append(req.prompt)
            s = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return VLMResponse(text=s)

    class FakeVerifier:
        def __init__(self, results):
            self.results = list(results)
            self.calls = 0

        def verify(self, task, steps, data_url):
            self.calls += 1
            return self.results[min(self.calls - 1, len(self.results) - 1)]

    el = UIElement(id=0, role="AXSearchField", title="搜索",
                   x=900, y=60, w=300, h=30, app="Safari")
    saved = (loopmod.capture_screen, loopmod.get_screen_size,
             loopmod.build_som_mapping, exmod.ActionExecutor.execute)
    loopmod.capture_screen = lambda: Image.new("RGB", (1200, 800))
    loopmod.get_screen_size = lambda: Screen(1200, 800)
    loopmod.build_som_mapping = lambda img, *a, **k: (img, {1: el}, [el])
    exmod.ActionExecutor.execute = lambda self, a: None
    try:
        cfg = {"max_steps": 6, "verify_after_action": False,
               "prompt_language": "zh", "som_mode": "marks"}
        cfg.update(cfg_extra or {})
        lp = AgentLoop(
            Backend(),
            MemoryManager(store=MemoryStore(db_path=":memory:")),
            build_default_registry(),
            config=cfg,
            dry_run=False,
        )
        fake = FakeVerifier(verdicts)
        lp._verifier = fake
        state = lp.run("在Safari搜索今天天气")
    finally:
        (loopmod.capture_screen, loopmod.get_screen_size,
         loopmod.build_som_mapping, exmod.ActionExecutor.execute) = saved
    return state, prompts, fake


def test_reject_then_continue() -> None:
    """复核说没完成 → 不结束，并把「还差什么」写进下一轮 prompt。"""
    seq = [
        '{"type":"click","mark":1,"reason":"聚焦搜索框"}',
        '{"type":"done","reason":"已经搜到了"}',
        '{"type":"key","key":"enter","reason":"补一次回车"}',
        '{"type":"done","reason":"这次真的搜到了"}',
    ]
    state, prompts, fake = _make_loop(
        seq,
        [Verdict(False, "结果区里没有天气内容", method="model", confidence=0.8),
         Verdict(True, "结果区出现了天气条目", method="model", confidence=0.9)],
    )
    assert fake.calls == 2, f"应复核两次，实际 {fake.calls}"
    assert state.success is True and state.verified is True, state
    # 被打回后的那一轮 prompt 必须包含复核意见，否则模型不知道为什么继续
    joined = "\n".join(prompts)
    assert "系统复核" in joined, "被打回时没把复核意见告诉模型"
    assert "结果区里没有天气内容" in joined, joined[-600:]
    print("✓ 复核未通过 → 打回继续，并把原因写进 prompt")


def test_reject_limit_marks_failed() -> None:
    """打回到上限仍不通过 → 如实标失败，不假装成功。"""
    seq = [
        '{"type":"click","mark":1,"reason":"聚焦搜索框"}',
        '{"type":"done","reason":"完成"}',
        '{"type":"done","reason":"真的完成了"}',
        '{"type":"done","reason":"相信我"}',
    ]
    state, _prompts, fake = _make_loop(
        seq, [Verdict(False, "屏幕上什么都没有变", method="model")],
        cfg_extra={"max_goal_rejects": 1},
    )
    assert fake.calls == 2, f"打回上限 1，应复核 2 次后收尾，实际 {fake.calls}"
    assert state.success is False, f"复核始终不通过却标成成功：{state}"
    assert state.verified is False and "复核未通过" in state.reason, state
    print("✓ 复核打回到上限 → success=False，原因写清")


def test_verify_disabled_keeps_old_behaviour() -> None:
    """关掉复核时行为与以前一致（success=True，verified=None）。"""
    state, _prompts, fake = _make_loop(
        ['{"type":"done","reason":"完成"}', '{"type":"done","reason":"完成"}'],
        [Verdict(True, "ok", method="model")],
        cfg_extra={"verify_goal": False},
    )
    assert fake.calls == 0, "关掉复核后不该再调用 verifier"
    assert state.success is True and state.verified is None, state
    print("✓ verify_goal=false 时退回旧行为（不多花一次模型调用）")


if __name__ == "__main__":
    test_parse_verdict_json()
    test_parse_verdict_tolerant()
    test_verify_prompt_asks_for_evidence()
    test_reject_then_continue()
    test_reject_limit_marks_failed()
    test_verify_disabled_keeps_old_behaviour()
    print("\nALL PASS")
