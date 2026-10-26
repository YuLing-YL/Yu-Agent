"""敏感信息脱敏：写日志 / trace 落盘前把 key、token 打码。

为什么需要：`runs/<ts>/trace.jsonl` 里保存的是**模型原始输出**，模型偶尔会把
用户粘贴的 key、URL 里的 token 复述出来；这些文件默认留在硬盘上、还经常被
打包发给别人排障。config 里的 `safety.redact_secrets_in_traces` 以前只是个摆设，
这里把它真正接上。
"""
from __future__ import annotations

import re
from typing import Any

# 常见密钥形状：sk-xxx / ghp_xxx / AKIAxxx / Bearer xxx / api_key=xxx
_PATTERNS = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{8,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{12,})"),
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{8,})"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|secret|password)\s*[\"']?\s*[:=]\s*[\"']?)"
        r"([^\s\"',}]{6,})"
    ),
]

_MASK = "***"


def redact_secrets(text: Any) -> str:
    """把文本里的疑似密钥替换成 ***；非字符串原样 str 化。"""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return s
    for pat in _PATTERNS:
        if pat.groups >= 2:
            s = pat.sub(lambda m: f"{m.group(1)}{_MASK}", s)
        else:
            s = pat.sub(_MASK, s)
    return s
