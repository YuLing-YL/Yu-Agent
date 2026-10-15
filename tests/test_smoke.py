"""端到端冒烟测试：验证所有模块可正常导入并跑通核心逻辑（离线，无需 API/屏幕）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_imports() -> None:
    """验证所有核心模块可导入。"""
    from yu_agent import YuAgent, build_agent_from_settings
    from yu_agent.actions import Action, ActionExecutor, describe_actions
    from yu_agent.agent import AgentLoop, Planner, Reflector, AgentState
    from yu_agent.grounding import GroundingResolver, parse_action, parse_actions
    from yu_agent.memory import MemoryManager
    from yu_agent.models import get_backend, VLMRequest, VLMResponse
    from yu_agent.perception import capture_screen, get_screen_size, Observation
    from yu_agent.safety import check_action_allowed, detect_injection
    from yu_agent.settings import get_settings, reload_settings
    from yu_agent.skills import build_default_registry

    print("✓ 所有核心模块导入成功")


def test_full_loop_mock() -> None:
    """用 mock 后端跑完整闭环（内存记忆库，避免沙箱 journal 限制）。"""
    from yu_agent.agent_facade import YuAgent
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore

    memory = MemoryManager(store=MemoryStore(db_path=":memory:"))
    agent = YuAgent(use_mock=True, dry_run=True, memory=memory)

    state = agent.run("打开浏览器搜索天气")
    agent.close()
    assert state.step > 0, "应至少执行一步"
    assert state.success, "mock 闭环应以成功结束"
    print(f"✓ mock 闭环成功：{state.step} 步，子任务 {len(state.subtasks)} 个")


def test_memory_persistence() -> None:
    """验证记忆可读写（内存库）。"""
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore

    m = MemoryManager(store=MemoryStore(db_path=":memory:"))
    m.set_preference("测试键", "测试值")
    prefs = m.profile_context()
    assert "测试键" in prefs
    print("✓ 记忆读写成功")


if __name__ == "__main__":
    test_imports()
    test_full_loop_mock()
    test_memory_persistence()
    print("\n=== 冒烟测试全部通过 ===")
