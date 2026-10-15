"""项目路径管理：定位项目根、runs 目录、数据目录等。"""
from __future__ import annotations

import os
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


def new_run_dir() -> Path:
    """为一次任务分配 runs/<YYYYMMDD-HHMMSS>/ 目录。"""
    runs = project_root() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = runs / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d
