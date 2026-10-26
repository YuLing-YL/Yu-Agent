"""单步执行（调试面板）的闭环测试。

要点：
1. step_mode=True 时，每执行完一步就暂停，必须 request_next_step() 才往下走
2. 暂停中调用 stop() 要能立刻退出，绝不能把线程挂死
3. 每步都要发 step_detail（标注图路径 / 编号表 / 模型原始输出）给界面
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image

import yu_agent.actions.executor as exmod
import yu_agent.agent.loop as loopmod
from yu_agent.agent.loop import AgentLoop
from yu_agent.memory import MemoryManager
from yu_agent.memory.store import MemoryStore
from yu_agent.models.base import VLMResponse
from yu_agent.perception.screenshot import Screen
from yu_agent.skills import build_default_registry

ORIG = (
    loopmod.capture_screen,
    loopmod.get_screen_size,
    loopmod.build_som_mapping,
    exmod.ActionExecutor.execute,
)


def patch_env():
    loopmod.capture_screen = lambda: Image.new("RGB", (400, 300))
    loopmod.get_screen_size = lambda: Screen(400, 300)
    loopmod.build_som_mapping = lambda img, *a, **k: (img, {}, [])
    exmod.ActionExecutor.execute = lambda self, action: None


def unpatch_env():
    (
        loopmod.capture_screen,
        loopmod.get_screen_size,
        loopmod.build_som_mapping,
        exmod.ActionExecutor.execute,
    ) = ORIG


def make_agent(events: list, steps: int = 4):
    """造一个假模型后端。

    ⚠️ 必须在**将要执行任务的同一个线程**里调用：
    MemoryStore 里的 sqlite 连接不能跨线程使用（真机 GUI 也是 worker 线程里建 agent）。
    """
    class Backend:
        name = "capture"

        def __init__(self):
            self.i = 0

        def chat(self, req):
            self.i += 1
            # 交替点两个位置，避免触发「同一目标 3 次」的自动停止
            x = 100 if self.i % 2 else 120
            return VLMResponse(
                text=f'{{"type": "click", "x": {x}, "y": 150, "reason": "step {self.i}"}}'
            )

    return AgentLoop(
        Backend(),
        MemoryManager(store=MemoryStore(db_path=":memory:")),
        build_default_registry(),
        config={"max_steps": steps, "verify_after_action": False},
        dry_run=True,
        callback=lambda e: events.append(e),
    )


def start_run(steps: int, step_mode: bool):
    """在子线程里建 agent 并跑任务（模拟 GUI 的 worker 线程），返回 (thread, box, events)。"""
    box: dict = {}
    events: list = []

    def target():
        lp = make_agent(events, steps=steps)
        lp.step_mode = step_mode
        box["lp"] = lp
        lp.run("测试单步")

    th = threading.Thread(target=target, daemon=True)
    return th, box, events


def wait_for(events: list, etype: str, count: int = 1, timeout: float = 8.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if sum(1 for e in events if e.get("type") == etype) >= count:
            return True
        time.sleep(0.02)
    return False


def test_step_mode_pauses_each_step() -> None:
    """每一步都要停下来等放行，而且每步的详情都要推给界面。"""
    patch_env()
    try:
        th, box, events = start_run(steps=3, step_mode=True)
        th.start()

        # 第 1 步跑完 → 应停在 paused
        assert wait_for(events, "paused", 1), "第 1 步后没有暂停"
        assert th.is_alive(), "暂停时线程应当还在（阻塞等放行）"
        assert sum(1 for e in events if e.get("type") == "step_detail") == 1, (
            "每步都应发 step_detail"
        )

        # 不放行 → 0.6 秒内不应有第 2 步
        time.sleep(0.6)
        assert sum(1 for e in events if e.get("type") == "step_detail") == 1, (
            "没点「下一步」却继续执行了"
        )

        # 放行两次 → 应走到第 2、3 步
        box["lp"].request_next_step()
        assert wait_for(events, "paused", 2), "第 2 步后没有暂停"
        box["lp"].request_next_step()
        assert wait_for(events, "paused", 3), "第 3 步后没有暂停"

        # 放行最后一次 → 达 max_steps 结束
        box["lp"].request_next_step()
        th.join(timeout=5)
        assert not th.is_alive(), "放行后线程应及时结束"

        details = [e for e in events if e.get("type") == "step_detail"]
        assert len(details) == 3, f"应有 3 条 step_detail，实际 {len(details)}"
        d = details[0]
        for key in ("image", "marks_desc", "model_raw", "action", "run_dir"):
            assert key in d, f"step_detail 缺少字段 {key}"
        assert "click" in d["model_raw"], d["model_raw"]
        print(f"✓ 单步：3 步各暂停一次，step_detail 字段齐全（image={bool(d['image'])}）")
    finally:
        unpatch_env()


def test_non_step_mode_runs_through() -> None:
    """不开单步时不应有任何 paused。"""
    patch_env()
    try:
        th, box, events = start_run(steps=3, step_mode=False)
        th.start()
        th.join(timeout=8)
        assert not th.is_alive(), "非单步模式应一次跑完"
        assert not any(e.get("type") == "paused" for e in events), "非单步模式不该暂停"
        assert sum(1 for e in events if e.get("type") == "step_detail") == 3
        print("✓ 非单步：一次跑完 3 步，没有暂停")
    finally:
        unpatch_env()


def test_stop_while_paused_exits() -> None:
    """暂停中按停止，必须立刻退出（否则关窗口会挂死）。"""
    patch_env()
    try:
        th, box, events = start_run(steps=5, step_mode=True)
        th.start()

        assert wait_for(events, "paused", 1), "第 1 步后没有暂停"
        box["lp"].stop()
        th.join(timeout=5)
        assert not th.is_alive(), "暂停中 stop() 没能让线程退出（会卡死 GUI）"
        print("✓ 暂停中点停止：线程立刻退出")
    finally:
        unpatch_env()


if __name__ == "__main__":
    test_step_mode_pauses_each_step()
    test_non_step_mode_runs_through()
    test_stop_while_paused_exits()
    print("\n=== 单步执行测试全部通过 ===")
