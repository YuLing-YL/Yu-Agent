"""悬浮控制条：任务运行时固定在屏幕最上层，随时可停止。

设计要点（解决"切到别的应用就看不到界面、无法停止"的问题）：
- 无边框 + 置顶（WindowStaysOnTopHint）+ Tool 窗口（不占 Dock）
- 通过 macOS 原生 API 设置 collectionBehavior，使其在所有桌面 / 全屏应用之上可见
- WA_MacAlwaysShowToolWindow：本应用失去焦点时也不隐藏（任务运行中必然会切走焦点）
- 可拖动，包含：状态点 + 状态文字 + 步数 + 「停止」+ 「显示主窗口」
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)


def enable_all_spaces(widget: QWidget) -> None:
    """让窗口在所有桌面 / 全屏应用之上可见（macOS 原生能力）。

    NSWindowCollectionBehavior 取值（见 AppKit）：
      CanJoinAllSpaces = 1，Stationary = 16，FullScreenAuxiliary = 256
    注意不要加 Transient(8)，否则应用失去焦点时窗口会隐藏。

    实现方式：按窗口标题在本进程的 NSWindow 列表里查找，避免直接操作裸指针
    （在不支持原生窗口的平台如 offscreen 上，裸指针调用会导致进程异常退出）。
    """
    import sys

    if sys.platform != "darwin":
        return
    try:
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.platformName() != "cocoa":
            return  # 非真实 macOS 窗口（offscreen 等），跳过
    except Exception:
        return

    try:
        import AppKit

        behavior = (
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        title = widget.windowTitle()
        ns_app = AppKit.NSApplication.sharedApplication()
        if ns_app is None:
            return
        for win in ns_app.windows():
            try:
                if str(win.title()) != title:
                    continue
                win.setCollectionBehavior_(behavior)
                # NSFloatingWindowLevel = 3，确保浮在其他应用之上
                win.setLevel_(3)
            except Exception:
                continue
    except Exception:
        # 任何异常都静默降级（仍保持 Qt 层的置顶标志）
        pass


class FloatingBar(QWidget):
    """任务运行时的悬浮控制条。"""

    stop_clicked = Signal()
    show_main_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("floatBar")
        self.setWindowTitle("Yu-Agent 控制条")
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        # 无边框窗口默认不画 QSS 背景，显式打开，否则控制条会是一片透明/黑块
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 本应用失去焦点时保持可见（任务运行必然会切走焦点）
        try:
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        except Exception:
            pass

        self._drag_offset = None
        self._build_ui()

    # ---- UI ----
    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 10)
        layout.setSpacing(10)

        self.dot = QLabel("●")
        self.dot.setObjectName("floatDot")
        layout.addWidget(self.dot)

        text_box = QWidget()
        text_layout = QHBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(8)

        self.status_label = QLabel("运行中…")
        self.status_label.setObjectName("floatStatus")
        self.status_label.setMaximumWidth(240)
        text_layout.addWidget(self.status_label)

        self.step_label = QLabel("")
        self.step_label.setObjectName("floatStep")
        text_layout.addWidget(self.step_label)

        layout.addWidget(text_box)
        layout.addStretch()

        self.show_btn = QPushButton("主窗口")
        self.show_btn.setObjectName("floatGhost")
        self.show_btn.clicked.connect(self.show_main_clicked.emit)
        layout.addWidget(self.show_btn)

        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setObjectName("floatStop")
        self.stop_btn.clicked.connect(self.stop_clicked.emit)
        layout.addWidget(self.stop_btn)

        self.setFixedHeight(56)
        self.setMinimumWidth(420)

    # ---- 外部接口 ----
    def update_status(self, text: str, step: Optional[int] = None) -> None:
        if len(text) > 44:
            text = text[:44] + "…"
        self.status_label.setText(text)
        self.step_label.setText(f"第 {step} 步" if step else "")

    def mark_finished(self, success: bool, reason: str = "") -> None:
        self.dot.setObjectName("floatDotDone" if success else "floatDotFail")
        # 触发样式重算
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        msg = "✅ 已完成" if success else "❌ 已结束"
        if reason:
            msg += f"：{reason}"
        self.update_status(msg)
        self.stop_btn.setText("关闭")
        try:
            self.stop_btn.clicked.disconnect()
        except Exception:
            pass
        self.stop_btn.clicked.connect(self.hide)

    def reset_for_run(self) -> None:
        self.dot.setObjectName("floatDot")
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        self.update_status("运行中…")
        self.stop_btn.setText("■ 停止")
        try:
            self.stop_btn.clicked.disconnect()
        except Exception:
            pass
        self.stop_btn.clicked.connect(self.stop_clicked.emit)

    def show_at_default(self) -> None:
        """在屏幕右上角（菜单栏下方）显示。"""
        self.adjustSize()
        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = geo.right() - self.width() - 24
            y = geo.top() + 12
            self.move(max(geo.left(), x), y)
        self.show()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 显示后设置跨桌面行为（需窗口已创建）
        enable_all_spaces(self)

    # ---- 拖动 ----
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        event.accept()
