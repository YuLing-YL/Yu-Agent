"""记忆系统统一入口。"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .distillation import MemoryDistiller
from .episodic import EpisodicMemory
from .procedural import ProceduralMemory
from .profile import ProfileMemory
from .retrieval import MemoryRetriever
from .semantic import SemanticMemory
from .store import MemoryStore
from .working import StepRecord, WorkingMemory


class MemoryManager:
    """整合短时（工作）记忆 + 长期（情节/语义/程序/画像）记忆。

    - 短时：WorkingMemory，仅当前任务有效
    - 长期：SQLite 持久化，跨会话/跨任务
    """

    def __init__(self, store: Optional[MemoryStore] = None, enabled: bool = True):
        self.enabled = enabled
        self.store = store or MemoryStore()
        self.working = WorkingMemory()
        self.episodic = EpisodicMemory(self.store)
        self.semantic = SemanticMemory(self.store)
        self.procedural = ProceduralMemory(self.store)
        self.profile = ProfileMemory(self.store)
        self.retriever = MemoryRetriever(self.store)
        self.distiller = MemoryDistiller(self.episodic, self.semantic, self.procedural)

    # ---- 短时记忆 ----
    def start_task(self, task: str) -> None:
        self.working.reset(task)

    def add_step(
        self,
        step: int,
        observation_summary: str,
        action: str,
        result: str,
        effect: str = "",
        screenshot_b64: Optional[str] = None,
    ) -> None:
        self.working.add_step(
            StepRecord(
                step=step,
                observation_summary=observation_summary,
                action=action,
                result=result,
                effect=effect,
                screenshot_b64=screenshot_b64,
            )
        )

    def set_subtasks(self, subtasks: List[str]) -> None:
        self.working.set_subtasks(subtasks)

    # ---- 长期记忆 ----
    def remember_episodic(self, task: str, content: str, success: bool = True) -> None:
        self.episodic.record(task, content, success=success)

    def remember_semantic(self, content: str, app: str = "") -> None:
        self.semantic.remember(content, app=app)

    def remember_procedural(self, name: str, steps: List[str], app: str = "") -> None:
        self.procedural.remember(name, steps, app=app)

    def set_preference(self, key: str, value: str) -> None:
        self.profile.set_preference(key, value)

    # ---- 检索 ----
    def recall_context(self, task: str, language: str = "") -> str:
        """召回长期记忆，作为 prompt 注入。"""
        if not self.enabled:
            return ""
        try:
            return self.retriever.context_for_prompt(task, language=language)
        except Exception:
            return ""

    def profile_context(self) -> str:
        if not self.enabled:
            return ""
        return self.profile.describe()

    # ---- 蒸馏 ----
    def finish_task(
        self,
        task: str,
        steps: List[Dict[str, Any]],
        success: bool,
        summarize: Optional[Callable[[str, str], str]] = None,
        language: str = "",
    ) -> Dict[str, Any]:
        """任务结束时把轨迹提炼进长期记忆。

        summarize(system, user) -> text：可选的模型总结函数（由 AgentLoop 提供）。
        返回 {summary, lesson, steps, used_llm}，供 GUI/日志展示「记住了什么」。
        """
        if not self.enabled:
            return {}
        try:
            return self.distiller.distill(
                task, steps, success, summarize=summarize, language=language
            )
        except Exception:
            # 记忆写失败绝不能影响任务结果
            return {}

    def memory_view(self, limit: int = 20) -> Dict[str, List[Dict[str, Any]]]:
        """按类型列出最近的长期记忆（CLI/GUI 查看用）。"""
        out: Dict[str, List[Dict[str, Any]]] = {}
        for kind in ("episodic", "semantic", "procedural", "profile"):
            rows = self.store.recent(kind=kind, limit=limit)
            out[kind] = [
                {
                    "id": e.id,
                    "content": e.content,
                    "created_at": e.created_at,
                    "success": (e.metadata or {}).get("success", 1),
                }
                for e in rows
            ]
        return out

    def close(self) -> None:
        self.store.close()
