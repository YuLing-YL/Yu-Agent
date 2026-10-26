"""运行控制回归测试：暂停/继续、向用户提问、危险动作拦截、调用计量。

这三件事以前都不存在：
- 停了只能重跑，没有「暂停看一眼」；
- 模型缺信息时只能猜，没有「问用户」这个动作；
- 危险动作（删除/发送）没有任何拦截，也没有任何地方统计花了多少 token。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yu_agent.agent.control import RunControl
from yu_agent.agent.usage import UsageMeter


# ── 1. 暂停 / 继续 ──────────────────────────────────────────────── #
def test_pause_blocks_and_resume_releases() -> None:
    ctl = RunControl()
    ctl.pause()
    assert ctl.paused is True

    done = threading.Event()

    def worker():
        ctl.wait_while_paused()
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.15)
    assert not done.is_set(), "暂停期间不该继续往下走"

    ctl.resume()
    assert done.wait(1.0), "继续之后应该立刻放行"
    print("✓ 暂停真的挡住了，继续立刻放行")


def test_pause_can_be_aborted_by_stop() -> None:
    """暂停状态下点「停止」必须能退出等待，不能卡死。"""
    ctl = RunControl()
    ctl.pause()
    t0 = time.time()
    ctl.wait_while_paused(should_abort=lambda: True)
    assert time.time() - t0 < 1.0, "带 abort 判断时不应继续等"
    print("✓ 暂停中也能被停止打断")


# ── 2. 问用户 ───────────────────────────────────────────────────── #
def test_ask_without_asker_returns_empty() -> None:
    assert RunControl().ask("用哪个账号？") == "", "没人可问时应返回空串，交给上层自行决定"
    print("✓ 无人接管时提问返回空（调用方转「自行决定」）")


def test_ask_uses_asker() -> None:
    seen = {}

    def asker(question: str, options):
        seen["q"] = question
        seen["opts"] = options
        return "账号B"

    ctl = RunControl(asker=asker)
    assert ctl.ask("用哪个账号？", ["账号A", "账号B"]) == "账号B"
    assert seen["opts"] == ["账号A", "账号B"], seen
    print("✓ 提问带上选项，回答原样返回")


# ── 3. 调用计量 ─────────────────────────────────────────────────── #
def test_usage_meter_accumulates() -> None:
    from yu_agent.models.base import TokenUsage

    m = UsageMeter(price_in=0.001, price_out=0.002)
    m.add(TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500), 1.2, "planner")
    m.add(TokenUsage(prompt_tokens=200, completion_tokens=100, total_tokens=300), 0.3, "verify")

    assert m.calls == 2 and m.total_tokens == 1800, m
    assert m.by_role["planner"]["calls"] == 1 and m.by_role["verify"]["calls"] == 1
    # 1000/1000*0.001 + 500/1000*0.002 + 200/1000*0.001 + 100/1000*0.002 = 0.0024
    assert abs((m.cost or 0) - 0.0024) < 1e-9, m.cost
    assert "1800 tokens" in m.one_line(), m.one_line()
    print("✓ 计量器累计 token/延迟/成本，并按角色分开记")


def test_usage_without_price_reports_no_cost() -> None:
    from yu_agent.models.base import TokenUsage

    m = UsageMeter()
    m.add(TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15), 0.1)
    assert m.cost is None
    assert "cost_usd" not in m.summary(), "没有单价就不该编造成本"
    print("✓ 没有配置单价时只报 token，不瞎算钱")


# ── 4. loop 层：提问进 prompt / 危险动作被拦 / 用量落到 state ─────── #
def _make_loop(seq, control=None, elements=None):
    from PIL import Image

    import yu_agent.actions.executor as exmod
    import yu_agent.agent.loop as loopmod
    from yu_agent.agent.loop import AgentLoop
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore
    from yu_agent.models.base import TokenUsage, VLMResponse
    from yu_agent.perception.a11y import UIElement
    from yu_agent.perception.screenshot import Screen
    from yu_agent.skills import build_default_registry

    prompts: list[str] = []
    executed: list[str] = []

    class Backend:
        name = "capture"

        def __init__(self):
            self.i = 0

        def chat(self, req):
            prompts.append(req.prompt)
            s = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return VLMResponse(
                text=s, usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
            )

    els = elements or [UIElement(id=0, role="AXButton", title="搜索", x=100, y=100, w=80, h=30, app="App")]
    mp = {i + 1: e for i, e in enumerate(els)}

    saved = (loopmod.capture_screen, loopmod.get_screen_size,
             loopmod.build_som_mapping, exmod.ActionExecutor.execute)
    loopmod.capture_screen = lambda: Image.new("RGB", (1200, 800))
    loopmod.get_screen_size = lambda: Screen(1200, 800)
    loopmod.build_som_mapping = lambda img, *a, **k: (img, dict(mp), list(mp.values()))

    def fake_execute(self, action):
        executed.append(action.type)
        return f"已执行 {action.type}"

    exmod.ActionExecutor.execute = fake_execute
    try:
        lp = AgentLoop(
            Backend(),
            MemoryManager(store=MemoryStore(db_path=":memory:")),
            build_default_registry(),
            config={"max_steps": 6, "verify_after_action": False,
                    "prompt_language": "zh", "som_mode": "marks",
                    "verify_goal": False},
            dry_run=False,
            control=control,
        )
        state = lp.run("测试任务")
    finally:
        (loopmod.capture_screen, loopmod.get_screen_size,
         loopmod.build_som_mapping, exmod.ActionExecutor.execute) = saved
    return state, prompts, executed


def test_ask_action_answer_reaches_next_prompt() -> None:
    ctl = RunControl(asker=lambda q, opts: "账号B")
    state, prompts, _ = _make_loop(
        [
            '{"type":"ask","text":"用哪个账号？","args":{"options":["账号A","账号B"]}}',
            '{"type":"click","mark":1,"reason":"点搜索"}',
            '{"type":"done","reason":"完成"}',
        ],
        control=ctl,
    )
    joined = "\n".join(prompts)
    assert "账号B" in joined, "用户回答必须出现在后续 prompt 里"
    assert "不要重复提问" in joined
    assert state.success is True
    print("✓ ask 的回答进入了下一轮 prompt，模型不会重复问")


def test_risky_click_is_blocked() -> None:
    el = __import__("yu_agent.perception.a11y", fromlist=["UIElement"]).UIElement(
        id=0, role="AXButton", title="删除文件", x=100, y=100, w=80, h=30, app="Finder"
    )
    ctl = RunControl(approver=lambda desc, risk: False)
    state, prompts, executed = _make_loop(
        [
            '{"type":"click","mark":1,"reason":"删除这个文件"}',
            '{"type":"done","reason":"完成"}',
        ],
        control=ctl,
        elements=[el],
    )
    assert executed == ["done"] or "click" not in executed, (
        f"危险动作不该被执行，实际执行了 {executed}"
    )
    assert any("未执行" in p or "已阻止" in p for p in prompts), "应把拦截原因告诉模型"
    print("✓ 未确认的删除操作被执行前就拦下了")


def test_usage_lands_in_state() -> None:
    state, _prompts, _exec = _make_loop(
        ['{"type":"click","mark":1,"reason":"点搜索"}', '{"type":"done","reason":"完成"}'],
        control=RunControl(),
    )
    usage = state.to_dict().get("usage") or {}
    assert usage.get("calls", 0) >= 2, f"应有决策+总结等多次调用，实际 {usage}"
    assert usage.get("total_tokens", 0) > 0, usage
    print("✓ 用量统计落到了 state 上（CLI/GUI 可直接展示）")


# ── 5. 卡住时的重规划 ───────────────────────────────────────────── #
def test_replan_when_stuck() -> None:
    """原计划走不通时，允许重新拆分一次（否则只能撞上自动停止）。"""
    state, prompts, _exec = _make_loop(
        [
            '{"type":"plan","subtasks":["打开 Safari","在地址栏输入关键词"]}',
            '{"type":"click","mark":1,"reason":"点击地址栏"}',
            '{"type":"click","mark":1,"reason":"再点一次地址栏"}',
            '{"type":"plan","subtasks":["用 spotlight 打开 Safari","直接输入关键词并回车"]}',
            '{"type":"done","reason":"完成"}',
        ],
        control=RunControl(),
    )
    assert state.subtasks == ["用 spotlight 打开 Safari", "直接输入关键词并回车"], (
        f"重规划没生效，当前子任务还是 {state.subtasks}"
    )
    assert any("重新规划" in p for p in prompts), "重规划后应告诉模型换了思路"
    print("✓ 卡住后重规划生效（旧计划被替换）")


# 脚本模式（python tests/test_control.py）：必须放在所有测试函数**之后**，
# 否则最后定义的 test_replan_when_stuck 还没绑定就调用 → NameError。
if __name__ == "__main__":
    test_pause_blocks_and_resume_releases()
    test_pause_can_be_aborted_by_stop()
    test_ask_without_asker_returns_empty()
    test_ask_uses_asker()
    test_usage_meter_accumulates()
    test_usage_without_price_reports_no_cost()
    test_ask_action_answer_reaches_next_prompt()
    test_risky_click_is_blocked()
    test_usage_lands_in_state()
    test_replan_when_stuck()
    print("\nALL PASS")
