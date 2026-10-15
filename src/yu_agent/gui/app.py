"""Yu-Agent 图形化界面主窗口。"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..settings import get_saved_models, get_settings, switch_model
from .floating import FloatingBar
from .settings_dialog import SettingsDialog
from .theme import APP_STYLESHEET
from .worker import AgentWorker


class MainWindow(QMainWindow):
    """主窗口：需求输入 + 模型选择 + 实时截图预览 + 动作日志。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Yu-Agent — GUI 自动化智能体")
        self.resize(1100, 720)
        self._worker: Optional[AgentWorker] = None
        self._build_ui()
        self._reload_models()

        # 悬浮控制条（任务运行时置顶显示，便于随时停止）
        self.floating = FloatingBar()
        self.floating.stop_clicked.connect(self._stop_run)
        self.floating.show_main_clicked.connect(self._show_main)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_floating_if_idle)

        # 关闭窗口时：若线程还在收尾，最多等一会儿，超时则强制退出
        self._quitting = False
        self._hidden_for_run = False
        self._quit_watchdog = QTimer(self)
        self._quit_watchdog.setSingleShot(True)
        self._quit_watchdog.timeout.connect(self._force_quit)

        # 定时刷新截图预览
        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._preview_timer.start(1500)

    def _force_quit(self) -> None:
        """兜底：线程迟迟不结束也要让程序退得掉。"""
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ---- UI 构建 ----
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setSpacing(16)

        # 顶部标题栏
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Yu-Agent")
        title.setObjectName("title")
        subtitle = QLabel("基于视觉大模型的 GUI 自动化智能体 · 观察 → 决策 → 执行")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.settings_btn = QPushButton("⚙ 设置")
        self.settings_btn.setObjectName("ghost")
        self.settings_btn.clicked.connect(self._open_settings)
        header.addWidget(self.settings_btn)

        root_layout.addLayout(header)

        # 需求输入卡片
        input_card = QFrame()
        input_card.setObjectName("card")
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(16, 16, 16, 16)

        input_title = QLabel("描述你的需求")
        input_title.setObjectName("sectionTitle")
        input_layout.addWidget(input_title)

        self.task_input = QPlainTextEdit()
        self.task_input.setObjectName("taskInput")
        self.task_input.setPlaceholderText(
            "例如：打开浏览器，搜索「天气」…\n也可以直接和我聊天，比如「你好」"
        )
        self.task_input.setMaximumHeight(90)
        input_layout.addWidget(self.task_input)

        # 发送栏下方：模型选择 + 发送按钮
        btn_row = QHBoxLayout()

        model_label = QLabel("模型")
        model_label.setObjectName("subtitle")
        btn_row.addWidget(model_label)

        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(220)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        btn_row.addWidget(self.model_combo)

        btn_row.addStretch()

        self.run_btn = QPushButton("▶ 发送")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._toggle_run)
        btn_row.addWidget(self.run_btn)

        input_layout.addLayout(btn_row)
        root_layout.addWidget(input_card)

        # 中部分屏：左截图预览，右日志
        splitter = QSplitter(Qt.Horizontal)

        # 左：截图预览
        preview_panel = QFrame()
        preview_panel.setObjectName("card")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(12, 12, 12, 12)

        preview_header = QHBoxLayout()
        preview_title = QLabel("屏幕预览")
        preview_title.setObjectName("sectionTitle")
        preview_header.addWidget(preview_title)
        preview_header.addStretch()
        self.preview_status = QLabel("待开始")
        self.preview_status.setObjectName("subtitle")
        preview_header.addWidget(self.preview_status)
        preview_layout.addLayout(preview_header)

        self.screenshot_label = QLabel("点击「发送」后显示屏幕截图")
        self.screenshot_label.setAlignment(Qt.AlignCenter)
        self.screenshot_label.setMinimumSize(400, 300)
        self.screenshot_label.setStyleSheet(
            "color: #6c7086; background: #181825; border-radius: 8px;"
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none;")
        scroll.setWidget(self.screenshot_label)
        preview_layout.addWidget(scroll)

        splitter.addWidget(preview_panel)

        # 右：动作日志 + 子任务
        log_panel = QFrame()
        log_panel.setObjectName("card")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(12, 12, 12, 12)

        log_title = QLabel("对话与执行日志")
        log_title.setObjectName("sectionTitle")
        log_layout.addWidget(log_title)

        self.subtask_label = QLabel("")
        self.subtask_label.setObjectName("logLabel")
        self.subtask_label.setWordWrap(True)
        log_layout.addWidget(self.subtask_label)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)

        splitter.addWidget(log_panel)

        splitter.setSizes([520, 480])
        root_layout.addWidget(splitter, stretch=1)

        # 底部状态栏
        self.status = QLabel("就绪 · 请先在「设置」中配置模型 API")
        self.status.setObjectName("subtitle")
        root_layout.addWidget(self.status)

    # ---- 模型选择 ----
    def _reload_models(self) -> None:
        """刷新模型下拉框（读取已保存的模型历史）。"""
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        saved = get_saved_models()
        current = get_settings().get_model("default")

        if saved:
            for m in saved:
                label = m["name"]
                self.model_combo.addItem(label, m)
            # 选中当前模型
            for i in range(self.model_combo.count()):
                d = self.model_combo.itemData(i)
                if d and d.get("model") == current.model and d.get("base_url") == current.base_url:
                    self.model_combo.setCurrentIndex(i)
                    break
        else:
            self.model_combo.addItem("（未配置模型，请点设置）", None)
        self.model_combo.blockSignals(False)

    def _on_model_changed(self, index: int) -> None:
        data = self.model_combo.itemData(index)
        if data and data.get("model"):
            switch_model(data["model"], data["base_url"])
            self.status.setText(f"已切换模型：{data['model']}")

    # ---- 交互逻辑 ----
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self)
        if dlg.exec():
            # 设置保存后刷新模型下拉框
            self._reload_models()
            self._update_status_from_config()

    def _update_status_from_config(self) -> None:
        mc = get_settings().get_model("default")
        if mc.is_configured:
            self.status.setText(f"当前模型：{mc.model} @ {mc.base_url}")
        else:
            self.status.setText("未配置 API · 请到「设置」填写")

    def _toggle_run(self) -> None:
        if self._worker and self._worker.isRunning():
            self._stop_run()
            return

        task = self.task_input.toPlainText().strip()
        if not task:
            self.status.setText("请输入内容")
            return

        # 读取当前模型配置
        mc = get_settings().get_model("default")
        if not mc.is_configured:
            self.status.setText("请先在「设置」中配置模型 API")
            return

        self._worker = AgentWorker(task=task, model_config=mc)
        self._worker.event.connect(self._on_event)
        self._worker.reply.connect(self._on_reply)
        self._worker.finished.connect(self._on_finished)

        self.log_view.clear()
        self.subtask_label.clear()
        self.run_btn.setText("■ 停止")
        self.status.setText("思考中…")

        # 显示悬浮控制条（置顶，切到别的应用也能随时停止）
        self.floating.reset_for_run()
        self.floating.show_at_default()

        # 让开屏幕：主窗口隐藏，避免 ①挡住目标应用 ②成为前台应用导致
        # 感知模块把 Yu-Agent 自己排除掉、只剩程序坞图标可选（曾经就因此先点了屏幕右下角）
        self._hidden_for_run = True
        self.hide()

        self._worker.start()

    def _stop_run(self) -> None:
        """停止当前任务（主窗口按钮 / 悬浮条按钮共用）。"""
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.status.setText("正在停止…（会立刻停下，不再执行任何点击）")
            self.run_btn.setText("▶ 发送")
            self.floating.update_status("正在停止…（当前这一步跑完即停）")
        else:
            self.run_btn.setText("▶ 发送")

    def _show_main(self) -> None:
        """从悬浮条唤回主窗口。"""
        self.show()
        self.raise_()
        self.activateWindow()

    def _hide_floating_if_idle(self) -> None:
        if not (self._worker and self._worker.isRunning()):
            self.floating.hide()

    def _on_reply(self, reply_text: str) -> None:
        """AI 给用户的回复文字。"""
        ts = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] 💬 {reply_text}")
        self.status.setText(f"💬 {reply_text}")

    def _on_event(self, event: dict) -> None:
        etype = event.get("type")
        ts = time.strftime("%H:%M:%S")

        if etype == "start":
            self.log_view.appendPlainText(f"[{ts}] 🚀 开始任务：{event.get('task')}")
            self.floating.update_status("开始执行…")
            try:
                from ..common.logging import log_file_path

                p = log_file_path()
                if p:
                    self.log_view.appendPlainText(f"[{ts}] 📄 日志文件：{p}")
            except Exception:
                pass
        elif etype == "plan":
            subtasks = event.get("subtasks", [])
            lines = ["📋 任务拆分："]
            for i, s in enumerate(subtasks, 1):
                lines.append(f"   {i}. {s}")
            self.subtask_label.setText("\n".join(lines))
            self.log_view.appendPlainText(f"[{ts}] 📋 拆分为 {len(subtasks)} 个子任务")
            self.floating.update_status(f"共 {len(subtasks)} 个子任务")
        elif etype == "subtask":
            idx = event.get("index", 0)
            total = event.get("total", 0)
            name = event.get("name", "")
            self.log_view.appendPlainText(
                f"[{ts}] ➡️ 子任务 {idx + 1}/{total}：{name}"
            )
            self.floating.update_status(f"子任务 {idx + 1}/{total}：{name}")
        elif etype == "action":
            a = event.get("action")
            x, y = event.get("x"), event.get("y")
            reason = event.get("reason", "")
            coord = f" ({x},{y})" if x is not None else ""
            self.log_view.appendPlainText(f"[{ts}] 🎯 动作：{a}{coord}" + (f" — {reason}" if reason else ""))
            self.status.setText(f"执行动作：{a}{coord}")
            self.floating.update_status(
                f"{a}{coord}" + (f" — {reason}" if reason else ""),
                step=event.get("step"),
            )
        elif etype == "result":
            self.log_view.appendPlainText(f"[{ts}]   ✓ {event.get('result')}")
        elif etype == "debug":
            # 感知诊断信息（识别到多少可点元素）
            self.log_view.appendPlainText(f"[{ts}] 🔍 {event.get('message')}")
        elif etype == "notice":
            # 系统自动停止（防重复/防卡死）
            self.log_view.appendPlainText(f"[{ts}] 🛑 自动停止：{event.get('message')}")
            self.status.setText(f"🛑 {event.get('message')}")
            self.floating.update_status(f"🛑 自动停止")
        elif etype == "error":
            self.log_view.appendPlainText(f"[{ts}] ⚠️ 错误：{event.get('message')}")
            self.floating.update_status(f"⚠️ {event.get('message')}")
        elif etype == "safety":
            self.log_view.appendPlainText(f"[{ts}] 🛡️ 安全拦截：{event.get('message')}")
            self.floating.update_status(f"🛡️ {event.get('message')}")

    def _on_finished(self, state: dict) -> None:
        ts = time.strftime("%H:%M:%S")
        success = state.get("success", False)
        steps = state.get("steps", 0)
        reason = state.get("reason", "")
        auto_stopped = state.get("auto_stopped", False)

        if state.get("skipped"):
            # 纯对话，未调用 agent
            self.status.setText("💬 已回复")
            self.floating.hide()
        elif success:
            tag = "✅ 已自动停止" if auto_stopped else "✅ 任务完成"
            self.log_view.appendPlainText(f"[{ts}] {tag}（共 {steps} 步）")
            self.status.setText(f"{tag}：{reason}")
            self.floating.mark_finished(True, reason)
            self._hide_timer.start(10000)  # 成功 10 秒后自动隐藏
        else:
            self.log_view.appendPlainText(f"[{ts}] ❌ 任务结束（共 {steps} 步）：{reason}")
            self.status.setText(f"❌ {reason}")
            self.floating.mark_finished(False, reason)

        self.run_btn.setText("▶ 发送")

        # 任务结束，把让开的主窗口收回来
        if getattr(self, "_hidden_for_run", False) and not self._quitting:
            self._hidden_for_run = False
            self.show()
            self.raise_()
            self.activateWindow()

    def _refresh_preview(self) -> None:
        """周期性刷新屏幕预览（只在运行中显示）。"""
        if self._quitting:
            return
        if not (self._worker and self._worker.isRunning()):
            return
        try:
            from ..perception.screenshot import capture_screen

            img = capture_screen()
            img.thumbnail((520, 520))
            data = img.tobytes("raw", "RGB")
            qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
            self.screenshot_label.setPixmap(QPixmap.fromImage(qimg))
            self.preview_status.setText(f"{img.width}×{img.height}")
        except Exception as e:
            self.preview_status.setText(f"预览失败：{e}")

    def closeEvent(self, event) -> None:
        running = bool(self._worker and self._worker.isRunning())

        if running and not self._quitting:
            # 后台还有任务在跑：先请求停止，窗口立刻隐藏（用户马上感觉关掉了），
            # 等线程真正收尾后再退出；最多兜底 15 秒，保证一定退得掉。
            self._quitting = True
            self._worker.stop()
            try:
                self._worker.finished.connect(self._force_quit)
            except Exception:
                pass
            self._quit_watchdog.start(15000)
            self.hide()
            try:
                self.floating.hide()
            except Exception:
                pass
            event.ignore()
            return

        if self._quitting:
            # 已在收尾阶段，忽略重复关闭，等线程结束自动退出
            try:
                self.floating.hide()
            except Exception:
                pass
            event.ignore()
            return

        try:
            self.floating.close()
        except Exception:
            pass
        super().closeEvent(event)
        # 悬浮条是独立顶层窗口，主窗口关闭后需手动退出应用
        app = QApplication.instance()
        if app is not None:
            app.quit()


def run_gui() -> int:
    """启动 GUI。"""
    import os
    import sys

    from ..perception.a11y import set_excluded_pids

    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLESHEET)
    app.setApplicationName("Yu-Agent")
    # 悬浮条是独立顶层窗口，主窗口关闭时手动退出
    app.setQuitOnLastWindowClosed(False)

    # 让 agent 不要把自己的窗口当成可点击元素
    set_excluded_pids([os.getpid()])

    win = MainWindow()
    win.show()
    return app.exec()
