"""输入「覆盖」语义回归测试。

背景（2026-10-26 用户反馈）：让 agent 搜索东西，它没有删掉搜索框里的旧记录，
直接在后面接着打，搜索框变成「旧词新词」，搜出来的结果自然是错的。

查了运行轨迹（runs/*/trace.jsonl）：模型从没输出过清空动作，典型序列就是
`click 搜索框 → type 新词 → enter` —— 是**模型没意识到要删**，而执行层也没有兜底。

修复：
1. `type` 动作支持 `replace`（先 ⌘A 全选再输入）；
2. loop 在「上一步点的是单行输入框」时自动打开 replace —— 这类框里输入新内容，
   语义几乎总是替换（多行文本域不自动覆盖，往文档里写正文不能删用户内容）；
3. 提示词明确告诉模型「直接 type 就会覆盖，不要自己发一串 backspace」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image

from yu_agent.actions.schema import Action
from yu_agent.grounding.parser import parse_action
from yu_agent.perception.a11y import UIElement


# ── 1. 解析层：模型写在顶层也要收进来 ──────────────────────────── #
def test_parser_collects_replace_flags() -> None:
    a = parse_action('{"type":"type","text":"新词","replace":true,"reason":"覆盖搜索"}')
    assert a.args.get("replace") is True, a.args
    # args 里写的也要保留
    b = parse_action('{"type":"type","text":"x","args":{"clear":true}}')
    assert b.args.get("clear") is True, b.args
    # 不写就没有（不能凭空给所有 type 加覆盖）
    c = parse_action('{"type":"type","text":"x"}')
    assert "replace" not in c.args and "clear" not in c.args
    print("✓ 解析：顶层 replace/clear 收进 args，不写则不加")


# ── 2. 执行层：replace 时先全选再输入 ────────────────────────────── #
class _FakePG:
    def __init__(self):
        self.calls = []

    def hotkey(self, *keys):
        self.calls.append(("hotkey", tuple(keys)))

    def write(self, text, interval=0.0):
        self.calls.append(("write", text))

    def press(self, key):
        self.calls.append(("press", key))


def _executor_with_fake_pg():
    import types

    from yu_agent.actions.executor import ActionExecutor

    fake = _FakePG()
    mod = types.SimpleNamespace(
        **{k: getattr(fake, k) for k in ("hotkey", "write", "press")}
    )
    mod.FAILSAFE = True
    mod.PAUSE = 0
    old = sys.modules.get("pyautogui")
    sys.modules["pyautogui"] = mod
    try:
        ex = ActionExecutor()
    finally:
        if old is not None:
            sys.modules["pyautogui"] = old
        else:
            sys.modules.pop("pyautogui", None)
    ex.pg = fake                      # 直接用假对象记录调用
    return ex, fake


def test_executor_selects_all_before_typing() -> None:
    ex, pg = _executor_with_fake_pg()
    ex.execute(Action(type="type", text="hello", args={"replace": True}))
    kinds = [c[0] for c in pg.calls]
    assert kinds[:2] == ["hotkey", "write"], pg.calls
    assert pg.calls[0][1] == ("command", "a"), f"macOS 应 ⌘A，实际 {pg.calls[0]}"
    assert pg.calls[1][1] == "hello"

    # 不加 replace 时不许多按 ⌘A（否则往文档里追加文字会覆盖全文）
    pg.calls.clear()
    ex.execute(Action(type="type", text="world"))
    assert [c[0] for c in pg.calls] == ["write"], pg.calls
    print("✓ 执行：replace=True 先 ⌘A 再输入；默认不覆盖")


def test_executor_accepts_string_flags() -> None:
    """模型有时把布尔写成字符串，"true" 也要认。"""
    ex, pg = _executor_with_fake_pg()
    ex.execute(Action(type="type", text="x", args={"clear": "true"}))
    assert pg.calls[0][0] == "hotkey", pg.calls
    print("✓ 执行：args.clear='true' 这类字符串也能识别")


# ── 3. loop：单行输入框自动覆盖，多行文本域不覆盖 ──────────────── #
def _loop_with_script(actions, input_role):
    """构造一个把 build_som_mapping 打桩的 AgentLoop，返回记录到的执行动作。"""
    import yu_agent.actions.executor as exmod
    import yu_agent.agent.loop as loopmod
    from yu_agent.agent.loop import AgentLoop
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore
    from yu_agent.models.base import VLMResponse
    from yu_agent.perception.screenshot import Screen
    from yu_agent.skills import build_default_registry

    seen = []
    el = UIElement(id=0, role=input_role, title="搜索", x=900, y=60, w=300, h=30,
                   app="Safari")

    class Backend:
        name = "fake"

        def __init__(self):
            self.i = 0

        def chat(self, req):
            s = actions[min(self.i, len(actions) - 1)]
            self.i += 1
            return VLMResponse(text=s)

    saved = (loopmod.capture_screen, loopmod.get_screen_size,
             loopmod.build_som_mapping, exmod.ActionExecutor.execute)
    loopmod.capture_screen = lambda: Image.new("RGB", (1200, 800))
    loopmod.get_screen_size = lambda: Screen(1200, 800)
    loopmod.build_som_mapping = lambda img, *a, **k: (img, {1: el}, [el])

    def fake_exec(self, action):
        seen.append(action)

    exmod.ActionExecutor.execute = fake_exec
    try:
        lp = AgentLoop(
            Backend(),
            MemoryManager(store=MemoryStore(db_path=":memory:")),
            build_default_registry(),
            config={"max_steps": 3, "verify_after_action": False, "prompt_language": "en"},
            dry_run=False,
        )
        lp.run("搜索天气")
    finally:
        (loopmod.capture_screen, loopmod.get_screen_size,
         loopmod.build_som_mapping, exmod.ActionExecutor.execute) = saved
    return seen


def test_loop_auto_replace_for_single_line_field() -> None:
    acts = _loop_with_script(
        ['{"type":"click","mark":1,"reason":"focus the search field"}',
         '{"type":"type","text":"今天天气","reason":"type"}',
         '{"type":"done","reason":"ok"}'],
        input_role="AXSearchField",
    )
    typed = [a for a in acts if a.type == "type"]
    assert typed, f"没有执行到 type：{[a.type for a in acts]}"
    assert typed[0].args.get("replace") is True, (
        f"搜索框里的输入应自动覆盖旧内容，实际 args={typed[0].args}"
    )
    print(f"✓ loop：搜索框 → type 自动 replace（args={typed[0].args}）")


def test_loop_no_auto_replace_for_textarea() -> None:
    acts = _loop_with_script(
        ['{"type":"click","mark":1,"reason":"focus the text area"}',
         '{"type":"type","text":"正文","reason":"type"}',
         '{"type":"done","reason":"ok"}'],
        input_role="AXTextArea",
    )
    typed = [a for a in acts if a.type == "type"]
    assert typed, "没有执行到 type"
    assert "replace" not in typed[0].args, (
        "多行文本域不能自动覆盖 —— 会把用户已有正文删掉"
    )
    print("✓ loop：多行文本域不自动覆盖")


def test_prompts_tell_model_to_overwrite() -> None:
    from yu_agent.prompts.loader import load_prompt

    zh = load_prompt("system", "zh")
    en = load_prompt("system", "en")
    assert "覆盖" in zh, "中文提示词要说明「直接 type 会覆盖旧内容」"
    assert "replace" in en.lower() or "replaced" in en.lower(), \
        "英文提示词要说明旧内容会被替换"
    print("✓ 中英文提示词都写明了「直接 type 就会覆盖旧内容」")


if __name__ == "__main__":
    test_parser_collects_replace_flags()
    test_executor_selects_all_before_typing()
    test_executor_accepts_string_flags()
    test_loop_auto_replace_for_single_line_field()
    test_loop_no_auto_replace_for_textarea()
    test_prompts_tell_model_to_overwrite()
    print("\n=== 输入覆盖测试全部通过 ===")
