"""环境包。"""
from __future__ import annotations

import sys

from .base import Environment


def create_environment() -> Environment:
    """按当前平台创建环境。"""
    if sys.platform == "darwin":
        from .macos import MacOSEnvironment

        return MacOSEnvironment()
    if sys.platform.startswith("win"):
        from .windows import WindowsEnvironment

        return WindowsEnvironment()
    from .linux import LinuxEnvironment

    return LinuxEnvironment()
