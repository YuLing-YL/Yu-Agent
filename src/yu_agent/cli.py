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
    p_run.add_argument(
        "--som-mode", choices=["marks", "coords", "off"], default=None,
        help="本次标注模式：marks=编号标注（默认）/ coords=纯坐标 / off=不标注",
    )
    p_run.add_argument(
        "--all-apps", action="store_true",
        help="本次标注屏幕上所有可见应用（默认只标最前面的应用 + 程序坞）",
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
    p_per.add_argument("--max", type=int, default=60, help="最多标记多少个元素")
    p_per.add_argument("--dock-max", type=int, default=20, help="程序坞最多标记几个图标")
    p_per.add_argument(
        "--max-apps", type=int, default=1,
        help="最多枚举几个可见应用（默认 1 = 只标最前面的应用）",
    )
    p_per.add_argument(
        "--no-content", action="store_true",
        help="关闭「用截图像素校准元素框」（框会更贴合 AX 原始矩形，用于对比效果）",
    )
    p_per.add_argument(
        "--no-ocr", action="store_true",
        help="关闭 OCR 补标注（蓝色框）；默认开着，用于 AX 读不到的界面",
    )

    # web：网页三模态感知（DOM 快照 + AXTree + 截图 OCR），需要已开调试端口的 Chrome
    p_web = sub.add_parser(
        "web",
        help="网页三模态感知（DOM+AXTree+OCR 融合，需要 playwright 与调试端口）",
    )
    p_web.add_argument("--cdp", default="http://127.0.0.1:9222", help="Chrome 远程调试地址")
    p_web.add_argument("--shot", default="", help="同时把截图存到指定路径")
    p_web.add_argument("--no-ocr", action="store_true", help="不做 OCR 校验（只看 DOM+AX）")
    p_web.add_argument(
        "--origin", default="",
        help="手动指定页面内容区左上角在屏幕上的坐标，如 0,88（默认按窗口几何估算）",
    )
    p_web.add_argument("--json", action="store_true", help="输出 JSON（给脚本/回归用）")

    # memory：查看 / 清空长期记忆
    p_mem = sub.add_parser("memory", help="查看或清空长期记忆（跨任务沉淀的经验）")
    p_mem.add_argument("-n", type=int, default=10, help="每类显示最近多少条（默认 10）")
    p_mem.add_argument("--kind", default="", help="只看某一类：episodic/semantic/procedural")
    p_mem.add_argument("--clear", action="store_true", help="清空长期记忆（需再确认）")
    p_mem.add_argument(
        "--prune-legacy", action="store_true",
        help="只删除旧版蒸馏留下的占位记录（「轨迹: …」「…的方法已沉淀」），保留其余",
    )
    p_mem.add_argument("--yes", action="store_true", help="配合 --clear，跳过确认")

    # logs：查看日志文件位置 / 末尾内容
    p_log = sub.add_parser("logs", help="显示日志文件路径（或末尾若干行）")
    p_log.add_argument("-n", type=int, default=0, help="显示末尾 n 行（默认只显示路径）")

    # gui：启动图形界面（默认）
    sub.add_parser("gui", help="启动调试面板（标注图 / 编号表 / 模型原文 / 单步执行）")

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
    if args.command == "web":
        return _web(args)
    if args.command == "memory":
        return _memory(args)
    if args.command == "logs":
        return _logs(args)
    if args.command == "doctor":
        return _doctor()
    if args.command == "gui" or args.command is None:
        return _gui()

    parser.print_help()
    return 0


def _run(args) -> int:
    import sys

    from .agent.control import interactive
    from .agent_facade import YuAgent
    from .settings import get_settings

    # 交互式终端才接人工审批/问答；管道/CI 里自动降级为无人值守
    control = interactive() if sys.stdin.isatty() else None
    agent = YuAgent(dry_run=args.dry_run, control=control)
    # 本次运行的标注模式（命令行优先于配置）
    mode = getattr(args, "som_mode", None)
    if mode:
        agent.loop.som_mode = mode
    if getattr(args, "all_apps", False):
        # 本次放开「只标当前窗口」：枚举屏幕上所有可见应用
        agent.loop.som_frontmost_only = False
        agent.loop.som_max_apps = int(get_settings().loop.get("som_max_apps", 6))
        print("本次标注所有可见应用（不再只标当前窗口）")
    state = agent.run(args.task)
    print(f"\n结果：success={state.success}，步骤={state.step}，原因={state.reason}")
    if state.verified is False:
        print(f"复核：未通过 —— {state.verify_reason}")
    elif state.verified is True:
        print("复核：通过（截图上确认已完成）")
    else:
        print(f"复核：未验证{f'（{state.verify_reason}）' if state.verify_reason else ''}")
    if state.usage:
        u = state.usage
        cost = f"，约 ${u['cost_usd']}" if "cost_usd" in u else ""
        print(
            f"用量：{u.get('calls', 0)} 次调用 / {u.get('total_tokens', 0)} tokens"
            f" / 模型耗时 {u.get('llm_latency_s', 0)}s{cost}"
        )
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

    img = capture_screen()
    print(f"屏幕截图：{img.size[0]}x{img.size[1]}（逻辑像素）")
    print(f"标注范围：{'全部可见应用' if args.max_apps > 1 else '只标最前面的应用 + 程序坞'}\n")

    from .prompts.loader import prompt_language
    from .settings import get_settings

    loop_cfg = get_settings().loop or {}
    lang = prompt_language()  # 与 agent 看到的编号表保持一致
    marked, mapping, _ = build_som_mapping(
        img,
        max_elements=args.max,
        dock_max=args.dock_max,
        max_apps=args.max_apps,
        language=lang,
        content_check=not args.no_content,
        shrink=not args.no_content,
        use_ocr=not args.no_ocr,
        ocr_max=int(loop_cfg.get("som_ocr_max", 30)),
        ocr_min_conf=float(loop_cfg.get("som_ocr_min_conf", 0.30)),
    )
    stats = dict(a11y_mod.last_stats)

    apps = stats.get("apps") or []
    print(f"可见应用：{stats.get('app_count')} 个")
    for a in apps:
        raw = a.get("raw") or 0
        kept = a.get("elements") or 0
        chain = f"原始候选 {raw} 个 → 保留 {kept} 个" if raw else f"{kept} 个可点元素"
        print(f"  · {a.get('name')}（pid={a.get('pid')}）：{chain}（本应用上限 {a.get('cap')}）")
    print(f"  程序坞元素：{stats.get('dock_elements')} 个")
    if stats.get("dock_hidden"):
        print("  ⚠️ 程序坞是「自动隐藏」状态（图标在屏幕外），已排除；"
              "agent 会用 cmd+space 打开应用")
    print(f"  坐标系校准：{stats.get('calibration')}")
    audit = stats.get("audit") or {}
    if audit:
        print(
            f"  标注校准：丢弃空框 {audit.get('dropped_empty', 0)} 个 / "
            f"收缩到内容 {audit.get('shrunk', 0)} 个 / "
            f"窗口外丢弃 {audit.get('dropped_outside_window', 0)} 个"
            f"{'（本次已关闭）' if args.no_content else ''}"
        )
        # 「标注不全」时这两个数字最关键：是**上限砍掉的**，不是没识别到
        if audit.get("dropped_unnamed") or audit.get("truncated_by_limit"):
            print(
                f"  数量上限：无名字图标丢弃 {audit.get('dropped_unnamed', 0)} 个 / "
                f"超出元素预算截断 {audit.get('truncated_by_limit', 0)} 个"
            )
            print("    想把这些也标出来：调大 --max（agent.total 在 loop.som_max_marks）")
        if audit.get("merged") or audit.get("dropped_noise") or audit.get("dropped_offscreen"):
            print(
                f"  去重降噪：套娃元素合并 {audit.get('merged', 0)} 个 / "
                f"噪声丢弃 {audit.get('dropped_noise', 0)} 个（菜单栏、<12px、无名字且禁用）/ "
                f"屏幕外 {audit.get('dropped_offscreen', 0)} 个"
            )
    if "frame_reliable" in stats:
        ok = stats.get("frame_reliable")
        ratio = stats.get("frame_content_ratio")
        print(
            f"  帧可信度：{'✓ 正常' if ok else '✗ 异常'}"
            f"（{ratio} 的框落在有画面的位置）"
        )
        if not ok:
            print("    ⚠️ 截图与元素枚举对不上（切了桌面 / 刚唤醒 / 截屏被卡住）。"
                  "\n       agent 会自动改用「看画面给坐标」，这张标记图仅作参考。")
    print(f"  合计标记：{len(mapping)} 个（提示词语言：{lang}）\n")

    ocr_n = sum(1 for el in mapping.values() if el.kind == "ocr")
    if args.no_ocr:
        print("OCR 补标注：已关闭（--no-ocr）\n")
    else:
        from .perception import ocr as ocr_mod

        backend = ocr_mod.backend_name()
        if not backend:
            print("OCR 补标注：不可用（没有 Vision / pytesseract）\n")
        else:
            print(f"OCR 补标注：{ocr_n} 个蓝色框（后端 {backend}）")
            if ocr_n == 0:
                print("  说明：AX 已经把屏幕上的文字都框住了，或者这帧确实没有文字。")
            elif not apps:
                print("  ⚠️ AX 一个应用元素都没读到，全靠 OCR 在撑："
                      "\n     要么这个应用是 Electron/网页（正常），"
                      "\n     要么它没拿到辅助功能权限（系统设置 → 隐私与安全性 → 辅助功能）。")
            print("")

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
            from datetime import datetime

            from .common.paths import runs_dir

            # 单独放 runs/perceive/：perceive 只是自检，不该在运行记录列表里
            # 占一个「好像跑过任务」的目录
            d = runs_dir() / "perceive"
            d.mkdir(parents=True, exist_ok=True)
            out = str(d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
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


def _web(args) -> int:
    """网页三模态感知：DOM 布局 + AXTree + 截图 OCR 融合，打印可点目标与冲突。

    这是"感知准不准"的现场诊断工具：不用跑模型、不用点鼠标，
    直接看三路信号对齐后剩下什么、哪些元素被判为遮挡/空白/名字不一致。
    """
    import json as _json

    from .common.logging import get_logger, setup_logging
    from .env.browser_cdp import get_session, playwright_available
    from .perception.web.collect import dump_table

    setup_logging()
    log = get_logger("yu_agent.cli")

    if not playwright_available():
        print("需要 playwright 才能连浏览器：")
        print("  pip install -e '.[browser]' && playwright install chromium   # 只需一次")
        return 2

    try:
        sess = get_session(args.cdp)
        geo = sess.geometry(refresh=True)
    except Exception as e:
        print(f"连接 {args.cdp} 失败：{e}")
        print("提示：Chrome 需要用 --remote-debugging-port=9222 启动后保持窗口打开")
        return 1

    origin = geo.origin
    if args.origin:
        try:
            ox, oy = [float(v) for v in args.origin.split(",")[:2]]
            origin = (ox, oy)
        except ValueError:
            print(f"--origin 格式应为 x,y，收到：{args.origin}")
            return 2

    try:
        perc = sess.perceive(ocr=not args.no_ocr, origin=origin)
        shot = sess.screenshot()
    except Exception as e:
        print(f"感知失败：{e}")
        log.warning("web 感知失败：%s", e)
        return 1

    print(f"CDP：{args.cdp}")
    print(f"窗口：{geo.window}　内容区原点：{origin}　zoom={geo.zoom}　视口={geo.inner}")
    if args.shot:
        try:
            shot.save(args.shot)
            print(f"截图：{args.shot}")
        except Exception as e:
            print(f"截图保存失败：{e}")
    print("")
    for line in dump_table(perc):
        print(line)

    if args.json:
        payload = {
            "viewport": perc.viewport,
            "origin": origin,
            "zoom": geo.zoom,
            "meta": perc.meta,
            "targets": [
                {"id": e.id, "role": e.role, "name": e.title,
                 "x": e.x, "y": e.y, "w": e.w, "h": e.h, "enabled": e.enabled}
                for e in perc.elements
            ],
            "conflicts": perc.result.conflicts,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _format_marks(mapping) -> list:
    """把编号映射整理成按应用分组的文本（与给模型的编号表同构）。"""
    if not mapping:
        return ["（没有识别到任何可标记元素）"]

    groups: list = []
    index: dict = {}
    for num in sorted(mapping.keys()):
        el = mapping[num]
        if el.kind == "dock":
            key = "程序坞"
        elif el.kind == "ocr":
            key = "屏幕文字(OCR)"
        else:
            key = el.app or "应用"
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


# 旧版蒸馏写入的占位记录特征（清理用）
_LEGACY_PATTERNS = ("轨迹: ", "轨迹：", "的方法已沉淀")


def _memory(args) -> int:
    """查看 / 清空长期记忆。

    长期记忆是任务结束后自动提炼的（summary / 经验 / 可复用步骤），
    这里让人能直接看到"它到底记住了什么"，也方便在记错时清掉重来。
    """
    from .memory import MemoryManager

    m = MemoryManager()
    try:
        if args.clear:
            if not args.yes:
                ans = input("确认清空全部长期记忆？输入 yes 继续：").strip().lower()
                if ans not in ("yes", "y"):
                    print("已取消，没有删除任何东西。")
                    return 0
            m.store.clear()
            print("长期记忆已清空。")
            return 0

        if args.prune_legacy:
            n = m.store.delete_like(_LEGACY_PATTERNS)
            print(f"已删除 {n} 条旧格式占位记忆（其余记忆保留）。")
            return 0

        view = m.memory_view(limit=max(1, args.n))
        kinds = ["episodic", "semantic", "procedural", "profile"]
        if args.kind:
            kinds = [args.kind] if args.kind in view else []
            if not kinds:
                print(f"未知类型：{args.kind}（可选：episodic / semantic / procedural / profile）")
                return 1

        labels = {
            "episodic": "情节记忆（每次任务做了什么）",
            "semantic": "语义记忆（跨任务的经验/注意事项）",
            "procedural": "程序记忆（可复用的操作步骤）",
            "profile": "画像（用户偏好）",
        }
        total = 0
        for kind in kinds:
            rows = view.get(kind) or []
            total += len(rows)
            print(f"\n── {labels.get(kind, kind)}：{len(rows)} 条")
            if not rows:
                continue
            for r in rows:
                import datetime

                ts = datetime.datetime.fromtimestamp(r["created_at"]).strftime("%m-%d %H:%M")
                flag = "" if r.get("success", 1) else "  ⚠️失败"
                body = " ".join(str(r["content"]).split())
                print(f"  [{ts}] #{r['id']}{flag} {body[:150]}")
        if total == 0:
            print("\n（还没有长期记忆。跑完一个任务后会自动生成。）")
        else:
            print(f"\n共 {total} 条。清空用：python -m yu_agent memory --clear")
        legacy = m.store.count_like(_LEGACY_PATTERNS)
        if legacy:
            print(
                f"⚠️ 其中 {legacy} 条是旧版蒸馏留下的占位记录（「轨迹: …」这类，"
                f"没有信息量，会混进召回）。\n"
                f"   只删它们：python -m yu_agent memory --prune-legacy"
            )
        return 0
    finally:
        m.close()


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

    # OCR 是「标注不全」的兜底通道（AX 读不到的界面全靠它），装没装要能一眼看到
    from .perception import ocr as ocr_mod

    backend = ocr_mod.backend_name()
    print(f"  {'✓' if backend else '✗（不可用）'} OCR 后端：{backend or '无'}")
    if backend == "vision":
        print("    macOS Vision 可用：AX 读不到的界面会用蓝色编号框补上")

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
