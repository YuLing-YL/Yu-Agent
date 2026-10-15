"""停止逻辑测试：验证「任务完成后不会一直点击」「用户点停止立即生效」。

用打桩避开真实截屏/真实鼠标，纯逻辑验证。
"""
import sys

sys.path.insert(0, "src")

from PIL import Image

import yu_agent.actions.executor as ex
import yu_agent.agent.loop as loopmod
from yu_agent.agent.loop import AgentLoop
from yu_agent.memory import MemoryManager
from yu_agent.memory.store import MemoryStore
from yu_agent.models.base import VLMResponse
from yu_agent.perception.screenshot import Screen
from yu_agent.skills import build_default_registry

# ── 打桩：避免真实截屏与真实鼠标操作 ──
loopmod.capture_screen = lambda: Image.new("RGB", (1512, 982))
loopmod.get_screen_size = lambda: Screen(1512, 982)
# SOM 打桩成"不标注"，这样 mark 不会被解析出坐标，正好顺便测 mark 兜底
loopmod.build_som_mapping = lambda img, *a, **k: (img, {}, [])

_EXECUTED = []  # 记录真正被执行的动作，用于验证"用户停止后不要再执行"
ex.ActionExecutor.execute = lambda self, action: _EXECUTED.append((action.type, action.x, action.y, action.mark))


class Backend:
    name = "fake"

    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def chat(self, req):
        s = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return VLMResponse(text=s)


def make_memory():
    return MemoryManager(store=MemoryStore(db_path=":memory:"))


def new_loop(seq, **cfg):
    base = {"max_steps": 30, "verify_after_action": False}
    base.update(cfg)
    return AgentLoop(Backend(seq), make_memory(), build_default_registry(),
                     config=base, dry_run=True)


# ── 场景 1：模型永远返回同一动作 → 达到阈值自动停止 ──
st = new_loop(['{"type": "click", "mark": 3, "reason": "点它"}'],
              max_repeated_actions=3, max_no_change_steps=99).run("测试重复")
print(f"场景1(连续重复动作): steps={st.step} success={st.success} auto={st.auto_stopped}")
print(f"  reason: {st.reason}")
assert st.step == 3, f"应在第 3 次重复时停止，实际 {st.step}"
assert st.auto_stopped and st.success

# ── 场景 2：子任务流 plan -> click -> subtask_done -> click -> subtask_done ──
seq = [
    '{"type": "plan", "subtasks": ["打开浏览器", "搜索天气"]}',
    '{"type": "click", "mark": 3, "reason": "打开"}',
    '{"type": "subtask_done", "reason": "已打开浏览器"}',
    '{"type": "click", "mark": 9, "reason": "输入"}',
    '{"type": "subtask_done", "reason": "已搜索完成"}',
]
events = []
lp = AgentLoop(Backend(seq), make_memory(), build_default_registry(),
               config={"max_steps": 20, "verify_after_action": False},
               dry_run=True, callback=lambda e: events.append(e))
st2 = lp.run("打开浏览器搜索天气")
print(f"\n场景2(子任务推进): steps={st2.step} success={st2.success}")
assert st2.success and not st2.auto_stopped
assert st2.step == 5, f"应为 5 步，实际 {st2.step}"

# ── 场景 3：无子任务时直接 done ──
st3 = new_loop(['{"type": "done", "reason": "没什么可做"}'], max_steps=10).run("简单任务")
print(f"\n场景3(直接完成): steps={st3.step} success={st3.success}")
assert st3.success and st3.step == 1 and not st3.auto_stopped

# ── 场景 4：A→B→A→B 交替点击（躲过"连续重复"检测）→ 累计同一目标次数抓住它 ──
seq4 = [
    '{"type": "click", "mark": 1, "reason": "点A"}',
    '{"type": "click", "mark": 2, "reason": "点B"}',
] * 20
st4 = new_loop(seq4, max_repeated_actions=99, max_no_change_steps=99,
               max_same_action_total=3, max_total_clicks=99).run("交替绕圈")
print(f"\n场景4(交替绕圈): steps={st4.step} success={st4.success} auto={st4.auto_stopped}")
print(f"  reason: {st4.reason}")
assert st4.step == 5, f"同一目标累计 3 次应在第 5 步停止，实际 {st4.step}"
assert st4.auto_stopped and st4.success

# ── 场景 5：点击总预算封顶 ──
seq5 = [f'{{"type": "click", "mark": {i}, "reason": "到处点"}}' for i in range(1, 40)]
st5 = new_loop(seq5, max_steps=99, max_repeated_actions=99, max_same_action_total=99,
               max_no_change_steps=99, max_total_clicks=6).run("到处乱点")
print(f"\n场景5(点击预算): steps={st5.step} success={st5.success} auto={st5.auto_stopped}")
assert st5.step == 6, f"点击预算 6 应在第 6 步停止，实际 {st5.step}"
assert st5.auto_stopped and not st5.success, "触顶属于安全截断，不应报成功"

# ── 场景 6：用户点停止 → 当前这一步的动作不再执行 ──


class StoppingBackend(Backend):
    """第 trigger_at 次调用模型后立刻模拟"用户点了停止"。"""

    def __init__(self, seq, trigger_at):
        super().__init__(seq)
        self.trigger_at = trigger_at
        self.loop = None

    def chat(self, req):
        resp = super().chat(req)
        if self.i == self.trigger_at and self.loop is not None:
            self.loop.stop()
        return resp


_EXECUTED.clear()
bk = StoppingBackend(['{"type": "click", "mark": 1, "reason": "点它"}'], trigger_at=2)
lp6 = AgentLoop(bk, make_memory(), build_default_registry(),
                config={"max_steps": 20, "verify_after_action": False}, dry_run=False)
bk.loop = lp6
st6 = lp6.run("打开浏览器")
print(f"\n场景6(停止立即生效): steps={st6.step} executed={len(_EXECUTED)}")
print(f"  reason: {st6.reason}")
assert len(_EXECUTED) == 1, f"停止后不应再有动作被执行，实际执行了 {len(_EXECUTED)} 次"
assert "停止" in st6.reason

# ── 场景 7：模型每轮都返回 plan（无限拆分）→ 忽略重复拆分并在阈值内停止 ──
# 曾经的 bug：plan 分支直接 continue，完全不进防重复计数，于是无限循环
# 「拆分 → 观察 → 拆分 → 观察」，用户只能手动停止。
seq7 = ['{"type": "plan", "subtasks": ["打开浏览器", "搜索天气"], "reason": "拆分"}'] * 30
ev7 = []
lp7 = AgentLoop(Backend(seq7), make_memory(), build_default_registry(),
                config={"max_steps": 30, "verify_after_action": False,
                        "max_plan_rejects": 2},
                dry_run=True, callback=lambda e: ev7.append(e))
st7 = lp7.run("帮我打开浏览器然后搜索今天天气")
print(f"\n场景7(无限拆分): steps={st7.step} success={st7.success} auto={st7.auto_stopped}")
print(f"  reason: {st7.reason}")
assert st7.step == 3, f"首次拆分 + 2 次重复拆分被拒后就该停，实际 {st7.step} 步"
assert st7.auto_stopped and not st7.success
assert "拆" in st7.reason
# 计划只会被接受一次，且子任务列表是第一次那份
assert st7.subtasks == ["打开浏览器", "搜索天气"], f"子任务被反复覆盖：{st7.subtasks}"
assert len([e for e in ev7 if e.get("type") == "plan"]) == 1, "plan 事件应只出现一次"

# ── 场景 8：拆分一次后正常执行（execution 阶段不应被拆分逻辑干扰）──
seq8 = [
    '{"type": "plan", "subtasks": ["打开浏览器", "搜索天气"]}',
    '{"type": "click", "mark": 3, "reason": "点浏览器"}',
    '{"type": "type", "text": "今天天气", "reason": "输入关键词"}',
    '{"type": "subtask_done", "reason": "已打开浏览器"}',
    '{"type": "done", "reason": "全部完成"}',
]
lp8 = AgentLoop(Backend(seq8), make_memory(), build_default_registry(),
                config={"max_steps": 20, "verify_after_action": False},
                dry_run=True)
st8 = lp8.run("打开浏览器搜索天气")
print(f"\n场景8(拆分后正常执行): steps={st8.step} success={st8.success} auto={st8.auto_stopped}")
assert st8.success and not st8.auto_stopped
assert st8.step == 5, f"应为 5 步，实际 {st8.step}"

# ── 场景 9：中间偶发一次 plan（有实际操作推进）→ 只忽略，不该误停 ──
seq9 = [
    '{"type": "plan", "subtasks": ["打开浏览器", "搜索天气"]}',
    '{"type": "click", "mark": 1, "reason": "点浏览器"}',
    '{"type": "plan", "subtasks": ["打开浏览器", "搜索天气"]}',  # 偶发重复拆分
    '{"type": "click", "mark": 2, "reason": "点搜索框"}',
    '{"type": "subtask_done", "reason": "已打开浏览器"}',
    '{"type": "done", "reason": "全部完成"}',
]
lp9 = AgentLoop(Backend(seq9), make_memory(), build_default_registry(),
                config={"max_steps": 20, "verify_after_action": False,
                        "max_plan_rejects": 2},
                dry_run=True)
st9 = lp9.run("打开浏览器搜索天气")
print(f"\n场景9(偶发重复拆分): steps={st9.step} success={st9.success} auto={st9.auto_stopped}")
assert st9.success and not st9.auto_stopped, f"不该误停：{st9.reason}"
assert st9.step == 6, f"应为 6 步，实际 {st9.step}"

print("\nALL PASS")
