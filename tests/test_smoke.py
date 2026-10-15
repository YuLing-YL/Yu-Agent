"""端到端冒烟测试：验证核心模块可导入 + 坐标还原 + 意图识别（离线，无需 API/屏幕）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_imports() -> None:
    """验证所有核心模块可导入。"""
    from yu_agent import YuAgent, build_agent_from_settings
    from yu_agent.actions import Action, ActionExecutor, describe_actions
    from yu_agent.agent import AgentLoop, Planner, Reflector, Responder, AgentState
    from yu_agent.grounding import GroundingResolver, parse_action, parse_actions
    from yu_agent.memory import MemoryManager
    from yu_agent.models import get_backend, VLMRequest, VLMResponse
    from yu_agent.perception import capture_screen, get_screen_size, Observation
    from yu_agent.safety import check_action_allowed, detect_injection
    from yu_agent.settings import get_settings, reload_settings
    from yu_agent.skills import build_default_registry

    print("✓ 所有核心模块导入成功")


def test_coord_mapping() -> None:
    """验证坐标还原逻辑。"""
    from PIL import Image

    from yu_agent.common.image import scale_factor, scale_image
    from yu_agent.perception.screenshot import region_to_pixel

    img = Image.new("RGB", (1728, 1117))
    scaled = scale_image(img, 1568)
    sx, sy = scale_factor(img, scaled)
    x, y = region_to_pixel(784, 500, sx, sy)
    assert 0 <= x <= 1728 and 0 <= y <= 1117
    # 比例一致性
    assert abs(x / 1728 - 784 / scaled.size[0]) < 0.01
    print(f"✓ 坐标还原正确：缩放图(784,500) -> 逻辑像素({x},{y})")


def test_responder() -> None:
    """验证意图识别模块（用 mock backend 测解析逻辑）。"""
    from yu_agent.agent.responder import Responder

    # 用 mock backend，让它返回预设 JSON
    class FakeBackend:
        name = "fake"

        def __init__(self, output: str):
            self.output = output

        def chat(self, req):
            from yu_agent.models.base import VLMResponse

            return VLMResponse(text=self.output)

    # 打招呼 → 不需要 GUI
    r = Responder(FakeBackend('{"needs_gui": false, "reply": "你好！我是 Yu-Agent"}'))
    result = r.respond("你好")
    assert result["needs_gui"] is False
    assert result["reply"]
    print(f"✓ 意图识别（闲聊）：{result}")

    # 操作任务 → 需要 GUI
    r2 = Responder(FakeBackend('{"needs_gui": true, "reply": "好的，我来帮你打开浏览器"}'))
    result2 = r2.respond("打开浏览器")
    assert result2["needs_gui"] is True
    print(f"✓ 意图识别（任务）：{result2}")

    # 解析失败 + 纯文本含动作词 → 启发式判断需要 GUI
    r3 = Responder(FakeBackend("好的，我来帮你搜索天气"))
    result3 = r3.respond("搜索天气")
    assert result3["needs_gui"] is True
    print(f"✓ 解析失败（含动作词）：{result3}")

    # 解析失败 + 纯文本打招呼 → 启发式判断不需要 GUI
    r4 = Responder(FakeBackend("你好，我是 Yu-Agent"))
    result4 = r4.respond("你好")
    assert result4["needs_gui"] is False
    print(f"✓ 解析失败（打招呼）：{result4}")

    # 空输出 → 保守地需要 GUI
    r5 = Responder(FakeBackend(""))
    result5 = r5.respond("随便")
    assert result5["needs_gui"] is True
    print(f"✓ 空输出降级：{result5}")


def test_memory() -> None:
    """验证记忆可读写（内存库）。"""
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore

    m = MemoryManager(store=MemoryStore(db_path=":memory:"))
    m.set_preference("测试键", "测试值")
    prefs = m.profile_context()
    assert "测试键" in prefs
    print("✓ 记忆读写成功")


def test_saved_models() -> None:
    """验证模型历史的存取（内存验证，不写真实配置）。"""
    from yu_agent.settings import get_saved_models

    # 只验证函数可调用
    models = get_saved_models()
    assert isinstance(models, list)
    print("✓ 模型历史函数可用")


if __name__ == "__main__":
    test_imports()
    test_coord_mapping()
    test_responder()
    test_memory()
    test_saved_models()
    print("\n=== 冒烟测试全部通过 ===")
