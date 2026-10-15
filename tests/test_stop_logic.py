import sys

sys.path.insert(0, "src")

from PIL import Image

import yu_agent.agent.loop as loopmod
import yu_agent.actions.executor as ex
from yu_agent.agent.loop import AgentLoop
from yu_agent.memory import MemoryManager
from yu_agent.memory.store import MemoryStore
from yu_agent.models.base import VLMResponse
from yu_agent.perception.screenshot import Screen
from yu_agent.skills import build_default_registry

# 打桩：避免真实截屏与真实鼠标操作
loopmod.capture_screen = lambda: Image.new("RGB", (1512, 982))
loopmod.get_screen_size = lambda: Screen(1512, 982)
loopmod.build_som_mapping = lambda img, *a, **k: (img, {}, [])
ex.ActionExecutor.execute = lambda self, action: None


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


# 场景1：模型永远返回同一动作 → 达到阈值自动停止
st = AgentLoop(
    Backend(['{"type": "click", "mark": 3, "reason": "点它"}']),
    make_memory(),
    build_default_registry(),
    config={"max_steps": 30, "max_repeated_actions": 3,
            "max_no_change_steps": 99, "verify_after_action": False},
    dry_run=True,
).run("测试重复")
print(f"场景1(重复动作): steps={st.step} success={st.success}")
print(f"  reason: {st.reason}")
assert st.step == 3, f"应在第 3 次重复时停止，实际 {st.step}"
assert not st.success

# 场景2：子任务流 plan -> click -> subtask_done -> click -> subtask_done
seq = [
    '{"type": "plan", "subtasks": ["打开浏览器", "搜索天气"]}',
    '{"type": "click", "mark": 3, "reason": "打开"}',
    '{"type": "subtask_done", "reason": "已打开浏览器"}',
    '{"type": "click", "mark": 9, "reason": "输入"}',
    '{"type": "subtask_done", "reason": "已搜索完成"}',
]
events = []
lp = AgentLoop(
    Backend(seq),
    make_memory(),
    build_default_registry(),
    config={"max_steps": 20, "verify_after_action": False},
    dry_run=True,
    callback=lambda e: events.append(e),
)
st2 = lp.run("打开浏览器搜索天气")
print(f"\n场景2(子任务推进): steps={st2.step} success={st2.success}")
print(f"  reason: {st2.reason}")
for e in events:
    if e["type"] in ("plan", "subtask"):
        print("   ", e)
assert st2.success, "子任务流应成功结束"
assert st2.step == 5, f"应为 5 步，实际 {st2.step}"

# 场景3：无子任务时直接 done
st3 = AgentLoop(
    Backend(['{"type": "done", "reason": "没什么可做"}']),
    make_memory(),
    build_default_registry(),
    config={"max_steps": 10, "verify_after_action": False},
    dry_run=True,
).run("简单任务")
print(f"\n场景3(直接完成): steps={st3.step} success={st3.success}")
assert st3.success and st3.step == 1

print("\nALL PASS")
