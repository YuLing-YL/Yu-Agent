"""Yu-Agent：基于 VLM 的 GUI 自动化智能体。"""
from __future__ import annotations

__version__ = "0.1.0"

from .agent_facade import YuAgent, build_agent_from_settings

__all__ = ["YuAgent", "build_agent_from_settings", "__version__"]
