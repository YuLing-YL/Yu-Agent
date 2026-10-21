"""调试面板的最小样式。

刻意只保留「看得清」必需的三样东西，其余全部交给系统原生控件：
1. 等宽字体（日志 / 编号表 / 模型原始输出要对齐才好读）
2. 图片区背景（否则截图透明处会和窗口混在一起）
3. 停止按钮醒目（这是安全相关的，不能靠猜）

不做圆角、渐变、自定义配色、字体层级——调试面板不是产品界面，
每多一行 QSS 就多一处要跟着改的东西。
"""
from __future__ import annotations

APP_STYLESHEET = """
QPlainTextEdit, QTextEdit {
    font-family: "Menlo", "Monaco", "Courier New", monospace;
    font-size: 12px;
}

QLabel#imageView {
    background: #2b2b2b;
    color: #dddddd;
    border: 1px solid #555555;
}

QPushButton#stopButton {
    background: #c0392b;
    color: #ffffff;
    font-weight: bold;
    padding: 6px 14px;
}

QPushButton#stopButton:hover {
    background: #d4493b;
}

QPushButton#nextButton {
    font-weight: bold;
    padding: 6px 14px;
}

QWidget#floatBar {
    background: #f0f0f0;
    border: 1px solid #888888;
}
"""
