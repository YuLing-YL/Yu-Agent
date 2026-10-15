"""技能系统包入口。"""
from __future__ import annotations

from .builtin import build_default_registry
from .registry import Skill, SkillRegistry

__all__ = ["Skill", "SkillRegistry", "build_default_registry"]
