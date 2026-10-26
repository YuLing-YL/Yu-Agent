"""项目路径管理：定位项目根、runs 目录、数据目录等。"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path


def project_root() -> Path:
    """返回项目根目录（pyproject.toml 所在处）。"""
    # 从本文件向上：common -> yu_agent -> src -> 项目根
    here = Path(__file__).resolve()
    return here.parent.parent.parent.parent


def data_dir() -> Path:
    """数据目录（记忆 sqlite、用户配置等）。"""
    root = os.environ.get("YU_AGENT_HOME")
    if root:
        p = Path(root)
    else:
        p = project_root() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def memory_dir() -> Path:
    p = data_dir() / "memory"
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_config_dir() -> Path:
    """用户可写配置目录（GUI 设置持久化到这里）。"""
    p = data_dir() / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runs_dir() -> Path:
    """运行记录根目录。

    `YU_AGENT_RUNS_DIR` 可以整体改道：测试/评测必须指向临时目录，
    否则每跑一次 pytest 都会往用户真实的 runs/ 里塞一批**纯黑假截图**
    （测试用假屏幕），看起来就像「agent 保存的图片全是黑的」。
    """
    env = os.environ.get("YU_AGENT_RUNS_DIR")
    p = Path(env) if env else (project_root() / "runs")
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_run_dir() -> Path:
    """为一次任务分配 runs/<YYYYMMDD-HHMMSS>/ 目录。

    ⚠️ 同一秒内启动两次（GUI 重跑、pytest 连续两个用例）会撞名，
    早期直接返回同一个目录：两轮 trace.jsonl 追加混在一起，
    step_01.png 被后者覆盖 —— 看起来就是「记录错乱」。这里撞名就加后缀。
    """
    runs = runs_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = runs / stamp
    n = 2
    while d.exists():
        d = runs / f"{stamp}-{n}"
        n += 1
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_runs(keep: int) -> int:
    """只保留最近 `keep` 次运行目录，返回删除的目录数。

    没有这一步，runs/ 会无限增长（实测一次体检时已经 358 个目录 / 113MB）。
    按目录名排序即可：名字就是 `YYYYMMDD-HHMMSS[-n]`。
    """
    if keep is None or keep <= 0:
        return 0
    runs = runs_dir()
    try:
        dirs = sorted((p for p in runs.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return 0
    extra = len(dirs) - keep
    if extra <= 0:
        return 0
    removed = 0
    for d in dirs[:extra]:
        try:
            shutil.rmtree(d)
            removed += 1
        except OSError:
            pass  # 正在被别的进程打开/权限问题：跳过，下次再清
    return removed
