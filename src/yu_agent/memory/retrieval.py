"""记忆检索：把长期记忆里跟当前任务有关的部分拼成给模型的上下文。

设计要点：

1. **知识优先于故事**。程序模板（怎么做）> 语义经验（要注意什么）>
   情节记录（上次发生了什么）。全是情节记忆时，模型只能看到一堆流水账。
2. **失败也要召回，而且要标出来** —— 「上次这么走不通」比成功路径更能
   阻止模型重复踩坑。
3. **必须限长**。记忆是辅助信息，塞太多会把当前截图的信息挤掉，反而更容易做错。
"""
from __future__ import annotations

from typing import List, Tuple

from .base import MemoryEntry
from .store import MemoryStore

# 类型权重：越小越先出现在 prompt 里
_KIND_WEIGHT = {"procedural": 0, "semantic": 1, "episodic": 2, "profile": 3}
# 注入 prompt 的总长度上限（字符）
_MAX_CHARS = 700

# 虚词：滑窗分词会把它们切出来，但对检索毫无价值
_STOPWORDS = {
    "帮我", "一下", "我的", "可以", "怎么", "什么", "一个", "然后", "这个", "那个",
    "现在", "如果", "我们", "你们", "他们", "是不", "不是", "就是", "能否", "麻烦",
    "please", "the", "and", "for", "with", "that",
}


class MemoryRetriever:
    """按当前任务从长期记忆里召回相关内容。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    # ------------------------------------------------------------------ #
    def retrieve(self, task: str, app: str = "", top_k: int = 5) -> List[MemoryEntry]:
        """关键词召回 + 按「相关性 / 类型 / 新旧」排序。"""
        keywords = self._extract_keywords(task)
        if not keywords and (task or "").strip():
            # 任务太短（"打开浏览器"）也要能召回，退化成整句匹配
            keywords = [task.strip()]
        if not keywords:
            return []

        seen: set = set()
        scored: List[Tuple[float, int, float, MemoryEntry]] = []
        # 最多查 16 个词，优先查长的（"天气" 比 "下明" 有信息量）
        probes = sorted(keywords, key=len, reverse=True)[:16]
        for kw in probes:
            for e in self.store.search(kw, app=app or None, limit=top_k * 2):
                if e.id is not None:
                    if e.id in seen:
                        continue
                    seen.add(e.id)
                hay = f"{e.content} {e.task}".lower()
                # 命中「长词」比命中「的/天」这种碎片更有说服力，按长度加权
                hits = sum(len(k) for k in keywords if k.lower() in hay)
                scored.append(
                    (
                        -float(hits),                     # 命中关键词越多越靠前
                        _KIND_WEIGHT.get(e.kind, 9),      # 同分时知识优先于流水账
                        -float(e.created_at or 0),        # 再按新旧
                        e,
                    )
                )
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        result = [s[3] for s in scored[:top_k]]

        # 通用经验往往不含任务关键词（"点输入框之前先确认焦点"），
        # 纯关键词召回永远捞不到它们。这里固定补最近 2 条语义经验，
        # 让"积累下来的注意事项"总能出现在 prompt 里。
        if len(result) < top_k:
            have = {e.id for e in result}
            for e in self.store.recent(kind="semantic", limit=2):
                if e.id in have:
                    continue
                result.append(e)
                if len(result) >= top_k:
                    break
        return result

    @staticmethod
    def _extract_keywords(task: str) -> List[str]:
        """任务关键词。

        中文没有空格，按空格切分等于把整句当成一个词去 LIKE，永远匹配不上
        （实测「帮我用Safari搜一下明天的天气」召回 0 条）。所以对中文长串
        再做 2~3 字滑窗，能命中「天气」「Safari」这类真正有检索价值的片段。
        """
        import re

        text = (task or "").strip()
        if not text:
            return []
        chunks = re.split(r"[\s，。！？、；：,\.!?;:\"'（）()\[\]【】]+", text)
        picked: List[str] = []

        def _chinese(seg: str) -> None:
            """中文段做 2~3 字滑窗。

            不设「只取前 N 个」的截断 —— 实测「帮我用Safari搜一下明天的天气」
            切出来的前 10 个全是 "我用/用S/Sa/af" 这种碎片，真正的「天气」
            被挤掉了，召回直接归零。宁可多查几次 LIKE（SQLite 上是微秒级）。
            """
            if len(seg) <= 3:
                if len(seg) >= 2:
                    picked.append(seg)
                return
            for n in (3, 2):
                for i in range(len(seg) - n + 1):
                    picked.append(seg[i : i + n])

        for w in chunks:
            w = w.strip()
            if not w:
                continue
            if re.fullmatch(r"[A-Za-z0-9]+", w):
                if len(w) >= 2:
                    picked.append(w)
                continue
            # 把混在中文里的英文/数字整段提出来（"用Safari搜" → "Safari" + 中文段）
            tail = 0
            for m in re.finditer(r"[A-Za-z0-9]+", w):
                _chinese(w[tail : m.start()])
                if len(m.group(0)) >= 2:
                    picked.append(m.group(0))
                tail = m.end()
            _chinese(w[tail:])

        # 去重保序 + 去掉虚词（这些词在任何句子里都出现，召回全靠它没有意义）
        seen: set = set()
        out: List[str] = []
        for w in picked:
            key = w.lower()
            if key in seen or key in _STOPWORDS:
                continue
            seen.add(key)
            out.append(w)
        return out[:24]

    # ------------------------------------------------------------------ #
    def context_for_prompt(self, task: str, app: str = "", language: str = "") -> str:
        """渲染成注入 prompt 的文本（带来源标注 + 长度上限）。"""
        entries = self.retrieve(task, app)
        if not entries:
            return ""
        en = str(language or "").strip().lower().startswith("en")

        head = (
            "Learned from previous tasks (reference only — may be stale; "
            "the current screenshot always wins):" if en
            else "以前任务里学到的（仅供参考，可能已过时；以当前截图为准）："
        )
        lines = [head]
        used = len(head)

        for e in entries:
            content = " ".join((e.content or "").split())
            if e.kind == "episodic":
                failed = (e.metadata or {}).get("success", 1) == 0
                # 内容本身已经带 [成功]/[失败] 前缀，成功时不再叠一个「历史」
                if failed:
                    content = ("[FAILED before] " if en else "[上次没成功] ") + content
            elif e.kind == "semantic":
                content = ("[tip] " if en else "[经验] ") + content
            elif e.kind == "procedural":
                # 程序模板是多行文本，压成一行，别打乱 prompt 排版
                content = ("[how-to] " if en else "[做法] ") + content.replace("\n", " | ")
            else:
                content = f"[{e.kind}] {content}"

            if len(content) > 180:
                content = content[:179] + "…"
            if used + len(content) > _MAX_CHARS:
                break
            lines.append(f"- {content}")
            used += len(content)

        return "\n".join(lines) if len(lines) > 1 else ""
