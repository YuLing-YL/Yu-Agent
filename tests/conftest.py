"""pytest 全局隔离：测试绝对不许写进用户真实的 runs/ 目录。

背景（用户实际反馈）：「runs 里面每次保存的图片都是纯黑的，什么都没有」。
查下来 312/350 个运行目录是**测试留下的**：测试用的是假屏幕（`Image.new()` 默认黑），
每跑一次 pytest 就往 runs/ 里塞十几个 `step_XX.png` 全黑图 + trace.jsonl，
而且时间戳最新，用户按时间打开目录看到的当然是黑的。

所以这里在**导入任何 yu_agent 模块之前**把运行记录改道到临时目录，
测试结束随临时目录一起销毁。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TMP_RUNS = Path(tempfile.mkdtemp(prefix="yu-agent-runs-"))
os.environ["YU_AGENT_RUNS_DIR"] = str(_TMP_RUNS)
# 日志也别混进用户的 data/logs，出问题时才好对照
_TMP_HOME = _TMP_RUNS.parent / (_TMP_RUNS.name + "-home")
_TMP_HOME.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("YU_AGENT_HOME", str(_TMP_HOME))


def _cleanup() -> None:
    shutil.rmtree(_TMP_RUNS, ignore_errors=True)
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


atexit.register(_cleanup)
