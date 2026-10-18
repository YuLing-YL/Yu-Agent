"""命令行入口：python -m yu_agent 或 yu-agent。"""
from __future__ import annotations

import argparse
import os
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
    p_run.add_argument(
        "--no-reset", action="store_true",
        help="本次不清场（不隐藏其它应用，也照常枚举所有可见应用）",
    )

    # test：离线测试闭环（无需 API / 屏幕）
    p_test = sub.add_parser("test", help="用 mock 后端离线测试完整闭环（无需 API 和屏幕）")
    p_test.add_argument("task", nargs="?", default="打开浏览器搜索天气", help="测试任务描述")

    # screenshot：测试截屏
    p_shot = sub.add_parser("screenshot", help="截屏并保存到文件")
    p_shot.add_argument("--output", "-o", default="screenshot.png", help="输出路径")

    # perceive：感知自检（截屏 + SOM 标记），不需要 API
    p_per = sub.add_parser(
        "perceive",
        help="感知自检：截屏 + 列出识别到的可点击元素（不需要 API）",
    )
    p_per.add_argument("--output", "-o", default="", help="标记图输出路径")
    p_per.add_argument("--max", type=int, default=120, help="最多标记多少个元素")
    p_per.add_argument("--dock-max", type=int, default=30, help="程序坞最多标记几个图标")
    p_per.add_argument("--max-apps", type=int, default=6, help="最多枚举几个可见应用")
    p_per.add_argument(
        "--clean", action="store_true",
        help="先清场（尽力隐藏其它应用）并只标当前应用，预览 agent 实际看到的效果",
    )

    # logs：查看日志文件位置 / 末尾内容
    p_log = sub.add_parser("logs", help="显示日志文件路径（或末尾若干行）")
    p_log.add_argument("-n", type=int, default=0, help="显示末尾 n 行（默认只显示路径）")

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
    if args.command == "perceive":
        return _perceive(args)
    if args.command == "logs":
        return _logs(args)
    if args.command == "doctor":
        return _doctor()
    if args.command == "gui" or args.command is None:
        return _gui()

    parser.print_help()
    return 0


def _run(args) -> int:
    from .agent_facade import YuAgent
    from .settings import get_settings

    agent = YuAgent(dry_run=args.dry_run)
    if getattr(args, "no_reset", False):
        # 本次关闭清场：恢复「枚举所有可见应用」的行为
        agent.loop.reset_desktop = False
        agent.loop.som_max_apps = int(get_settings().loop.get("som_max_apps", 6))
        print("已关闭清场：本次不隐藏其它应用，照常枚举所有可见应用")
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


def _perceive(args) -> int:
    """感知自检：截屏 + SOM 标记，把「模型能看到什么」直接打印出来。

    排查「找不到输入框 / 点不准 / 漏元素」时先跑这个，一眼就能看出
    是元素没被标记出来（插件问题），还是标记对了但模型没点对（模型问题）。
    打印的编号与标记图上的编号**完全一致**（都用 draw_marks 的映射）。
    """
    from .common.logging import get_logger, log_file_path, setup_logging

    setup_logging()
    log = get_logger("yu_agent.cli")

    from .perception import a11y as a11y_mod
    from .perception.screenshot import capture_screen
    from .perception.som import build_som_mapping

    if getattr(args, "clean", False):
        # 和 agent 的「清场模式」完全一致：先尽力隐藏其它应用，再只标当前应用 + 程序坞
        from .actions.desktop import clean_desktop

        report = clean_desktop(task="", keep=[], exclude_pids=[os.getpid()])
        print(f"清场：{report.summary()}")
        args.max_apps = 1

    img = capture_screen()
    print(f"屏幕截图：{img.size[0]}x{img.size[1]}（逻辑像素）")

    marked, mapping, _ = build_som_mapping(
        img,
        max_elements=args.max,
        dock_max=args.dock_max,
        max_apps=args.max_apps,
    )
    stats = dict(a11y_mod.last_stats)

    apps = stats.get("apps") or []
    print(f"可见应用：{stats.get('app_count')} 个")
    for a in apps:
        print(f"  · {a.get('name')}（pid={a.get('pid')}）：{a.get('elements')} 个可点元素")
    print(f"  程序坞元素：{stats.get('dock_elements')} 个")
    print(f"  坐标系校准：{stats.get('calibration')}")
    print(f"  合计标记：{len(mapping)} 个\n")

    if not apps:
        print("⚠️ 没有识别到任何应用内元素！")
        print("   常见原因：目标应用没有授予「辅助功能」权限")
        print("   （系统设置 → 隐私与安全性 → 辅助功能，把终端 / Yu-Agent 勾上）\n")

    # 按分组打印，编号与图上的完全一致
    for line in _format_marks(mapping):
        print(line)

    out = args.output
    if not out:
        try:
            from .common.paths import new_run_dir

            out = str(new_run_dir() / "perceive.png")
        except Exception:
            out = "perceive.png"

    try:
        marked.save(out)
        print(f"\n标记图已保存：{out}")
        print("（打开这张图，编号应当与上面打印的一一对应）")
    except Exception as e:
        print(f"标记图保存失败：{e}")

    p = log_file_path()
    if p:
        print(f"日志文件：{p}")
    log.info("感知自检完成：%s", stats)
    return 0


def _format_marks(mapping) -> list:
    """把编号映射整理成按应用分组的文本（与给模型的编号表同构）。"""
    if not mapping:
        return ["（没有识别到任何可标记元素）"]

    groups: list = []
    index: dict = {}
    for num in sorted(mapping.keys()):
        el = mapping[num]
        key = "程序坞" if el.kind == "dock" else (el.app or "应用")
        if key not in index:
            index[key] = len(groups)
            groups.append((key, []))
        groups[index[key]][1].append(num)

    lines = []
    for name, nums in groups:
        span = f"#{nums[0]}~#{nums[-1]}" if len(nums) > 1 else f"#{nums[0]}"
        lines.append(f"── {name}：{span}（{len(nums)} 个）")
    lines.append("")

    for num in sorted(mapping.keys()):
        el = mapping[num]
        lines.append(
            f"#{num:<3} {el.label() or '（无标题元素）'}"
            f"   中心≈({int(el.cx)},{int(el.cy)}) 尺寸={int(el.w)}x{int(el.h)}"
        )
    return lines


def _logs(args) -> int:
    """显示日志文件路径；给了 -n 就打印末尾若干行。"""
    from .common.logging import log_file_path, setup_logging

    setup_logging()
    p = log_file_path()
    if not p:
        print("没有启用文件日志。")
        return 1
    print(f"日志文件：{p}")
    if args.n and args.n > 0:
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            print(f"\n--- 末尾 {args.n} 行 ---")
            for line in lines[-args.n:]:
                print(line)
        except Exception as e:
            print(f"读取失败：{e}")
            return 1
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
