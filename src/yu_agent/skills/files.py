"""文件读写技能：把「读文件 / 写文件 / 列目录」交给代码，而不是让模型去点编辑器。

为什么值得单独做一组：凡是涉及文件内容的子任务，走 GUI 都又慢又容易错
（打开、等待、全选、复制）；直接读写是确定性的，而且失败会给出确切原因。
安全上只做「文本文件」，二进制一律拒绝——agent 目前没有处理二进制的手段，
硬读只会把乱码塞进上下文。
"""
from __future__ import annotations

from pathlib import Path

# 单次读取上限：再大就不该往上下文里塞了，让模型改用命令行工具
_MAX_READ_BYTES = 200_000


def _expand(path: str) -> Path:
    """展开 ~ 与相对路径；不做存在性检查（各函数自己判断）。"""
    return Path(path).expanduser()


def register(reg) -> None:
    """把文件相关技能注册进技能表。"""

    @reg.register(
        "read_file",
        "读取一个**文本文件**的内容（支持 ~ 展开）。适合「看看这个文件里写了什么」",
        params={
            "path": "文件路径，如 ~/Desktop/notes.txt",
            "max_chars": "最多返回多少字符（默认 4000）",
        },
        returns="文件内容；文件不存在 / 二进制 / 过大时说明具体原因",
    )
    def read_file(path: str, max_chars: int = 4000) -> str:
        p = _expand(path or "")
        if not path:
            return "读取失败：没有给出文件路径"
        if not p.exists():
            return f"读取失败：文件不存在（{p}）"
        if p.is_dir():
            return f"读取失败：{p} 是一个目录，用 list_dir 来看里面有什么"
        try:
            size = p.stat().st_size
        except OSError as e:
            return f"读取失败：无法获取文件信息（{e}）"
        if size > _MAX_READ_BYTES:
            return (
                f"读取失败：文件太大（{size / 1024:.0f} KB，上限 {_MAX_READ_BYTES // 1024} KB）。"
                f"请改用命令行工具处理，或只读前面一部分。"
            )
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = p.read_text(encoding="gbk")
            except UnicodeDecodeError:
                return f"读取失败：{p.name} 不是文本文件（UTF-8 / GBK 都解不开）"
        except OSError as e:
            return f"读取失败：{e}"

        limit = max(200, int(max_chars))
        if len(text) > limit:
            return f"{text[:limit]}\n\n…（已截断，全文 {len(text)} 字符）"
        return text or "（文件是空的）"

    @reg.register(
        "write_file",
        "把文本写入文件（默认覆盖，append=true 追加）。目录不存在会自动创建",
        params={
            "path": "目标路径，如 ~/Desktop/out.txt",
            "text": "要写入的文本",
            "append": "true=追加到末尾；默认覆盖",
        },
        returns="真实结果：写入了多少字符、写到哪个绝对路径",
    )
    def write_file(path: str, text: str, append: bool = False) -> str:
        if not path:
            return "写入失败：没有给出路径"
        p = _expand(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with p.open(mode, encoding="utf-8") as fh:
                fh.write(text or "")
        except OSError as e:
            return f"写入失败：{e}"
        verb = "追加" if append else "写入"
        return f"已{verb} {len(text or '')} 个字符到 {p}"

    @reg.register(
        "list_dir",
        "列出一个目录里的文件和子目录（名称 + 是否目录）",
        params={"path": "目录路径，如 ~/Downloads", "limit": "最多列多少项（默认 40）"},
        returns="逐行列出条目；目录不存在会说明",
    )
    def list_dir(path: str, limit: int = 40) -> str:
        if not path:
            return "列目录失败：没有给出路径"
        p = _expand(path)
        if not p.exists():
            return f"列目录失败：路径不存在（{p}）"
        if not p.is_dir():
            return f"列目录失败：{p} 不是目录"
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError as e:
            return f"列目录失败：{e}"
        if not entries:
            return f"{p} 是空目录"
        cap = max(1, int(limit))
        lines = [f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries[:cap]]
        more = f"\n…（共 {len(entries)} 项，只列前 {cap} 项）" if len(entries) > cap else ""
        return "\n".join(lines) + more
