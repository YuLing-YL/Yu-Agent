"""技能系统：Python 插件函数，AI 决策时可选择调用。

技能 = 一个带元数据的 Python 函数。注册后，技能名与参数说明会注入 prompt，
模型可以返回 `{"type": "skill", "skill": "open_app", "args": {...}}` 来调用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..common.errors import SkillNotFoundError


@dataclass
class Skill:
    name: str
    description: str
    func: Callable[..., Any]
    # 参数名 -> 说明。用 default_factory 而不是 None 默认值：
    # 之前写的是 `= None` + __post_init__ 兜底，但那让类型变成 Optional，
    # 每个读取点都得再判一次空，纯属自找麻烦。
    params: Dict[str, str] = field(default_factory=dict)
    returns: str = ""


class SkillRegistry:
    """技能注册表。"""

    def __init__(self):
        self._skills: Dict[str, Skill] = {}

    def register(
        self,
        name: str,
        description: str,
        params: Optional[Dict[str, str]] = None,
        returns: str = "",
    ) -> Callable:
        """装饰器：把函数注册为技能。"""

        def decorator(func: Callable) -> Callable:
            self._skills[name] = Skill(
                name=name,
                description=description,
                func=func,
                params=params or {},
                returns=returns,
            )
            return func

        return decorator

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise SkillNotFoundError(f"技能未注册：{name}")
        return self._skills[name]

    def call(self, name: str, **kwargs: Any) -> Any:
        skill = self.get(name)
        return skill.func(**kwargs)

    def has(self, name: str) -> bool:
        return name in self._skills

    def list_skills(self) -> List[Skill]:
        return list(self._skills.values())

    def describe_for_prompt(self) -> str:
        """生成技能说明，注入 prompt。"""
        if not self._skills:
            return "（无可用技能）"
        lines = []
        for s in self._skills.values():
            params = ", ".join(f"{k}({v})" for k, v in s.params.items())
            lines.append(f"- {s.name}: {s.description}" + (f" 参数: {params}" if params else ""))
        return "\n".join(lines)
