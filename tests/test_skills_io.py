"""技能回归测试：文件读写 / 剪贴板 / 屏幕文字，以及注册表是否把它们装上了。

以前技能只有 8 个系统级动作（打开应用、截图、按键…），凡是涉及「文件内容」
的子任务都只能靠模拟 GUI 去点编辑器，又慢又容易错。这里锁住新加的数据类技能。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yu_agent.skills import build_default_registry

_TMP = Path(__file__).resolve().parent / "_tmp_skills"


def _call(name: str, **kwargs) -> str:
    return build_default_registry().call(name, **kwargs)


def test_skills_registered() -> None:
    reg = build_default_registry()
    listed = reg.describe_for_prompt()
    for name in (
        "click_text", "read_screen_text", "ocr_status",
        "read_file", "write_file", "list_dir",
        "clipboard_get", "clipboard_set",
    ):
        assert f"- {name}:" in listed, f"技能未注册：{name}"
    print("✓ 屏幕文字 / 文件 / 剪贴板技能都已注册")


def test_write_read_roundtrip() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)
    path = _TMP / "sub" / "note.txt"
    try:
        out = _call("write_file", path=str(path), text="第一行\n第二行")
        assert "已写入" in out, out
        assert path.exists(), "目录不存在时应自动创建"

        back = _call("read_file", path=str(path))
        assert "第一行" in back and "第二行" in back, back

        _call("write_file", path=str(path), text="追加", append=True)
        back2 = _call("read_file", path=str(path))
        assert back2.rstrip().endswith("追加"), back2
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("✓ write_file 自动建目录；read_file 读回原文；append 生效")


def test_read_file_rejects_binary_and_missing() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)
    _TMP.mkdir(parents=True, exist_ok=True)
    binary = _TMP / "a.bin"
    binary.write_bytes(b"\x00\x01\x02\xff\xfe")
    try:
        out = _call("read_file", path=str(binary))
        assert "不是文本文件" in out, out
        missing = _call("read_file", path=str(_TMP / "nope.txt"))
        assert "不存在" in missing, missing
        as_dir = _call("read_file", path=str(_TMP))
        assert "目录" in as_dir, as_dir
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("✓ 二进制/不存在/目录 三种错误都给出确切原因")


def test_list_dir() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)
    (_TMP / "d1").mkdir(parents=True)
    (_TMP / "f1.txt").write_text("x", encoding="utf-8")
    try:
        out = _call("list_dir", path=str(_TMP))
        assert "d1" in out and "f1.txt" in out, out
        assert "📁" in out, "目录要有标记，模型才能区分"
        assert "不存在" in _call("list_dir", path=str(_TMP / "nope"))
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("✓ list_dir 区分目录/文件，不存在的路径有明确提示")


def test_clipboard_roundtrip_restores() -> None:
    """剪贴板：写入后能读回；结束时把原内容还回去（不破坏用户数据）。"""
    original = _call("clipboard_get", max_chars=100000)
    if original.startswith("剪贴板里没有文本"):
        original_text = ""
    else:
        original_text = original
    try:
        out = _call("clipboard_set", text="yu-agent 测试字符串")
        assert "已复制" in out, out
        back = _call("clipboard_get")
        assert "yu-agent 测试字符串" in back, back
    finally:
        _call("clipboard_set", text=original_text)
    print("✓ 剪贴板读写可用（并已还原原内容）")


if __name__ == "__main__":
    test_skills_registered()
    test_write_read_roundtrip()
    test_read_file_rejects_binary_and_missing()
    test_list_dir()
    test_clipboard_roundtrip_restores()
    print("\nALL PASS")
