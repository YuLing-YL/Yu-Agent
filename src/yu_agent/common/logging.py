"""日志初始化：同时输出到控制台和文件。

文件日志放在 data/logs/yu_agent.log，出问题时可以直接翻这个文件，
不用依赖启动 GUI 的那个终端窗口还在。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_configured = False
_log_file = None


def setup_logging(level: int = logging.INFO) -> None:
    """初始化根日志（幂等）。"""
    global _configured, _log_file
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 文件日志（写不进去就只保留控制台，不影响运行）
    try:
        from .paths import data_dir

        log_dir = data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "yu_agent.log"
        fh = RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
        _log_file = path
    except Exception:
        pass

    _configured = True


def log_file_path():
    """当前日志文件路径（可能为 None）。"""
    return _log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
