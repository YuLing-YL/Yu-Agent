#!/usr/bin/env python3
"""对比两次评测结果，回答「这次改动到底有没有变好」。

用法
----
    python eval/compare.py eval/reports/A/results.json eval/reports/B/results.json
    python eval/compare.py --latest          # 用最近两份结果自动对比
    python eval/compare.py --latest 3        # 列出最近三份

为什么单独写一个：评测的价值全在对比。改了提示词、换了标注模式、换了模型，
只有把两次的同一批任务放在一起看，才分得清「真的变好了」还是「这次碰巧」。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

EVAL_DIR = Path(__file__).resolve().parent
REPORT_DIR = EVAL_DIR / "reports"


def _pad(text: str, width: int) -> str:
    """按显示宽度补齐（中文算 2 列）。"""
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))
    return str(text) + " " * max(0, width - w)


def load(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    data["_path"] = str(path)
    return data


def label_of(data: Dict[str, Any]) -> str:
    meta = data.get("meta") or {}
    tag = meta.get("tag") or ""
    ts = str(meta.get("timestamp") or "")
    model = str(meta.get("model") or "")
    parts = [p for p in (tag, ts[5:16], model) if p]
    return " / ".join(parts) or Path(data["_path"]).parent.name


def recent(n: int) -> List[Path]:
    if not REPORT_DIR.exists():
        return []
    dirs = sorted(
        [d for d in REPORT_DIR.iterdir() if (d / "results.json").exists()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    return [d / "results.json" for d in dirs[:n]]


def _rate(data: Dict[str, Any], task_id: Optional[str] = None) -> Optional[float]:
    rs = data.get("results") or []
    if task_id is not None:
        rs = [r for r in rs if r.get("task_id") == task_id]
    counted = [r for r in rs if r.get("verdict") in ("pass", "fail")]
    if not counted:
        return None
    return sum(1 for r in counted if r["verdict"] == "pass") / len(counted)


def _avg(data: Dict[str, Any], key: str, task_id: Optional[str] = None) -> Optional[float]:
    rs = data.get("results") or []
    if task_id is not None:
        rs = [r for r in rs if r.get("task_id") == task_id]
    rs = [r for r in rs if r.get("verdict") in ("pass", "fail")]
    if not rs:
        return None
    return sum(float(r.get(key) or 0) for r in rs) / len(rs)


def _count(data: Dict[str, Any], verdict: str) -> int:
    return sum(1 for r in (data.get("results") or []) if r.get("verdict") == verdict)


def _fmt_rate(v: Optional[float]) -> str:
    return f"{v * 100:.0f}%" if v is not None else "—"


def _delta_rate(a: Optional[float], b: Optional[float]) -> str:
    if a is None or b is None:
        return "—"
    d = (b - a) * 100
    if abs(d) < 0.5:
        return "持平"
    return f"{'+' if d > 0 else ''}{d:.0f}pt"


def _delta_num(a: Optional[float], b: Optional[float], unit: str = "") -> str:
    if a is None or b is None:
        return "—"
    d = b - a
    if abs(d) < 0.05:
        return "持平"
    return f"{'+' if d > 0 else ''}{d:.1f}{unit}"


def build_markdown(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    la, lb = label_of(a), label_of(b)
    lines: List[str] = []
    lines.append("# 评测对比")
    lines.append("")
    lines.append(f"- A：`{la}`")
    lines.append(f"- B：`{lb}`")
    lines.append("")

    # ── 总览 ──
    ra, rb = _rate(a), _rate(b)
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | A | B | 差异 |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(f"| 成功率 | {_fmt_rate(ra)} | {_fmt_rate(rb)} | {_delta_rate(ra, rb)} |")
    lines.append(
        f"| 可计分运行数 | {sum(1 for r in a['results'] if r['verdict'] in ('pass','fail'))} "
        f"| {sum(1 for r in b['results'] if r['verdict'] in ('pass','fail'))} | — |"
    )
    lines.append(
        f"| 平均步数 | {_avg(a, 'steps') or 0:.1f} | {_avg(b, 'steps') or 0:.1f} "
        f"| {_delta_num(_avg(a, 'steps'), _avg(b, 'steps'))} |"
    )
    lines.append(
        f"| 平均耗时 | {_avg(a, 'elapsed_s') or 0:.1f}s | {_avg(b, 'elapsed_s') or 0:.1f}s "
        f"| {_delta_num(_avg(a, 'elapsed_s'), _avg(b, 'elapsed_s'), 's')} |"
    )
    lines.append(f"| 待人工 | {_count(a, 'manual')} | {_count(b, 'manual')} | — |")
    lines.append(f"| 无效起点 | {_count(a, 'invalid')} | {_count(b, 'invalid')} | — |")
    lines.append(f"| 运行异常 | {_count(a, 'error')} | {_count(b, 'error')} | — |")
    lines.append("")
    lines.append("> 只有两边都有可计分样本的任务才能比较成功率；`待人工`/`无效起点` 多了，"
                 "说明判据或前置条件需要调整，别直接拿成功率下结论。")
    lines.append("")

    # ── 逐任务 ──
    ids = []
    for data in (a, b):
        for r in data.get("results") or []:
            tid = r.get("task_id")
            if tid and tid not in ids:
                ids.append(tid)

    lines.append("## 逐任务")
    lines.append("")
    lines.append("| 任务 | A 成功率 | B 成功率 | 差异 | A 步数 | B 步数 | 结论 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    better, worse = [], []
    for tid in ids:
        ta, tb = _rate(a, tid), _rate(b, tid)
        sa, sb = _avg(a, "steps", tid), _avg(b, "steps", tid)
        mark = "—"
        if ta is not None and tb is not None:
            if tb > ta:
                mark = "✅ 变好"
                better.append(tid)
            elif tb < ta:
                mark = "❌ 变差"
                worse.append(tid)
            else:
                mark = "持平"
        lines.append(
            f"| `{tid}` | {_fmt_rate(ta)} | {_fmt_rate(tb)} | {_delta_rate(ta, tb)} | "
            f"{sa:.1f} | {sb:.1f} | {mark} |" if sa is not None and sb is not None
            else f"| `{tid}` | {_fmt_rate(ta)} | {_fmt_rate(tb)} | {_delta_rate(ta, tb)} "
                 f"| — | — | {mark} |"
        )
    lines.append("")

    # ── 结论 ──
    lines.append("## 该怎么看")
    lines.append("")
    if ra is not None and rb is not None:
        if rb - ra >= 0.1:
            lines.append(f"- 整体成功率提升 **{(rb - ra) * 100:.0f}pt**，改动方向是对的。")
        elif ra - rb >= 0.1:
            lines.append(f"- 整体成功率下降 **{(ra - rb) * 100:.0f}pt**，改动有副作用，建议回滚或缩小范围。")
        else:
            lines.append("- 整体成功率变化在 10pt 以内，**还看不出结论**——样本太少，"
                         "把 `-n` 调到 5 或更多再跑一次。")
    if better:
        lines.append(f"- 变好的任务：{', '.join(f'`{x}`' for x in better)}")
    if worse:
        lines.append(f"- **变差的任务**：{', '.join(f'`{x}`' for x in worse)}"
                     f"　→ 优先查这几个，往往能定位到某个能力被改坏了。")
    if not better and not worse:
        lines.append("- 没有任务的成绩发生变化。")
    lines.append("")
    return "\n".join(lines)


def print_console(a: Dict[str, Any], b: Dict[str, Any], md_path: Path) -> None:
    ra, rb = _rate(a), _rate(b)
    print()
    print(f"A：{label_of(a)}")
    print(f"B：{label_of(b)}")
    print()
    print(_pad("指标", 16) + _pad("A", 14) + _pad("B", 14) + "差异")
    print("-" * 60)
    rows = [
        ("成功率", _fmt_rate(ra), _fmt_rate(rb), _delta_rate(ra, rb)),
        ("平均步数", f"{_avg(a, 'steps') or 0:.1f}", f"{_avg(b, 'steps') or 0:.1f}",
         _delta_num(_avg(a, "steps"), _avg(b, "steps"))),
        ("平均耗时", f"{_avg(a, 'elapsed_s') or 0:.1f}s", f"{_avg(b, 'elapsed_s') or 0:.1f}s",
         _delta_num(_avg(a, "elapsed_s"), _avg(b, "elapsed_s"), "s")),
        ("待人工", str(_count(a, "manual")), str(_count(b, "manual")), ""),
        ("无效起点", str(_count(a, "invalid")), str(_count(b, "invalid")), ""),
        ("运行异常", str(_count(a, "error")), str(_count(b, "error")), ""),
    ]
    for name, va, vb, d in rows:
        print(_pad(name, 16) + _pad(va, 14) + _pad(vb, 14) + d)
    print("-" * 60)
    print(f"详细对比：{md_path}")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="对比两次评测结果")
    p.add_argument("a", nargs="?", help="A 的 results.json")
    p.add_argument("b", nargs="?", help="B 的 results.json")
    p.add_argument("--latest", type=int, nargs="?", const=2, default=0,
                   help="自动取最近 N 份（默认 2：最新两份对比）")
    args = p.parse_args(argv)

    if args.latest:
        paths = recent(args.latest)
        if len(paths) < 2:
            print(f"最近只有 {len(paths)} 份结果，至少要两份才能对比。先跑两次评测：")
            print("  python eval/runner.py --tag A")
            print("  python eval/runner.py --tag B")
            return 2
        if args.latest > 2:
            print("最近的结果：")
            for x in paths:
                print(f"  {x}")
            return 0
        pa, pb = paths[1], paths[0]  # 时间上：A 早、B 晚
    else:
        if not (args.a and args.b):
            p.error("要么给两个 results.json，要么用 --latest")
        pa, pb = Path(args.a), Path(args.b)
        for x in (pa, pb):
            if not x.exists():
                print(f"找不到文件：{x}")
                return 2

    a, b = load(pa), load(pb)
    md = build_markdown(a, b)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"compare-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text(md, encoding="utf-8")
    print_console(a, b, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
