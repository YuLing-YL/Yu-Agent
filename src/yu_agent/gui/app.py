"""调试面板：把「模型看到什么 / 说了什么 / 点了哪」直接摊在界面上。

这不是产品界面，唯一目标是**让调参变快**，所以刻意做了三件事：
1. 左侧显示**模型实际收到的那张标注图**（不是当前屏幕的实时预览）
2. 右侧同时给出编号表、模型原始输出、执行日志——判断「标注错」还是「模型选错」
   不用再去翻 runs/xxx/trace.jsonl
3. 支持**单步执行**：跑一步、看清楚、再放行下一步

样式全部交给系统原生控件，只保留等宽字体等最低限度样式（见 style.py）。
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QInputDialog,
    QMainWindow,
    QMessageBox,
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
from .style import APP_STYLESHEET
from .worker import AgentWorker


class MainWindow(QMainWindow):
    """调试面板主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Yu-Agent 调试面板")
        self.resize(1280, 840)
        self._worker: Optional[AgentWorker] = None
        self._pixmap: Optional[QPixmap] = None
        self._run_dir = ""
        self._paused = False
        self._quitting = False
        self._hidden_for_run = False

        self._build_ui()
        self._reload_models()

        # 悬浮控制条：运行时可随时停止 / 唤回主窗口
        self.floating = FloatingBar()
        self.floating.stop_clicked.connect(self._stop_run)
        self.floating.show_main_clicked.connect(self._show_main)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_floating_if_idle)

        # 关闭窗口时：线程仍在收尾则最多等 15 秒
        self._quit_watchdog = QTimer(self)
        self._quit_watchdog.setSingleShot(True)
        self._quit_watchdog.timeout.connect(self._force_quit)

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 第 1 行：任务 + 运行控制
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        self.task_input = QPlainTextEdit()
        self.task_input.setPlaceholderText(
            "任务描述，例如：打开 Finder，进入 /Users/yuling/code/model_test，新建一个文件夹"
        )
        self.task_input.setFixedHeight(52)
        row1.addWidget(self.task_input, 1)

        self.run_btn = QPushButton("运行")
        self.run_btn.setMinimumSize(80, 32)
        self.run_btn.clicked.connect(self._start_run)
        row1.addWidget(self.run_btn, 0, Qt.AlignTop)

        self.next_btn = QPushButton("下一步")
        self.next_btn.setObjectName("nextButton")
        self.next_btn.setMinimumSize(90, 32)
        self.next_btn.setEnabled(False)
        self.next_btn.setToolTip("单步模式下，放行下一步（每步执行完会自动暂停）")
        self.next_btn.clicked.connect(self._next_step)
        row1.addWidget(self.next_btn, 0, Qt.AlignTop)

        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setMinimumSize(80, 32)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setToolTip(
            "暂停：当前这一步做完后停住，不会再开始新的点击；\n"
            "再点一次「继续」从暂停处恢复。要立刻中止请用「停止」"
        )
        self.pause_btn.clicked.connect(self._toggle_pause)
        row1.addWidget(self.pause_btn, 0, Qt.AlignTop)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("stopButton")
        self.stop_btn.setMinimumSize(80, 32)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_run)
        row1.addWidget(self.stop_btn, 0, Qt.AlignTop)

        root.addLayout(row1)

        # 第 2 行：配置项
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        row2.addWidget(QLabel("模型"))
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(200)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        row2.addWidget(self.model_combo)

        row2.addWidget(QLabel("标注"))
        self.som_mode_combo = QComboBox()
        self.som_mode_combo.addItem("编号标注", "marks")
        self.som_mode_combo.addItem("纯坐标", "coords")
        self.som_mode_combo.addItem("不标注", "off")
        self.som_mode_combo.setToolTip(
            "marks：在截图上画编号框，模型输出编号（默认）\n"
            "coords：不画框，模型直接给坐标，程序吸附到最近元素\n"
            "off：完全不识别元素，纯看图给坐标"
        )
        idx = self.som_mode_combo.findData(str(get_settings().agent.som_mode or "marks"))
        self.som_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.som_mode_combo.currentIndexChanged.connect(self._on_som_mode_changed)
        row2.addWidget(self.som_mode_combo)

        self.frontmost_only_cb = QCheckBox("只标当前窗口")
        self.frontmost_only_cb.setChecked(bool(get_settings().agent.som_frontmost_only))
        self.frontmost_only_cb.toggled.connect(self._on_frontmost_only_toggled)
        row2.addWidget(self.frontmost_only_cb)

        self.content_check_cb = QCheckBox("框校准")
        self.content_check_cb.setChecked(bool(get_settings().agent.som_content_check))
        self.content_check_cb.setToolTip(
            "用截图像素校准元素框：\n"
            "· 框比内容大 → 收缩到真实内容（列表行不再压住邻居）\n"
            "· 框里什么都没有 → 直接丢弃（不再出现罩在空白上的大红框）\n"
            "取消勾选可对比「AX 给什么框就画什么框」的原样。"
        )
        self.content_check_cb.toggled.connect(self._on_content_check_toggled)
        row2.addWidget(self.content_check_cb)

        self.step_cb = QCheckBox("单步执行")
        self.step_cb.setToolTip("每执行完一步就暂停，点「下一步」再继续。排查第几步出错时用。")
        row2.addWidget(self.step_cb)

        self.hide_cb = QCheckBox("运行时隐藏本窗口")
        self.hide_cb.setChecked(True)
        self.hide_cb.setToolTip(
            "勾选：运行时隐藏主窗口，避免挡住目标应用（截图里会带上本窗口）。\n"
            "取消：边跑边看屏幕。"
        )
        row2.addWidget(self.hide_cb)

        row2.addStretch()

        self.open_dir_btn = QPushButton("打开运行目录")
        self.open_dir_btn.setEnabled(False)
        self.open_dir_btn.setToolTip("runs/<时间戳>/：截图 + trace.jsonl")
        self.open_dir_btn.clicked.connect(self._open_run_dir)
        row2.addWidget(self.open_dir_btn)

        self.settings_btn = QPushButton("设置")
        self.settings_btn.clicked.connect(self._open_settings)
        row2.addWidget(self.settings_btn)

        root.addLayout(row2)

        # 主体：左 = 标注图，右 = 编号表 / 模型原始输出 / 日志
        splitter = QSplitter(Qt.Horizontal)

        left = QGroupBox("模型看到什么（标注图）")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 6, 6)
        left_layout.setSpacing(4)

        img_header = QHBoxLayout()
        self.image_info = QLabel("等待运行")
        img_header.addWidget(self.image_info)
        img_header.addStretch()
        left_layout.addLayout(img_header)

        self.image_scroll = QScrollArea()
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setAlignment(Qt.AlignCenter)
        self.image_label = QLabel("运行后这里显示送进模型的标注截图")
        self.image_label.setObjectName("imageView")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(400, 300)
        self.image_scroll.setWidget(self.image_label)
        left_layout.addWidget(self.image_scroll, 1)

        splitter.addWidget(left)

        right = QSplitter(Qt.Vertical)

        self.marks_view = self._make_text_panel(
            right, "编号表（模型指代这些编号）", "这里显示本步的元素编号 → 元素描述",
            height=200,
        )
        self.raw_view = self._make_text_panel(
            right, "模型原始输出", "这里显示模型这一轮返回的原文（可能是半截 JSON、多余解释）",
            height=170,
        )
        self.log_view = self._make_text_panel(
            right, "执行日志", "", height=200,
        )
        right.setSizes([220, 180, 220])
        splitter.addWidget(right)

        splitter.setSizes([640, 640])
        root.addWidget(splitter, 1)

        self.status = QLabel("就绪")
        root.addWidget(self.status)

    def _make_text_panel(
        self, parent_splitter: QSplitter, title: str, placeholder: str, height: int
    ) -> QPlainTextEdit:
        """右侧三块文本区：用原生分组框（自带边框和标题，边界清楚）。"""
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(2)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlaceholderText(placeholder)
        view.setMaximumBlockCount(3000)
        lay.addWidget(view, 1)
        box.setMinimumHeight(height)
        parent_splitter.addWidget(box)
        return view

    # ------------------------------------------------------------------ #
    # 模型选择
    # ------------------------------------------------------------------ #
    def _reload_models(self) -> None:
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        saved = get_saved_models()
        current = get_settings().get_model("default")

        if saved:
            for m in saved:
                self.model_combo.addItem(m["name"], m)
            for i in range(self.model_combo.count()):
                d = self.model_combo.itemData(i)
                if d and d.get("model") == current.model and d.get("base_url") == current.base_url:
                    self.model_combo.setCurrentIndex(i)
                    break
        else:
            self.model_combo.addItem("（未配置模型，点「设置」）", None)
        self.model_combo.blockSignals(False)

    def _on_model_changed(self, index: int) -> None:
        data = self.model_combo.itemData(index)
        if data and data.get("model"):
            switch_model(data["model"], data["base_url"])
            self.status.setText(f"已切换模型：{data['model']}")

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self)
        if dlg.exec():
            self._reload_models()
            mc = get_settings().get_model("default")
            self.status.setText(
                f"模型：{mc.model} @ {mc.base_url}" if mc.is_configured
                else "未配置 API"
            )

    def _on_som_mode_changed(self, _index: int) -> None:
        from ..settings import set_agent_option

        mode = str(self.som_mode_combo.currentData() or "marks")
        try:
            set_agent_option("som_mode", mode)
            self.status.setText(f"标注模式：{mode}")
        except Exception as e:
            self.status.setText(f"保存失败：{e}")

    def _on_frontmost_only_toggled(self, checked: bool) -> None:
        from ..settings import set_agent_option

        try:
            set_agent_option("som_frontmost_only", bool(checked))
            self.status.setText("只标当前窗口" if checked else "标注所有可见应用")
        except Exception as e:
            self.status.setText(f"保存失败：{e}")

    def _on_content_check_toggled(self, checked: bool) -> None:
        from ..settings import set_agent_option

        try:
            set_agent_option("som_content_check", bool(checked))
            self.status.setText(
                "元素框已按截图内容校准" if checked else "元素框按 AX 原始矩形绘制"
            )
        except Exception as e:
            self.status.setText(f"保存失败：{e}")

    # ------------------------------------------------------------------ #
    # 运行控制
    # ------------------------------------------------------------------ #
    def _start_run(self) -> None:
        if self._worker and self._worker.isRunning():
            self.status.setText("任务运行中；要停止请点「停止」")
            return

        task = self.task_input.toPlainText().strip()
        if not task:
            self.status.setText("请输入任务描述")
            return

        mc = get_settings().get_model("default")
        if not mc.is_configured:
            self.status.setText("请先在「设置」中配置模型 API")
            return

        self._worker = AgentWorker(
            task=task,
            model_config=mc,
            step_mode=self.step_cb.isChecked(),
        )
        self._worker.event.connect(self._on_event)
        self._worker.reply.connect(self._on_reply)
        self._worker.finished.connect(self._on_finished)
        # 人机往返：危险动作确认 / agent 提问（worker 线程阻塞等这两个弹窗）
        self._worker.approval_needed.connect(self._on_approval_needed)
        self._worker.question_asked.connect(self._on_question_asked)

        # 清空上一轮的面板
        self.log_view.clear()
        self.raw_view.clear()
        self.marks_view.clear()
        self.image_label.setText("等待第一步…")
        self.image_label.setPixmap(QPixmap())
        self._pixmap = None
        self._run_dir = ""
        self.image_info.setText("等待第一步")
        self.open_dir_btn.setEnabled(False)
        self._paused = False

        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("暂停")
        self.next_btn.setEnabled(False)
        self.status.setText("运行中…" + ("（单步）" if self.step_cb.isChecked() else ""))

        self.floating.reset_for_run()
        self.floating.show_at_default()

        # 隐藏主窗口：避免挡住目标应用（也避免成为前台应用干扰感知）
        if self.hide_cb.isChecked():
            self._hidden_for_run = True
            self.hide()

        self._worker.start()

    def _next_step(self) -> None:
        if not (self._worker and self._worker.isRunning()):
            return
        self.next_btn.setEnabled(False)
        self._paused = False
        self.status.setText("放行下一步…")
        self.floating.update_status("继续执行…")
        self._worker.next_step()

    def _stop_run(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.next_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.status.setText("正在停止…（不会再执行任何点击）")
            self.floating.update_status("正在停止…")
        else:
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)

    def _open_run_dir(self) -> None:
        if not self._run_dir:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._run_dir))

    def _show_main(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _hide_floating_if_idle(self) -> None:
        if not (self._worker and self._worker.isRunning()):
            self.floating.hide()

    def _force_quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def _on_reply(self, reply_text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] 回复：{reply_text}")
        self.status.setText(f"回复：{reply_text}")

    def _on_event(self, event: dict) -> None:
        etype = event.get("type")
        ts = time.strftime("%H:%M:%S")

        if etype == "start":
            self.log_view.appendPlainText(f"[{ts}] 开始任务：{event.get('task')}")
            self.floating.update_status("开始执行…")
        elif etype == "plan":
            subtasks = event.get("subtasks", [])
            self.log_view.appendPlainText(f"[{ts}] 计划：{len(subtasks)} 个子任务")
            for i, s in enumerate(subtasks, 1):
                self.log_view.appendPlainText(f"        {i}. {s}")
        elif etype == "subtask":
            idx = event.get("index", 0)
            total = event.get("total", 0)
            self.log_view.appendPlainText(
                f"[{ts}] 子任务 {idx + 1}/{total}：{event.get('name', '')}"
            )
            self.floating.update_status(f"子任务 {idx + 1}/{total}")
        elif etype == "action":
            x, y = event.get("x"), event.get("y")
            coord = f" ({x},{y})" if x is not None else ""
            act = event.get("action")
            if act == "skill":
                # 技能调用没有坐标，改成显示「技能名(参数)」，否则日志里只有一个 skill
                sk = event.get("skill") or "?"
                args = event.get("args") or {}
                shown = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
                coord = f" {sk}({shown})" if shown else f" {sk}"
            self.log_view.appendPlainText(
                f"[{ts}] 动作 {act}{coord} — {event.get('reason', '')}"
            )
            self.status.setText(f"执行 {act}{coord}")
            self.floating.update_status(
                f"{act}{coord}", step=event.get("step")
            )
        elif etype == "result":
            self.log_view.appendPlainText(f"[{ts}]   -> {event.get('result')}")
        elif etype == "memory":
            # 任务结束时长期记忆提炼了什么 —— 让人能直接看到"它记住了什么"
            how = "模型总结" if event.get("used_llm") else "模板总结"
            summary = event.get("summary") or ""
            self.log_view.appendPlainText(f"[{ts}] [记忆/{how}] {summary}")
            lesson = event.get("lesson") or ""
            if lesson:
                self.log_view.appendPlainText(f"           经验：{lesson}")
            steps = event.get("steps") or []
            if steps:
                self.log_view.appendPlainText(
                    "           可复用步骤：" + " → ".join(str(s) for s in steps[:6])
                )
        elif etype == "step_detail":
            self._on_step_detail(event, ts)
        elif etype == "paused":
            self._paused = True
            step = event.get("step")
            self.next_btn.setEnabled(True)
            self.status.setText(f"已暂停：第 {step} 步执行完毕，点「下一步」继续")
            self.log_view.appendPlainText(f"[{ts}] [暂停] 第 {step} 步完成，等待下一步")
            self.floating.update_status(f"已暂停（第 {step} 步）", step=step)
        elif etype == "verify":
            ok = event.get("ok")
            if ok is True:
                tag = "✅ 复核通过"
            elif ok is False:
                tag = "❌ 复核未通过"
            else:
                tag = "⚠️ 未能复核"
            reason = event.get("reason") or ""
            self.log_view.appendPlainText(f"[{ts}] [复核] {tag}：{reason}")
        elif etype == "stats":
            line = event.get("line") or ""
            self.log_view.appendPlainText(f"[{ts}] [用量] {line}")
        elif etype == "run_paused":
            self.pause_btn.setText("继续")
            self.status.setText(f"已暂停（第 {event.get('step')} 步之后）")
            self.log_view.appendPlainText(f"[{ts}] [暂停] 已停住，点「继续」恢复")
            self.floating.update_status("已暂停")
        elif etype == "run_resumed":
            self.pause_btn.setText("暂停")
            self.status.setText("已继续")
            self.log_view.appendPlainText(f"[{ts}] [继续] 从暂停处恢复")
        elif etype == "debug":
            self.log_view.appendPlainText(f"[{ts}] {event.get('message')}")
        elif etype == "notice":
            self.log_view.appendPlainText(f"[{ts}] [系统] {event.get('message')}")
            self.status.setText(f"[系统] {event.get('message')}")
        elif etype == "error":
            self.log_view.appendPlainText(f"[{ts}] [错误] {event.get('message')}")
            self.floating.update_status("执行出错")
        elif etype == "safety":
            self.log_view.appendPlainText(f"[{ts}] [安全拦截] {event.get('message')}")

    def _on_step_detail(self, event: dict, ts: str) -> None:
        """把这一步的标注图 / 编号表 / 模型原始输出摊到面板上。"""
        step = event.get("step")
        self.marks_view.setPlainText(event.get("marks_desc") or "（本步没有识别到元素）")
        self.raw_view.setPlainText(event.get("model_raw") or "（模型没有返回文本）")

        img_path = event.get("image") or ""
        if img_path:
            pm = QPixmap(img_path)
            if not pm.isNull():
                self._pixmap = pm
                self._apply_pixmap()
                self.image_info.setText(
                    f"第 {step} 步 · {event.get('marks_total', 0)} 个元素 · "
                    f"{pm.width()}x{pm.height()}"
                )
            else:
                self.image_info.setText(f"第 {step} 步 · 图片加载失败")

        rd = event.get("run_dir") or ""
        if rd and rd != self._run_dir:
            self._run_dir = rd
            self.open_dir_btn.setEnabled(True)

        mark = event.get("mark")
        coord = (
            f"({event.get('x')},{event.get('y')})"
            if event.get("x") is not None else ""
        )
        self.log_view.appendPlainText(
            f"[{ts}] 第 {step} 步：{event.get('action')} "
            + (f"#{mark} " if mark is not None else "")
            + coord
        )

    def _apply_pixmap(self) -> None:
        """按视口宽度等比缩放（缩放只为看全貌，编号细节仍可放大窗口看）。"""
        if self._pixmap is None:
            return
        vp = self.image_scroll.viewport().size()
        target = max(320, vp.width() - 8)
        if self._pixmap.width() <= target:
            scaled = self._pixmap
        else:
            scaled = self._pixmap.scaledToWidth(target, Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)
        self.image_label.resize(scaled.size())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_pixmap()

    # ---- 人机往返 ----
    def _toggle_pause(self) -> None:
        """暂停/继续（不打断正在进行的点击）。"""
        if not (self._worker and self._worker.isRunning()):
            return
        if str(self.pause_btn.text()) == "暂停":
            self._worker.pause()
        else:
            self._worker.resume()

    def _on_approval_needed(self, info: dict) -> None:
        """危险动作确认：删除/发送/支付这类不可逆操作必须人来点头。"""
        desc = info.get("description", "")
        level = info.get("level", "high")
        category = info.get("category", "")
        detail = info.get("detail", "")
        box = QMessageBox(self)
        box.setWindowTitle("需要确认")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"即将执行：{desc}")
        box.setInformativeText(
            f"风险等级：{level}（{category}）\n"
            f"命中：{detail}\n\n"
            f"这类操作不可逆，确认继续吗？"
        )
        yes = box.addButton("执行", QMessageBox.AcceptRole)
        box.addButton("跳过", QMessageBox.RejectRole)
        box.exec()
        ok = box.clickedButton() is yes
        self.log_view.appendPlainText(
            f"[{time.strftime('%H:%M:%S')}] [确认] {desc} → {'执行' if ok else '跳过'}"
        )
        if self._worker:
            self._worker.provide_approval(ok)

    def _on_question_asked(self, info: dict) -> None:
        """agent 主动提问：给它一个回答框，别让它瞎猜。"""
        question = info.get("question", "需要补充信息")
        options = info.get("options") or []
        if options:
            text, ok = QInputDialog.getItem(
                self, "agent 提问", question, [str(o) for o in options], 0, True
            )
        else:
            text, ok = QInputDialog.getText(self, "agent 提问", question)
        answer = str(text).strip() if ok else ""
        self.log_view.appendPlainText(
            f"[{time.strftime('%H:%M:%S')}] [提问] {question} → {answer or '（未回答）'}"
        )
        if self._worker:
            self._worker.provide_answer(answer)

    def _on_finished(self, state: dict) -> None:
        ts = time.strftime("%H:%M:%S")
        success = state.get("success", False)
        steps = state.get("steps", 0)
        reason = state.get("reason", "")

        # 状态栏/悬浮条只有一行，长文本会溢出；日志里保留完整内容
        short = reason if len(reason) <= 60 else reason[:57] + "…"

        if state.get("skipped"):
            self.status.setText("已回复（未操作屏幕）")
            self.floating.hide()
        elif success:
            tag = "自动停止（判定已完成）" if state.get("auto_stopped") else "任务完成"
            self.log_view.appendPlainText(f"[{ts}] {tag}：共 {steps} 步")
            # 「屏幕上有什么」这类任务的答案就是 reason，必须完整留在日志里
            if reason:
                for line in str(reason).splitlines():
                    self.log_view.appendPlainText(f"    {line}")
            self.status.setText(f"{tag}：{short}")
            self.floating.mark_finished(True, short)
            self._hide_timer.start(10000)
        else:
            self.log_view.appendPlainText(f"[{ts}] 结束（共 {steps} 步）：{reason}")
            self.status.setText(f"未完成：{short}")
            self.floating.mark_finished(False, short)

        # 复核结论与用量：这两项是「这次到底做成了没有、花了多少」的唯一凭据
        verified = state.get("verified")
        if verified is False:
            self.log_view.appendPlainText(
                f"[{ts}] [复核] 模型声称完成，但截图复核未通过：{state.get('verify_reason', '')}"
            )
        elif verified is None and not state.get("skipped"):
            self.log_view.appendPlainText(f"[{ts}] [复核] 本次未能复核（不影响任务结果）")
        usage = state.get("usage") or {}
        if usage.get("calls"):
            cost = usage.get("cost_usd")
            tail = f"，约 ${cost}" if cost is not None else ""
            self.log_view.appendPlainText(
                f"[{ts}] [用量] {usage['calls']} 次调用 / "
                f"{usage.get('total_tokens', 0)} tokens / "
                f"模型耗时 {usage.get('llm_latency_s', 0)}s{tail}"
            )
        self.status.setText(self.status.text() + f"（{usage.get('total_tokens', 0)} tokens）"
                            if usage.get("total_tokens") else self.status.text())

        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("暂停")
        self.next_btn.setEnabled(False)
        self._paused = False

        if getattr(self, "_hidden_for_run", False) and not self._quitting:
            self._hidden_for_run = False
            self.show()
            self.raise_()
            self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        running = bool(self._worker and self._worker.isRunning())

        if running and not self._quitting:
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
    app.setQuitOnLastWindowClosed(False)

    # 让 agent 不要把自己的窗口当成可点击元素
    set_excluded_pids([os.getpid()])

    win = MainWindow()
    win.show()
    return app.exec()
