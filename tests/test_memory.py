"""记忆系统回归测试：工作记忆（短期）+ 蒸馏/召回（长期）。

背景（2026-10-26 用户要求）：给 agent 装上短期和长期记忆。
- 短期：知道「我上一步干了什么」「这一步该干什么」；
- 长期：任务结束后把轨迹总结提炼，重要的部分存下来，下次能召回。

改造前实测的两个问题，这里都锁死：
1. 工作记忆记的是 `界面=执行click | 动作=click(513,127)` —— 模型完全看不懂，
   等于没记（而且和子任务提示重复占 token）；
2. 长期记忆只是把动作名串起来（"轨迹: skill;click;type"），
   甚至写成「完成任务『打开微信』的方法已沉淀，共 2 步。」—— 没有任何信息量。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yu_agent.actions.schema import Action
from yu_agent.memory import MemoryManager
from yu_agent.memory.retrieval import MemoryRetriever
from yu_agent.memory.store import MemoryStore
from yu_agent.memory.working import StepRecord, WorkingMemory
from yu_agent.perception.a11y import UIElement


def _mem() -> MemoryManager:
    return MemoryManager(store=MemoryStore(db_path=":memory:"))


# ── 1. 工作记忆 ─────────────────────────────────────────────────── #
def test_working_memory_reads_like_a_story() -> None:
    w = WorkingMemory()
    w.reset("在Safari搜索今天天气")
    w.set_subtasks(["打开Safari", "输入关键词"])
    w.current_subtask = 1
    w.add_step(StepRecord(step=1, observation_summary="WorkBuddy，30 个可点元素",
                          action="点击「搜索框」", result="已执行 click", effect="changed"))
    w.add_step(StepRecord(step=2, observation_summary="Safari，18 个可点元素",
                          action="输入「今天天气」", result="已执行 type", effect="no_change"))

    text = w.context_for_prompt("zh", include_plan=False)
    assert "点击「搜索框」" in text, text
    assert "输入「今天天气」" in text, text
    assert "屏幕没变化" in text, text
    # 最近的一步排在最上面（模型一眼看到"上一步"）
    assert text.index("步骤2") < text.index("步骤1"), f"应倒序：\n{text}"
    # 无变化时要给出明确警示，避免模型重复同一个动作
    assert "换一种做法" in text, text
    # 不应再把子任务计划也塞进来（由 _build_subtask_hint 负责）
    assert "子任务计划" not in text, text
    print("✓ 工作记忆可读、倒序、带「无变化」警示、不重复子任务")


def test_working_memory_keeps_only_recent_steps() -> None:
    w = WorkingMemory(max_recent_steps=3)
    for i in range(1, 8):
        w.add_step(StepRecord(step=i, observation_summary="X", action=f"第{i}步", result=""))
    text = w.context_for_prompt("zh")
    assert "第7步" in text and "第5步" in text
    assert "第3步" not in text, "超出窗口的旧步骤应被丢掉"
    print("✓ 工作记忆只保留最近 N 步")


def test_step_action_description() -> None:
    """动作要翻译成人话，不能是 click(513,127)。"""
    from yu_agent.agent.loop import AgentLoop

    class _Stub:
        prompt_lang = "zh"

    el = UIElement(id=0, role="AXButton", title="搜索", x=100, y=100, w=50, h=20)
    loop = _Stub()
    d1 = AgentLoop._describe_step_action(loop, Action(type="click", mark=1), {1: el})
    assert d1 == "点击「搜索」", d1

    d2 = AgentLoop._describe_step_action(loop, Action(type="type", text="今天天气"))
    assert "今天天气" in d2 and "输入" in d2, d2

    d3 = AgentLoop._describe_step_action(loop, Action(type="key", key="enter"))
    assert "enter" in d3, d3

    loop.prompt_lang = "en"
    d4 = AgentLoop._describe_step_action(loop, Action(type="click", mark=1), {1: el})
    # 动词跟着提示词语言走；元素名保持屏幕上的原样（中文名不硬翻）
    assert d4.lower().startswith("clicked") and "搜索" in d4, d4
    print(f"✓ 动作描述：{d1} / {d2} / {d3} / {d4}")


# ── 2. 蒸馏（长期记忆） ──────────────────────────────────────────── #
_STEPS = [
    {"step": 1, "action": "skill", "action_desc": "调用技能 open_app",
     "reason": "打开Safari", "result": "已打开 Safari"},
    {"step": 2, "action": "click", "action_desc": "点击「智能搜索栏」",
     "reason": "聚焦地址栏", "result": "已执行 click"},
    {"step": 3, "action": "type", "action_desc": "输入「今天天气」",
     "reason": "输入关键词", "result": "已执行 type"},
]


def _summarizer(payload: dict):
    def _f(system: str, user: str) -> str:
        assert "任务" in user or "Task" in user, "总结请求里应带任务描述"
        assert "步骤" in user or "Steps" in user, "总结请求里应带步骤"
        return json.dumps(payload, ensure_ascii=False)

    return _f


def test_distill_with_model_summary() -> None:
    m = _mem()
    out = m.finish_task(
        "在Safari搜索今天天气", _STEPS, True,
        summarize=_summarizer({
            "summary": "在 Safari 里搜索「今天天气」并成功打开结果页。",
            "lesson": "搜索新词时先点地址栏直接输入，旧内容会自动覆盖。",
            "steps": ["调用 open_app 打开 Safari", "点击地址栏", "输入关键词", "按 enter"],
        }),
        language="zh",
    )
    assert out["used_llm"] is True, out
    view = m.memory_view()
    assert len(view["episodic"]) == 1, view
    assert "Safari" in view["episodic"][0]["content"]
    assert len(view["semantic"]) == 1, "提炼出的经验应进语义记忆"
    assert len(view["procedural"]) == 1, "成功任务的可复用步骤应进程序记忆"
    print(f"✓ 模型总结：{out['summary']}")


def test_distill_falls_back_without_model() -> None:
    """没有模型 / 模型报错时，必须降级成模板总结，绝不能抛异常影响任务。"""
    m = _mem()
    out = m.finish_task("打开计算器", _STEPS, True, summarize=None, language="zh")
    assert out["used_llm"] is False
    assert out["summary"], "模板总结也要有内容"
    assert "3 步" in out["summary"], out["summary"]

    def boom(system, user):
        raise RuntimeError("模型挂了")

    out2 = m.finish_task("打开计算器", _STEPS, False, summarize=boom, language="zh")
    assert out2["used_llm"] is False and out2["summary"], out2
    print(f"✓ 总结降级：{out2['summary'][:50]}")


def test_failed_task_records_but_no_procedure() -> None:
    """失败也要记（教训更值钱），但不能把失败路径存成「怎么做」的模板。"""
    m = _mem()
    m.finish_task("在微信里找张三", _STEPS, False, summarize=_summarizer({
        "summary": "没能在微信里定位到张三的聊天记录。",
        "lesson": "微信要先点搜索图标再输入，直接点输入区点不到。",
        "steps": ["点搜索", "点输入框"],       # 模型硬给了步骤也不该落成模板
    }), language="zh")
    view = m.memory_view()
    assert len(view["episodic"]) == 1
    assert view["episodic"][0]["success"] == 0, "应标记为失败"
    assert len(view["procedural"]) == 0, "失败路径不能当模板"
    assert len(view["semantic"]) == 1, "失败提炼出的教训要留下"
    print("✓ 失败任务：记情节 + 记教训，不记模板")


def test_distill_is_idempotent() -> None:
    """同一条任务跑多次，不该堆出一堆重复记忆。"""
    m = _mem()
    payload = {"summary": "搜索天气成功。", "lesson": "先点地址栏再输入。",
               "steps": ["打开 Safari", "输入", "回车"]}
    for _ in range(3):
        m.finish_task("搜索天气", _STEPS, True,
                      summarize=_summarizer(payload), language="zh")
    view = m.memory_view()
    assert len(view["episodic"]) == 1, f"情节记忆重复了：{len(view['episodic'])}"
    assert len(view["semantic"]) == 1, f"语义记忆重复了：{len(view['semantic'])}"
    assert len(view["procedural"]) == 1, f"程序记忆重复了：{len(view['procedural'])}"
    print("✓ 重复蒸馏不会堆重复记忆")


# ── 3. 召回 ─────────────────────────────────────────────────────── #
def test_recall_chinese_without_spaces() -> None:
    """中文没有空格，按空格分词会召回 0 条（踩过的坑）。"""
    m = _mem()
    m.finish_task("在Safari搜索今天天气", _STEPS, True, summarize=_summarizer({
        "summary": "在 Safari 搜索天气成功。",
        "lesson": "先点地址栏再输入关键词。",
        "steps": ["打开 Safari", "输入", "回车"],
    }), language="zh")

    kws = MemoryRetriever._extract_keywords("帮我用Safari搜一下明天的天气")
    assert "Safari" in kws, kws
    assert any("天气" in k for k in kws), kws

    ctx = m.recall_context("帮我用Safari搜一下明天的天气", language="zh")
    assert ctx and "Safari" in ctx, f"应召回 Safari 相关记忆，实际：{ctx!r}"
    print("✓ 中文长句能召回（滑窗分词）")


def test_recall_marks_failed_steps() -> None:
    """失败教训必须带醒目标记，否则模型会把失败路径当成功经验照着做。"""
    m = _mem()
    m.finish_task("在微信里找张三", _STEPS, False, summarize=_summarizer({
        "summary": "微信里没找到张三。",
        "lesson": "微信要先展开搜索框。",
        "steps": [],
    }), language="zh")
    ctx = m.recall_context("微信里找李四", language="zh")
    assert "上次没成功" in ctx, ctx
    print("✓ 失败记忆带「上次没成功」标记")


def test_recall_is_length_bounded() -> None:
    """记忆是辅助信息，不能把 prompt 撑爆。"""
    m = _mem()
    for i in range(8):
        m.finish_task(f"任务{i} 打开浏览器", _STEPS, True, summarize=_summarizer({
            "summary": "x" * 200,
            "lesson": "y" * 120,
            "steps": ["a" * 50, "b" * 50],
        }), language="zh")
    ctx = m.recall_context("打开浏览器", language="zh")
    assert len(ctx) <= 900, f"召回上下文过长：{len(ctx)}"
    print(f"✓ 召回上下文限长：{len(ctx)} 字符")


def test_loop_injects_working_memory_into_prompt() -> None:
    """端到端：跑三步，第三步送给模型的 prompt 里必须带上前两步的记录。

    这是「工作记忆真的装上了」的唯一硬证据 —— 前面测的都是函数本身，
    只有这里能证明它真的到了模型眼前。
    """
    from PIL import Image

    import yu_agent.actions.executor as exmod
    import yu_agent.agent.loop as loopmod
    from yu_agent.agent.loop import AgentLoop
    from yu_agent.models.base import VLMResponse
    from yu_agent.perception.screenshot import Screen
    from yu_agent.skills import build_default_registry

    prompts: list[str] = []
    seq = [
        '{"type":"click","mark":1,"reason":"focus the search field"}',
        '{"type":"type","text":"今天天气","reason":"type the keyword"}',
        '{"type":"done","reason":"ok"}',
    ]

    class Backend:
        name = "capture"

        def __init__(self):
            self.i = 0

        def chat(self, req):
            prompts.append(req.prompt)
            s = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return VLMResponse(text=s)

    el = UIElement(id=0, role="AXSearchField", title="搜索",
                   x=900, y=60, w=300, h=30, app="Safari")
    saved = (loopmod.capture_screen, loopmod.get_screen_size,
             loopmod.build_som_mapping, exmod.ActionExecutor.execute)
    loopmod.capture_screen = lambda: Image.new("RGB", (1200, 800))
    loopmod.get_screen_size = lambda: Screen(1200, 800)
    loopmod.build_som_mapping = lambda img, *a, **k: (img, {1: el}, [el])
    exmod.ActionExecutor.execute = lambda self, a: None
    try:
        lp = AgentLoop(
            Backend(),
            _mem(),
            build_default_registry(),
            config={"max_steps": 3, "verify_after_action": False,
                    "prompt_language": "zh", "som_mode": "marks"},
            dry_run=False,
        )
        lp.run("在Safari搜索今天天气")
    finally:
        (loopmod.capture_screen, loopmod.get_screen_size,
         loopmod.build_som_mapping, exmod.ActionExecutor.execute) = saved

    # 注意：任务结束时还会调一次模型做记忆总结，那不是决策 prompt，要排除掉
    decisions = [p for p in prompts if "观察当前屏幕截图" in p]
    assert len(decisions) >= 3, f"应有 3 次决策调用，实际 {len(decisions)}"
    last = decisions[-1]
    assert "你已经做过的" in last, f"第三步的 prompt 缺少工作记忆：\n{last[-800:]}"
    assert "搜索" in last and "今天天气" in last, (
        f"工作记忆里应记录「点击了搜索」和「输入了今天天气」：\n{last[-800:]}"
    )
    print("✓ 工作记忆真的进了 prompt（第三步能看到前两步）")


if __name__ == "__main__":
    test_working_memory_reads_like_a_story()
    test_working_memory_keeps_only_recent_steps()
    test_step_action_description()
    test_distill_with_model_summary()
    test_distill_falls_back_without_model()
    test_failed_task_records_but_no_procedure()
    test_distill_is_idempotent()
    test_recall_chinese_without_spaces()
    test_recall_marks_failed_steps()
    test_recall_is_length_bounded()
    test_loop_injects_working_memory_into_prompt()
    print("\n=== 记忆系统测试全部通过 ===")
