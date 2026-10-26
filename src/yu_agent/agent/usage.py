"""调用计量：一次任务到底花了多少 token、多少钱、多少时间。

以前完全没有这个概念——跑一个任务花多少，只能去翻服务商后台。没有数字就
无法做取舍（是该换小模型、该减少编号数量、还是该少走两步），所以这里把
每类调用分开记：planner（决策）/ verify（复核）/ memory（总结），
再按配置里的单价折算成本。

单价缺失时不编造，只报 token 数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class UsageMeter:
    """按角色累计 token / 延迟。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_s: float = 0.0
    by_role: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # 单价（每 1K token，美元）。为空则不算钱。
    price_in: Optional[float] = None
    price_out: Optional[float] = None

    def add(self, usage: Any = None, latency_s: float = 0.0, role: str = "planner") -> None:
        """记一次调用。usage 是 models.base.TokenUsage（可能为 None）。"""
        self.calls += 1
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        tt = int(getattr(usage, "total_tokens", 0) or 0) or (pt + ct)
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.total_tokens += tt
        self.latency_s += float(latency_s or 0.0)

        slot = self.by_role.setdefault(
            role, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_s": 0.0}
        )
        slot["calls"] += 1
        slot["prompt_tokens"] += pt
        slot["completion_tokens"] += ct
        slot["latency_s"] += float(latency_s or 0.0)

    @property
    def cost(self) -> Optional[float]:
        """按单价估算成本（美元）。没有单价返回 None，不瞎猜。"""
        if self.price_in is None or self.price_out is None:
            return None
        return (
            self.prompt_tokens / 1000.0 * self.price_in
            + self.completion_tokens / 1000.0 * self.price_out
        )

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "llm_latency_s": round(self.latency_s, 2),
            "by_role": {k: dict(v) for k, v in self.by_role.items()},
        }
        cost = self.cost
        if cost is not None:
            out["cost_usd"] = round(cost, 4)
        return out

    def one_line(self) -> str:
        """给日志/状态栏看的一行摘要。"""
        base = (
            f"{self.calls} 次模型调用，"
            f"{self.total_tokens} tokens"
            f"（输入 {self.prompt_tokens} / 输出 {self.completion_tokens}），"
            f"模型耗时 {self.latency_s:.1f}s"
        )
        cost = self.cost
        if cost is not None:
            base += f"，约 ${cost:.4f}"
        return base
