#!/usr/bin/env python3
"""Yu-Agent 评测集运行器。

它做三件事：
1. 按 eval/tasks.yaml 逐个跑任务，每个任务跑 N 次；
2. 每次跑完用**外部状态**判定成功与否（前台应用 / 窗口标题 / 文件系统 / 界面元素），
   不采信模型自己输出的 done；
3. 输出终端汇总表 + Markdown 报告 + JSON 原始数据，落盘到 eval/reports/。

常用命令
--------
    python eval/runner.py --list                  # 看有哪些任务
    python eval/runner.py -n 1                    # 全部任务各跑 1 次（快速冒烟）
    python eval/runner.py                         # 默认每个跑 3 次
    python eval/runner.py -t safari_search_zh     # 只跑一个任务
    python eval/runner.py -t 搜索                  # 按类别筛选（id 或 category 都行）
    python eval/runner.py --tag marks             # 结果打标签，方便 A/B 对比
    python eval/runner.py --self-test             # mock 后端验证评测流程（不出真实成绩）

注意：真机模式会**真的操作鼠标和键盘**，跑之前请确认桌面状态可以被打扰。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
# 允许 `python -m eval.runner` 这类启动方式也能找到同目录的 checks
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import checks  # noqa: E402  （同目录模块）

EVAL_DIR = Path(__file__).resolve().parent
TASKS_FILE = EVAL_DIR / "tasks.yaml"
REPORT_DIR = EVAL_DIR / "reports"

# 启动类任务若上一轮把应用留在前台，下一轮会变成「起点已满足终点」。
# 这个开关用 osascript 主动退出这些应用（不动终端，因为评测器自己就在终端里跑）。
_RESETTABLE_APPS = ("Safari", "TextEdit", "System Settings", "System Preferences")


# --------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------- #
@dataclass
class RunResult:
    task_id: str
    category: str
    task: str
    run_idx: int
    verdict: str = "error"          # pass / fail / manual / invalid / error
    check_detail: str = ""
    precheck_detail: str = ""
    steps: int = 0
    elapsed_s: float = 0.0
    auto_stopped: bool = False
    reason: str = ""
    run_dir: str = ""
    screenshot: str = ""
    actions: List[str] = field(default_factory=list)
    model_raws: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def counted(self) -> bool:
        """是否计入成功率分母（invalid 起点 / error 异常 都不计）。"""
        return self.verdict in ("pass", "fail")


# --------------------------------------------------------------------- #
# 载入任务
# --------------------------------------------------------------------- #
def load_spec() -> Dict[str, Any]:
    import yaml

    if not TASKS_FILE.exists():
        raise SystemExit(f"找不到任务集：{TASKS_FILE}")
    with TASKS_FILE.open(encoding="utf-8") as f:
        spec = yaml.safe_load(f) or {}
    if not spec.get("tasks"):
        raise SystemExit(f"{TASKS_FILE} 里没有 tasks")
    return spec


def select_tasks(spec: Dict[str, Any], pattern: str) -> List[Dict[str, Any]]:
    """按 id / category 过滤（逗号分隔，子串匹配）。"""
    tasks = list(spec.get("tasks") or [])
    keys = [p.strip().lower() for p in (pattern or "").split(",") if p.strip()]
    if not keys:
        return tasks
    picked = []
    for t in tasks:
        hay = f"{t.get('id', '')} {t.get('category', '')}".lower()
        if any(k in hay for k in keys):
            picked.append(t)
    return picked


def print_task_list(tasks: List[Dict[str, Any]]) -> None:
    print(f"共 {len(tasks)} 个任务：\n")
    head = f"{'id':<22}{'类别':<12}{'判据'}"
    print(head)
    print("-" * 88)
    for t in tasks:
        print(
            f"{t.get('id', ''):<22}{t.get('category', ''):<12}"
            f"{checks.describe(t.get('check'))}"
        )
        print(f"{'':<22}{t.get('task', '')}")
    print()


def prepare_sandbox(spec: Dict[str, Any]) -> None:
    """准备评测沙盒（幂等，只在 /tmp 下建目录和一个小文件）。"""
    sb = spec.get("sandbox")
    if not sb:
        return
    p = Path(os.path.expanduser(str(sb)))
    p.mkdir(parents=True, exist_ok=True)
    note = p / "note.txt"
    if not note.exists():
        note.write_text("yu-agent eval sandbox\n", encoding="utf-8")
    print(f"沙盒就绪：{p}")


def reset_apps() -> None:
    """退出常见的目标应用，保证「打开 X」类任务的起点干净。"""
    for name in _RESETTABLE_APPS:
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{name}" to quit saving no'],
                capture_output=True,
                timeout=8,
            )
        except Exception:
            pass
    time.sleep(1.0)


# --------------------------------------------------------------------- #
# 预检
# --------------------------------------------------------------------- #
def _silence_console_logs(verbose: bool) -> None:
    """把 agent 的 INFO 日志从控制台移走，只留文件日志。

    YuAgent 初始化时会 setup_logging()，把每一步的「观察 / 动作」打到 stdout，
    直接把评测的进度行和汇总表冲散。评测期间只保留 data/logs/yu_agent.log，
    需要实时看日志时加 --verbose。
    """
    if verbose:
        return
    import logging

    try:
        from yu_agent.common.logging import setup_logging

        setup_logging()
        root = logging.getLogger()
        for h in list(root.handlers):
            # 精确匹配：RotatingFileHandler 是 StreamHandler 的子类，不能一起摘掉
            if type(h) is logging.StreamHandler:
                root.removeHandler(h)
        root.setLevel(logging.WARNING)
    except Exception:
        pass


def preflight(self_test: bool) -> List[str]:
    problems: List[str] = []
    if self_test:
        return problems

    try:
        from yu_agent.settings import get_settings

        mc = get_settings().get_model("default")
        if not mc.is_configured:
            problems.append(
                "模型未配置 API Key（在设置页填写后再跑；想只验证评测流程用 --self-test）"
            )
    except Exception as e:
        problems.append(f"读取配置失败：{e}")

    try:
        import ApplicationServices as AX

        if not AX.AXIsProcessTrusted():
            problems.append(
                "没有辅助功能权限：系统设置 → 隐私与安全性 → 辅助功能，"
                "勾选运行评测的终端/Python"
            )
    except Exception as e:
        problems.append(f"辅助功能权限检测失败：{e}")

    return problems


# --------------------------------------------------------------------- #
# 运行一次任务
# --------------------------------------------------------------------- #
def _summarize_action(ev: Dict[str, Any]) -> str:
    """把 step_detail 事件压成一行动作摘要。"""
    t = str(ev.get("action") or "?")
    mark = ev.get("mark")
    x, y = ev.get("x"), ev.get("y")
    if mark is not None:
        return f"{t}#{mark}"
    if x is not None and y is not None:
        return f"{t}({x},{y})"
    if ev.get("text"):
        return f"{t}「{str(ev['text'])[:20]}」"
    if ev.get("key"):
        return f"{t}:{ev['key']}"
    return t


def run_once(
    task: str,
    timeout_s: float,
    self_test: bool,
    som_mode: Optional[str],
    events_sink: List[Dict[str, Any]],
) -> Tuple[Any, Optional[BaseException], bool]:
    """在子线程里建 agent 并跑一次任务。

    为什么必须在线程里建：MemoryStore 的 sqlite 连接不能跨线程使用，
    agent 必须在「将要运行它的那个线程」里创建（GUI 的 worker 也是这么做的）。
    """
    from yu_agent.agent_facade import YuAgent

    box: Dict[str, Any] = {"agent": None, "state": None, "error": None}

    def collect(ev: Dict[str, Any]) -> None:
        if ev.get("type") == "step_detail":
            events_sink.append(ev)

    def target() -> None:
        agent = None
        try:
            if self_test:
                from yu_agent.memory import MemoryManager
                from yu_agent.memory.store import MemoryStore
                from yu_agent.models.mock import MockBackend

                agent = YuAgent(
                    backend=MockBackend(),
                    memory=MemoryManager(store=MemoryStore(db_path=":memory:")),
                    dry_run=True,
                    callback=collect,
                )
            else:
                agent = YuAgent(callback=collect)
            box["agent"] = agent
            if som_mode:
                agent.loop.som_mode = som_mode
            box["state"] = agent.run(task)
        except BaseException as e:  # noqa: BLE001 - 评测要吞掉一切异常继续跑
            box["error"] = e
        finally:
            if agent is not None:
                try:
                    agent.close()
                except Exception:
                    pass

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout_s)

    timed_out = False
    if th.is_alive():
        timed_out = True
        agent = box.get("agent")
        if agent is not None:
            try:
                agent.stop()  # 让主循环在下一个检查点退出
            except Exception:
                pass
        th.join(30)

    return box.get("state"), box.get("error"), timed_out


def evaluate_once(
    tspec: Dict[str, Any],
    run_idx: int,
    args,
    shots_dir: Path,
) -> RunResult:
    tid = str(tspec.get("id") or "")
    res = RunResult(
        task_id=tid,
        category=str(tspec.get("category") or ""),
        task=str(tspec.get("task") or ""),
        run_idx=run_idx,
    )

    # 1. 前置条件：起点已经满足终点的话，这次跑出来的 pass 是假的，直接记 invalid
    #    （self-test 不读真实桌面，跳过，否则会被当前前台应用随手判成 invalid）
    pre = tspec.get("pre_check")
    if pre and not args.self_test:
        pr = checks.run_check(pre)
        res.precheck_detail = pr.detail
        if pr.ok is False:
            res.verdict = "invalid"
            res.check_detail = f"起点不满足：{pr.detail}"
            return res

    # 2. 跑
    events: List[Dict[str, Any]] = []
    started = time.time()
    state, err, timed_out = run_once(
        res.task, args.timeout, args.self_test, args.som_mode, events
    )
    res.elapsed_s = round(time.time() - started, 1)

    res.actions = [_summarize_action(e) for e in events]
    res.model_raws = [str(e.get("model_raw") or "") for e in events]
    if events:
        res.run_dir = str(events[-1].get("run_dir") or "")
        res.screenshot = str(events[-1].get("image") or "")

    if err is not None:
        res.verdict = "error"
        res.error = f"{type(err).__name__}: {err}"
        res.check_detail = "运行时异常"
        return res

    if state is not None:
        res.steps = int(getattr(state, "step", 0) or 0)
        res.auto_stopped = bool(getattr(state, "auto_stopped", False))
        res.reason = str(getattr(state, "reason", "") or "")
    if timed_out:
        res.error = f"超过 {args.timeout:.0f}s 被强制停止"
        res.reason = res.reason or res.error

    # 3. 判定
    if args.self_test:
        # 自检模式只用来看流程能不能跑通，成绩无意义
        res.verdict = "pass" if (state is not None and getattr(state, "success", False)) else "fail"
        res.check_detail = "self-test（mock 后端，不判定真实结果）"
    else:
        cr = checks.run_check(tspec.get("check"))
        res.verdict = cr.verdict
        res.check_detail = cr.detail

    # 4. 存档一张跑完的截图：人工复核判据时最有用
    if not args.no_screenshot and not args.self_test:
        try:
            from yu_agent.perception.screenshot import capture_screen

            img = capture_screen()
            out = shots_dir / f"{tid}_{run_idx}.png"
            img.save(out)
            res.screenshot = str(out)
        except Exception as e:
            res.check_detail += f"（截图失败：{e}）"

    return res


# --------------------------------------------------------------------- #
# 汇总与报告
# --------------------------------------------------------------------- #
def _pad(text: str, width: int) -> str:
    """按显示宽度补齐（中文算 2 列）。"""
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in text)
    return text + " " * max(0, width - w)


def summarize(results: List[RunResult], tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    per_task: List[Dict[str, Any]] = []
    for t in tasks:
        tid = str(t.get("id") or "")
        rows = [r for r in results if r.task_id == tid]
        cnt = {k: sum(1 for r in rows if r.verdict == k) for k in
               ("pass", "fail", "manual", "invalid", "error")}
        counted = cnt["pass"] + cnt["fail"]
        done = [r for r in rows if r.counted]
        per_task.append(
            {
                "id": tid,
                "category": t.get("category", ""),
                "task": t.get("task", ""),
                "runs": len(rows),
                **cnt,
                "pass_rate": (cnt["pass"] / counted) if counted else None,
                "avg_steps": (sum(r.steps for r in done) / len(done)) if done else None,
                "avg_elapsed": (sum(r.elapsed_s for r in done) / len(done)) if done else None,
                "auto_stopped": sum(1 for r in rows if r.auto_stopped),
            }
        )

    total = {k: sum(1 for r in results if r.verdict == k) for k in
             ("pass", "fail", "manual", "invalid", "error")}
    counted = total["pass"] + total["fail"]
    return {
        "per_task": per_task,
        "total": total,
        "counted": counted,
        "pass_rate": (total["pass"] / counted) if counted else None,
        "avg_steps": (
            sum(r.steps for r in results if r.counted) / counted if counted else None
        ),
        "avg_elapsed": (
            sum(r.elapsed_s for r in results if r.counted) / counted if counted else None
        ),
        "auto_stopped": sum(1 for r in results if r.auto_stopped),
    }


def print_summary(summary: Dict[str, Any]) -> None:
    print("\n" + "=" * 92)
    print(
        _pad("任务", 22) + _pad("类别", 12) + _pad("通过/有效", 11)
        + _pad("待人工", 8) + _pad("无效", 7) + _pad("异常", 7)
        + _pad("平均步数", 10) + "平均耗时"
    )
    print("-" * 92)
    for row in summary["per_task"]:
        counted = row["pass"] + row["fail"]
        rate = f"{row['pass']}/{counted}" if counted else "-"
        steps = f"{row['avg_steps']:.1f}" if row["avg_steps"] is not None else "-"
        el = f"{row['avg_elapsed']:.1f}s" if row["avg_elapsed"] is not None else "-"
        print(
            _pad(row["id"], 22) + _pad(row["category"], 12) + _pad(rate, 11)
            + _pad(str(row["manual"]), 8) + _pad(str(row["invalid"]), 7)
            + _pad(str(row["error"]), 7) + _pad(steps, 10) + el
        )
    print("-" * 92)

    t = summary["total"]
    rate = summary["pass_rate"]
    rate_txt = f"{rate * 100:.1f}%" if rate is not None else "无可计分样本"
    print(
        f"总览：成功率 {rate_txt}（{t['pass']}/{summary['counted']}）"
        f" | 待人工 {t['manual']} | 无效起点 {t['invalid']} | 异常 {t['error']}"
        f" | 自动停止 {summary['auto_stopped']} 次"
    )
    if summary["avg_steps"] is not None:
        print(
            f"      平均 {summary['avg_steps']:.1f} 步 / "
            f"{summary['avg_elapsed']:.1f}s 每次（仅统计可计分的运行）"
        )
    print("=" * 92)


def write_report(
    results: List[RunResult],
    summary: Dict[str, Any],
    meta: Dict[str, Any],
    args,
    out_dir: Path,
) -> Tuple[Path, Path]:
    t = summary["total"]
    rate = summary["pass_rate"]
    rate_txt = f"{rate * 100:.1f}%" if rate is not None else "—"
    lines: List[str] = []
    lines.append(f"# 评测报告 · {meta.get('name', 'eval')}")
    lines.append("")
    lines.append(f"- 时间：{meta.get('timestamp')}")
    lines.append(f"- 标签：`{meta.get('tag') or '-'}`")
    lines.append(f"- 模型：`{meta.get('model')}`")
    lines.append(
        f"- 配置：som_mode=`{meta.get('som_mode')}` · 只标当前窗口="
        f"`{meta.get('frontmost_only')}` · 提示词语言=`{meta.get('prompt_lang')}`"
    )
    lines.append(f"- 每个任务运行次数：{meta.get('runs')}")
    if args.self_test:
        lines.append("")
        lines.append("> ⚠️ **self-test 模式**：mock 后端 + dry-run，只验证评测流程，成绩无意义。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 成功率（pass / 有效） | **{rate_txt}**（{t['pass']}/{summary['counted']}） |")
    lines.append(f"| 待人工确认 | {t['manual']} |")
    lines.append(f"| 无效起点（不计分） | {t['invalid']} |")
    lines.append(f"| 运行异常 | {t['error']} |")
    lines.append(f"| 自动停止次数 | {summary['auto_stopped']} |")
    if summary["avg_steps"] is not None:
        lines.append(
            f"| 平均步数 / 耗时 | {summary['avg_steps']:.1f} 步 / "
            f"{summary['avg_elapsed']:.1f}s |"
        )
    lines.append("")
    lines.append("> 成功率 = pass / (pass + fail)。`待人工` 是软判据给不出结论的运行，"
                 "`无效起点` 是开始前目标状态就已满足（例如 Safari 已在前台），"
                 "两者都不进分母，免得虚高或冤枉。")
    lines.append("")
    lines.append("## 逐任务")
    lines.append("")
    lines.append("| 任务 | 类别 | 通过/有效 | 待人工 | 无效 | 异常 | 平均步数 | 平均耗时 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in summary["per_task"]:
        counted = row["pass"] + row["fail"]
        steps = f"{row['avg_steps']:.1f}" if row["avg_steps"] is not None else "—"
        el = f"{row['avg_elapsed']:.1f}s" if row["avg_elapsed"] is not None else "—"
        lines.append(
            f"| `{row['id']}` | {row['category']} | {row['pass']}/{counted} | "
            f"{row['manual']} | {row['invalid']} | {row['error']} | {steps} | {el} |"
        )
    lines.append("")

    # 明细：每次运行一行
    lines.append("## 每次运行明细")
    lines.append("")
    lines.append("| 任务 | 第几次 | 结果 | 步数 | 耗时 | 判据 | 动作序列 | 截图 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    icon = {
        "pass": "✅ 通过", "fail": "❌ 未通过", "manual": "👤 待人工",
        "invalid": "⊘ 无效起点", "error": "⚠️ 异常",
    }
    for r in results:
        acts = " ".join(r.actions[:8]) + (" …" if len(r.actions) > 8 else "")
        acts = acts.replace("|", "/") or "—"
        detail = (r.check_detail or r.error or "").replace("|", "/")[:70]
        shot = "—"
        if r.screenshot:
            p = Path(r.screenshot)
            # runs/<时间戳>/step_XX.png 带上目录名，方便一眼看出是哪次运行的图
            shot = p.name if p.parent.name == "shots" else f"{p.parent.name}/{p.name}"
        lines.append(
            f"| `{r.task_id}` | {r.run_idx} | {icon.get(r.verdict, r.verdict)} | "
            f"{r.steps} | {r.elapsed_s:.1f}s | {detail} | {acts} | {shot} |"
        )
    lines.append("")
    lines.append("> 截图列的 `runs/<时间戳>/step_XX.png` 是**模型实际收到的标注图**，"
                 "同目录下的 `trace.jsonl` 有每步的完整决策记录；"
                 "评测器自己存的复核截图在本次报告目录的 `shots/` 下。")
    lines.append("")

    # 失败与自动停止的原因聚合，直接指向该改什么
    fails = [r for r in results if r.verdict in ("fail", "manual", "error")]
    if fails:
        lines.append("## 需要关注的原因")
        lines.append("")
        for r in fails:
            reason = (r.reason or r.check_detail or r.error or "").strip()
            lines.append(
                f"- `{r.task_id}#{r.run_idx}` **{r.verdict}**：{reason[:160] or '（无原因文本）'}"
            )
        lines.append("")
    auto = [r for r in results if r.auto_stopped]
    if auto:
        lines.append("## 被系统自动停止的运行")
        lines.append("")
        for r in auto:
            lines.append(
                f"- `{r.task_id}#{r.run_idx}`：{r.reason[:160] or '（无原因文本）'}"
                f"　动作：{' '.join(r.actions[:10])}"
            )
        lines.append("")

    md = out_dir / "report.md"
    md.write_text("\n".join(lines), encoding="utf-8")

    js = out_dir / "results.json"
    js.write_text(
        json.dumps(
            {"meta": meta, "summary": summary,
             "results": [asdict(r) for r in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return md, js


# --------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Yu-Agent 评测集运行器（会真的操作鼠标键盘）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-t", "--tasks", default="", help="按 id / category 筛选，逗号分隔")
    p.add_argument("-n", "--runs", type=int, default=0, help="每个任务跑几次（默认取 tasks.yaml 的 meta.runs）")
    p.add_argument("--tag", default="", help="结果标签，便于 A/B 对比（会进文件名）")
    p.add_argument("--timeout", type=float, default=180.0, help="单次运行的硬超时秒数")
    p.add_argument("--cooldown", type=float, default=-1.0, help="两次运行之间的等待秒数")
    p.add_argument("--som-mode", default="", choices=["", "marks", "coords", "off"],
                   help="临时覆盖标注模式（便于 A/B）")
    p.add_argument("--reset-apps", action="store_true",
                   help="每次运行前退出 Safari / 文本编辑 / 系统设置，让「打开 X」类任务起点干净")
    p.add_argument("--no-screenshot", action="store_true", help="跑完不存截图")
    p.add_argument("--no-setup", action="store_true", help="不自动创建评测沙盒目录")
    p.add_argument("--self-test", action="store_true", help="mock 后端验证评测流程，不操作真实界面")
    p.add_argument("--verbose", action="store_true", help="保留 agent 的实时日志（会冲乱表格，排障时用）")
    p.add_argument("--list", action="store_true", help="只列出任务，不运行")
    p.add_argument("--yes", action="store_true", help="跳过开跑前的确认")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    spec = load_spec()
    tasks = select_tasks(spec, args.tasks)
    if not tasks:
        print(f"没有匹配的任务（--tasks {args.tasks}）")
        return 2

    if args.list:
        print_task_list(tasks)
        return 0

    _silence_console_logs(args.verbose)

    problems = preflight(args.self_test)
    if problems:
        print("预检未通过：")
        for x in problems:
            print(f"  - {x}")
        return 2

    if not args.no_setup:
        prepare_sandbox(spec)

    runs = args.runs or int(spec.get("meta", {}).get("runs", 3) or 3)
    cooldown = (
        args.cooldown if args.cooldown >= 0
        else float(spec.get("meta", {}).get("cooldown_s", 3) or 0)
    )

    # 元信息（写进报告，方便对比两次评测）
    from yu_agent.prompts.loader import prompt_language
    from yu_agent.settings import get_settings

    st = get_settings()
    meta: Dict[str, Any] = {
        "name": spec.get("meta", {}).get("name", "eval"),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tag": args.tag,
        "model": "mock（self-test）" if args.self_test else st.get_model("default").model,
        "som_mode": args.som_mode or st.agent.som_mode,
        "frontmost_only": st.agent.som_frontmost_only,
        "prompt_lang": prompt_language(),
        "runs": runs,
    }

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{args.tag}" if args.tag else stamp
    out_dir = REPORT_DIR / name
    shots_dir = out_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    total_runs = len(tasks) * runs
    print(f"评测集：{meta['name']}　任务 {len(tasks)} 个 × {runs} 次 = {total_runs} 次运行")
    print(f"模型：{meta['model']}　标注：{meta['som_mode']}　语言：{meta['prompt_lang']}")
    print(f"报告目录：{out_dir}")
    if not args.self_test:
        print("\n⚠️  接下来会真的操作鼠标键盘，请不要动鼠标；需要中止按 Ctrl+C。")
        if not args.yes:
            for i in range(3, 0, -1):
                print(f"   {i} 秒后开始…", end="\r", flush=True)
                time.sleep(1)
            print(" " * 24, end="\r")

    results: List[RunResult] = []
    stopped = False
    try:
        for tspec in tasks:
            tid = str(tspec.get("id") or "")
            for idx in range(1, runs + 1):
                if args.reset_apps and not args.self_test:
                    reset_apps()

                tag = f"{tid} #{idx}/{runs}"
                print(f"→ {_pad(tag, 34)}", end="", flush=True)
                try:
                    r = evaluate_once(tspec, idx, args, shots_dir)
                except KeyboardInterrupt:
                    raise
                except Exception as e:  # 评测器自身异常不能中断整轮
                    r = RunResult(
                        task_id=tid,
                        category=str(tspec.get("category") or ""),
                        task=str(tspec.get("task") or ""),
                        run_idx=idx,
                        verdict="error",
                        error=f"{type(e).__name__}: {e}",
                        check_detail=traceback.format_exc(limit=2),
                    )
                results.append(r)
                icon = {"pass": "✅", "fail": "❌", "manual": "👤",
                        "invalid": "⊘", "error": "⚠️"}.get(r.verdict, "?")
                print(
                    f"{icon} {r.verdict:<7} {r.steps:>2} 步 {r.elapsed_s:>6.1f}s  "
                    f"{(r.check_detail or r.error)[:60]}"
                )
                if cooldown and not (idx == runs and tspec is tasks[-1]):
                    time.sleep(cooldown)
    except KeyboardInterrupt:
        stopped = True
        print("\n\n已中断：保存已完成的部分结果。")

    if not results:
        print("没有任何结果，退出。")
        return 1

    summary = summarize(results, tasks)
    meta["interrupted"] = stopped
    print_summary(summary)
    md, js = write_report(results, summary, meta, args, out_dir)
    print(f"\n报告：{md}")
    print(f"数据：{js}")
    print(f"截图：{shots_dir}")

    # 退出码：全部有效样本都通过才 0
    t = summary["total"]
    return 0 if (t["fail"] == 0 and t["error"] == 0 and summary["counted"] > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
