"""剪贴板技能：读 / 写系统剪贴板。

用途：跨应用搬数据最稳的一条路——在 A 应用复制、让代码读出来、再写进 B，
比模拟"全选+复制+切换+粘贴"少三步，也不会因为焦点跑掉而粘错地方。
macOS 用 pbcopy/pbpaste（系统自带，无依赖）；其它平台走 pyperclip（可选）。
"""
from __future__ import annotations

import subprocess
import sys

_MAX_CHARS = 100_000


def _pbpaste() -> str:
    r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
    return r.stdout if r.returncode == 0 else ""


def _pbcopy(text: str) -> bool:
    r = subprocess.run(
        ["pbcopy"], input=text, text=True, capture_output=True, timeout=5
    )
    return r.returncode == 0


def register(reg) -> None:
    """把剪贴板技能注册进技能表。"""

    @reg.register(
        "clipboard_get",
        "读取系统剪贴板里的文本（刚复制的内容、上一步复制的结果）",
        params={"max_chars": "最多返回多少字符（默认 2000）"},
        returns="剪贴板文本；为空或不可读会说明",
    )
    def clipboard_get(max_chars: int = 2000) -> str:
        try:
            if sys.platform == "darwin":
                text = _pbpaste()
            else:
                import pyperclip

                text = pyperclip.paste() or ""
        except Exception as e:
            return f"读取剪贴板失败：{e}"
        if not text:
            return "剪贴板里没有文本（可能是空的，或里面是图片/文件）"
        limit = max(50, min(_MAX_CHARS, int(max_chars)))
        if len(text) > limit:
            return f"{text[:limit]}\n…（已截断，全文 {len(text)} 字符）"
        return text

    @reg.register(
        "clipboard_set",
        "把文本写入系统剪贴板（之后可以用 cmd+v 粘贴到任何地方）",
        params={"text": "要复制的文本"},
        returns="真实结果：写入成功与否、写了多少字符",
    )
    def clipboard_set(text: str) -> str:
        content = text or ""
        try:
            if sys.platform == "darwin":
                ok = _pbcopy(content)
            else:
                import pyperclip

                pyperclip.copy(content)
                ok = True
        except Exception as e:
            return f"写入剪贴板失败：{e}"
        if not ok:
            return "写入剪贴板失败：pbcopy 返回非零退出码"
        return f"已复制 {len(content)} 个字符到剪贴板"
