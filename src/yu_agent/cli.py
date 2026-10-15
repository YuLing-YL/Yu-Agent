"""命令行入口：python -m yu_agent 或 yu-agent。"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="yu-agent",
        description="Yu-Agent：基于 VLM 的 GUI 自动化智能体",
    )
    sub = parser.add_subparsers(dest="command")

    # run：命令行执行任务
    p_run = sub.add_parser("run", help="在命令行执行一个任务")
    p_run.add_argument("task", help="任务描述")
    p_run.add_argument("--dry-run", action="store_true", help="只规划不执行")

    # test：离线测试闭环（无需 API / 屏幕）
    p_test = sub.add_parser("test", help="用 mock 后端离线测试完整闭环（无需 API 和屏幕）")
    p_test.add_argument("task", nargs="?", default="打开浏览器搜索天气", help="测试任务描述")

    # screenshot：测试截屏
    p_shot = sub.add_parser("screenshot", help="截屏并保存到文件")
    p_shot.add_argument("--output", "-o", default="screenshot.png", help="输出路径")

    # gui：启动图形界面（默认）
    sub.add_parser("gui", help="启动图形界面")

    # doctor：环境自检
    sub.add_parser("doctor", help="检查环境依赖")

    args = parser.parse_args(argv)

    if args.command == "run":
        return _run(args)
    if args.command == "test":
        return _test(args)
    if args.command == "screenshot":
        return _screenshot(args)
    if args.command == "doctor":
        return _doctor()
    if args.command == "gui" or args.command is None:
        return _gui()

    parser.print_help()
    return 0


def _run(args) -> int:
    from .agent_facade import YuAgent

    agent = YuAgent(dry_run=args.dry_run)
    state = agent.run(args.task)
    print(f"\n结果：success={state.success}，步骤={state.step}，原因={state.reason}")
    agent.close()
    return 0 if state.success else 1


def _test(args) -> int:
    """离线测试：mock 后端 + 内存记忆，跑一遍完整闭环，验证核心逻辑。"""
    from .agent_facade import build_test_agent

    print(f"离线测试任务：{args.task}")
    print("使用 mock 后端 + 内存记忆（不调用真实模型，不操作屏幕）\n")

    agent = build_test_agent()
    state = agent.run(args.task)
    agent.close()

    print(f"\n测试结果：success={state.success}，步骤={state.step}，原因={state.reason}")
    if state.subtasks:
        print(f"子任务：{state.subtasks}")
    return 0 if state.success else 1


def _screenshot(args) -> int:
    from .perception.screenshot import capture_screen

    img = capture_screen()
    img.save(args.output)
    print(f"截图已保存到 {args.output}（{img.size[0]}x{img.size[1]}）")
    return 0


def _doctor() -> int:
    print("Yu-Agent 环境自检：")
    checks = [
        ("Pillow", _check_import("PIL")),
        ("pyautogui", _check_import("pyautogui")),
        ("requests", _check_import("requests")),
        ("PyYAML", _check_import("yaml")),
        ("pydantic", _check_import("pydantic")),
    ]
    ok = True
    for name, installed in checks:
        mark = "✓" if installed else "✗（未安装）"
        if not installed:
            ok = False
        print(f"  {mark} {name}")
    print("\n提示：pip install -e '.[macos]' 安装完整依赖（macOS）。")
    return 0 if ok else 1


def _check_import(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def _gui() -> int:
    from .gui.app import run_gui

    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
