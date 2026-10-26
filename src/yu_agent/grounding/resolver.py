"""定位解析器：把模型动作解析并落地为可执行动作。"""
from __future__ import annotations

from typing import List

from ..actions.schema import Action
from ..common.errors import GroundingError
from .parser import parse_actions


class GroundingResolver:
    """把模型原始文本输出解析成 Action 列表。"""

    def __init__(self, coords_mode: str = "pixel"):
        self.coords_mode = coords_mode

    def resolve(self, text: str) -> List[Action]:
        try:
            actions = parse_actions(text)
        except GroundingError:
            raise
        if not actions:
            raise GroundingError("模型未返回任何动作")
        return actions

    def resolve_single(self, text: str) -> Action:
        actions = self.resolve(text)
        return actions[0]
