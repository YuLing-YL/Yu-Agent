"""GUI 主题样式（QSS）。"""
from __future__ import annotations

# 现代、简洁的深色主题
APP_STYLESHEET = """
* {
    font-family: "PingFang SC", "Helvetica Neue", "Microsoft YaHei", sans-serif;
    font-size: 13px;
}

QMainWindow, QWidget#root {
    background-color: #1e1e2e;
    color: #cdd6f4;
}

QLabel#title {
    font-size: 22px;
    font-weight: 700;
    color: #ffffff;
}

QLabel#subtitle {
    font-size: 12px;
    color: #6c7086;
}

QLabel#sectionTitle {
    font-size: 14px;
    font-weight: 600;
    color: #cdd6f4;
    padding: 4px 0;
}

QFrame#card {
    background-color: #2a2a3d;
    border-radius: 12px;
    border: 1px solid #313244;
}

QTextEdit#taskInput {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 10px;
    padding: 12px;
    color: #cdd6f4;
    font-size: 14px;
    selection-background-color: #89b4fa;
}

QTextEdit#taskInput:focus {
    border: 1px solid #89b4fa;
}

QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: none;
    border-radius: 8px;
    padding: 10px 18px;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #45475a;
}

QPushButton:pressed {
    background-color: #585b70;
}

QPushButton#primary {
    background-color: #89b4fa;
    color: #11111b;
}

QPushButton#primary:hover {
    background-color: #a6c8ff;
}

QPushButton#primary:disabled {
    background-color: #45475a;
    color: #6c7086;
}

QPushButton#danger {
    background-color: #f38ba8;
    color: #11111b;
}

QPushButton#danger:hover {
    background-color: #f5a0b5;
}

QPushButton#ghost {
    background-color: transparent;
    border: 1px solid #45475a;
    color: #a6adc8;
}

QPushButton#ghost:hover {
    border: 1px solid #89b4fa;
    color: #89b4fa;
}

QLabel#logLabel {
    color: #a6adc8;
    padding: 2px 0;
}

QPlainTextEdit#log {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 10px;
    padding: 10px;
    color: #a6adc8;
    font-family: "Menlo", "Monaco", monospace;
    font-size: 12px;
}

QScrollBar:vertical {
    background: transparent;
    width: 8px;
}

QScrollBar::handle:vertical {
    background: #45475a;
    border-radius: 4px;
    min-height: 30px;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QLineEdit, QComboBox {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 8px;
    padding: 8px 10px;
    color: #cdd6f4;
}

QLineEdit:focus, QComboBox:focus {
    border: 1px solid #89b4fa;
}

QComboBox::drop-down {
    border: none;
}

QComboBox QAbstractItemView {
    background-color: #2a2a3d;
    border: 1px solid #313244;
    color: #cdd6f4;
    selection-background-color: #89b4fa;
    selection-color: #11111b;
}

QCheckBox {
    color: #cdd6f4;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #45475a;
    background: #181825;
}

QCheckBox::indicator:checked {
    background: #89b4fa;
    border: 1px solid #89b4fa;
}

QDialog {
    background-color: #1e1e2e;
}

QTabWidget::pane {
    border: 1px solid #313244;
    border-radius: 8px;
    top: -1px;
}

QTabBar::tab {
    background: transparent;
    color: #a6adc8;
    padding: 8px 16px;
    margin-right: 4px;
}

QTabBar::tab:selected {
    color: #89b4fa;
    border-bottom: 2px solid #89b4fa;
}

QStatusBar {
    color: #6c7086;
}

QSplitter::handle {
    background: #313244;
    width: 2px;
}
"""
