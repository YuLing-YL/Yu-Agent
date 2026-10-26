"""运行记录目录（runs/）的回归测试：为什么用户看到「保存的图片全是黑的」。

根因不是截图坏了，而是**测试把自己的假截图写进了用户真实的 runs/**：
测试用 `Image.new("RGB", ...)` 当屏幕（默认全黑），每跑一次 pytest 就往 runs/
里丢十几个 `step_XX.png` 全黑图。它们时间戳最新，用户按时间打开看到的自然全黑。

这里锁死三件事：
1. `YU_AGENT_RUNS_DIR` 能整体改道（conftest 就靠它把测试隔离到临时目录）；
2. 同一秒内连续分配目录不撞名（否则 trace 追加混写、step 图互相覆盖）；
3. `loop.save_trace=false` 时**完全不建目录、不写图**。

⚠️ 不用 pytest 的 tmp_path：本机沙箱里它会在 /private/var/folders 下 mkdir 被拒，
统一用项目内的 tests/_tmp_runs（跑完删掉）。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image

from yu_agent.common import paths

_TMP = Path(__file__).resolve().parent / "_tmp_runs"


def _fresh(name: str) -> Path:
    d = _TMP / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cleanup() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)


def test_runs_dir_honours_env(monkeypatch) -> None:
    root = _fresh("env")
    try:
        monkeypatch.setenv("YU_AGENT_RUNS_DIR", str(root / "custom-runs"))
        d = paths.new_run_dir()
        assert str(d).startswith(str(root / "custom-runs")), d
        assert d.is_dir()
        print(f"✓ 环境变量改道生效：{d}")
    finally:
        _cleanup()


def test_new_run_dir_unique_in_same_second(monkeypatch) -> None:
    """同一秒里连续两次（GUI 重跑 / 连续两个用例）必须是两个目录。"""
    root = _fresh("unique")
    try:
        monkeypatch.setenv("YU_AGENT_RUNS_DIR", str(root))
        a = paths.new_run_dir()
        b = paths.new_run_dir()
        assert a != b, f"两次调用拿到同一个目录：{a}"
        print(f"✓ 同秒不撞名：{a.name} / {b.name}")
    finally:
        _cleanup()


def test_is_blank_detects_black_and_content() -> None:
    """纯黑/纯白帧要认出来，正常画面不能误判（否则会天天误报）。"""
    from PIL import Image, ImageDraw

    from yu_agent.common.image import is_blank

    black = Image.new("RGB", (400, 300))
    white = Image.new("RGB", (400, 300), (255, 255, 255))
    assert is_blank(black), "全黑图没被认出来"
    assert is_blank(white), "全白图没被认出来"

    # 有内容的桌面：几个色块 + 文字
    busy = Image.new("RGB", (400, 300), (240, 240, 242))
    d = ImageDraw.Draw(busy)
    d.rectangle([20, 20, 200, 80], fill=(20, 60, 160))
    d.rectangle([30, 120, 380, 280], fill=(250, 250, 250), outline=(80, 80, 80))
    assert not is_blank(busy), "有画面的图被误判成纯黑"

    # 只差一点点亮度（压暗的壁纸）也算"没内容"，但差得多就不是
    dim = Image.new("RGB", (50, 50), (3, 3, 3))
    assert is_blank(dim), "近黑图应算纯色"
    print("✓ is_blank：全黑/全白/近黑认得出，有画面的不误判")


def test_loop_warns_once_on_black_screen(monkeypatch) -> None:
    """截到纯黑帧时要告警一次（模型看不到画面，别让它照着黑屏瞎点）。"""
    import yu_agent.agent.loop as loopmod
    from yu_agent.agent.loop import AgentLoop
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore
    from yu_agent.models.base import VLMResponse
    from yu_agent.perception.screenshot import Screen
    from yu_agent.skills import build_default_registry

    root = _fresh("blank")
    events = []

    class Backend:
        name = "fake"

        def chat(self, req):
            return VLMResponse(text='{"type": "done", "reason": "ok"}')

    saved = (loopmod.capture_screen, loopmod.get_screen_size, loopmod.build_som_mapping)
    loopmod.capture_screen = lambda: Image.new("RGB", (200, 120))   # 纯黑
    loopmod.get_screen_size = lambda: Screen(200, 120)
    loopmod.build_som_mapping = lambda img, *a, **k: (img, {}, [])
    try:
        monkeypatch.setenv("YU_AGENT_RUNS_DIR", str(root))
        lp = AgentLoop(
            Backend(),
            MemoryManager(store=MemoryStore(db_path=":memory:")),
            build_default_registry(),
            config={"max_steps": 1, "verify_after_action": False, "save_trace": False,
                    "prompt_language": "zh"},
            dry_run=True,
            callback=events.append,
        )
        lp.run("看一眼屏幕")
    finally:
        (loopmod.capture_screen, loopmod.get_screen_size, loopmod.build_som_mapping) = saved
        _cleanup()

    warns = [e for e in events if e.get("type") == "notice" and "纯黑" in str(e.get("message"))]
    assert warns, [e.get("type") for e in events]
    assert len(warns) == 1, f"同一次任务只该告警一次，实际 {len(warns)} 次"
    print("✓ 纯黑帧告警一次：", warns[0]["message"][:28])


def test_save_trace_off_writes_nothing(monkeypatch) -> None:
    """save_trace=false：一步都不落盘，目录也不建。"""
    import yu_agent.agent.loop as loopmod
    from yu_agent.agent.loop import AgentLoop
    from yu_agent.memory import MemoryManager
    from yu_agent.memory.store import MemoryStore
    from yu_agent.models.base import VLMResponse
    from yu_agent.perception.screenshot import Screen
    from yu_agent.skills import build_default_registry

    root = _fresh("save_trace")
    created = []

    def spy():
        d = paths.new_run_dir()
        created.append(d)
        return d

    class Backend:
        name = "fake"

        def chat(self, req):
            return VLMResponse(text='{"type": "done", "reason": "ok"}')

    saved = (loopmod.capture_screen, loopmod.get_screen_size,
             loopmod.build_som_mapping, loopmod.new_run_dir)
    loopmod.capture_screen = lambda: Image.new("RGB", (200, 120))
    loopmod.get_screen_size = lambda: Screen(200, 120)
    loopmod.build_som_mapping = lambda img, *a, **k: (img, {}, [])
    loopmod.new_run_dir = spy
    try:
        monkeypatch.setenv("YU_AGENT_RUNS_DIR", str(root))
        lp = AgentLoop(
            Backend(),
            MemoryManager(store=MemoryStore(db_path=":memory:")),
            build_default_registry(),
            config={
                "max_steps": 1, "verify_after_action": False, "save_trace": False,
                "prompt_language": "en",
            },
            dry_run=True,
        )
        lp.run("随便做点什么")
    finally:
        (loopmod.capture_screen, loopmod.get_screen_size,
         loopmod.build_som_mapping, loopmod.new_run_dir) = saved

    try:
        assert created == [], f"save_trace=false 仍然建了目录：{created}"
        assert not list(root.glob("*/step_*.png")), "save_trace=false 仍写了截图"
        print("✓ save_trace=false：不建目录、不写截图")
    finally:
        _cleanup()
