"""步骤追踪：把每一步（截图 + 动作 + 结果 + token + 耗时）写入 runs/<ts>/steps.jsonl。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from .paths import new_run_dir


class Tracer:
    """逐步记录 agent 执行轨迹，便于回放 / 调试 / 蒸馏记忆。"""

    def __init__(self, run_dir: Optional[Path] = None):
        self.run_dir = run_dir or new_run_dir()
        self.steps_file = self.run_dir / "steps.jsonl"
        self._start = time.time()

    def log(self, step: int, kind: str, **payload: Any) -> None:
        record = {
            "step": step,
            "kind": kind,
            "ts": time.time() - self._start,
            **payload,
        }
        with self.steps_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def save_screenshot(self, data: bytes, name: str) -> Path:
        p = self.run_dir / name
        p.write_bytes(data)
        return p
