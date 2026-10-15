"""设置对话框：API 配置（base_url / api_key / 模型名）等。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..settings import (
    get_settings,
    load_user_config,
    reload_settings,
    save_user_config,
)


class SettingsDialog(QDialog):
    """设置窗口，用户在此填写模型 API 配置。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        self._build_ui()
        self._load_current()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        title = QLabel("模型 API 设置")
        title.setObjectName("title")
        layout.addWidget(title)

        hint = QLabel("使用 OpenAI 兼容接口，可接入 OpenAI / Gemini / 通义千问 / DeepSeek / Kimi / 本地 vLLM 等")
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(12)

        self.base_url_input = QLineEdit()
        self.base_url_input.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("Base URL", self.base_url_input)

        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("sk-...")
        self.api_key_input.setEchoMode(QLineEdit.Password)
        form.addRow("API Key", self.api_key_input)

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("gpt-4o")
        form.addRow("模型名称", self.model_input)

        layout.addLayout(form)

        # 常见服务快捷填充
        quick_label = QLabel("快捷选择：")
        quick_label.setObjectName("sectionTitle")
        layout.addWidget(quick_label)

        quick_row = QHBoxLayout()
        presets = [
            ("OpenAI", "https://api.openai.com/v1", "gpt-4o"),
            ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
            ("通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-vl-max"),
            ("Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
            ("本地 vLLM", "http://localhost:8000/v1", ""),
            ("本地 Ollama", "http://localhost:11434/v1", ""),
        ]
        for name, url, model in presets:
            btn = QPushButton(name)
            btn.setObjectName("ghost")
            btn.clicked.connect(lambda _, u=url, m=model: self._apply_preset(u, m))
            quick_row.addWidget(btn)
        layout.addLayout(quick_row)

        layout.addStretch()

        # 按钮
        btn_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setObjectName("ghost")
        self.test_btn.clicked.connect(self._test_connection)
        btn_row.addWidget(self.test_btn)

        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("ghost")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

    def _load_current(self) -> None:
        cfg = load_user_config()
        mc = cfg.get("model_config", {})
        self.base_url_input.setText(mc.get("base_url", ""))
        self.api_key_input.setText(mc.get("api_key", ""))
        self.model_input.setText(mc.get("model", ""))

    def _apply_preset(self, url: str, model: str) -> None:
        self.base_url_input.setText(url)
        if model:
            self.model_input.setText(model)

    def _collect(self) -> dict:
        return {
            "base_url": self.base_url_input.text().strip(),
            "api_key": self.api_key_input.text().strip(),
            "model": self.model_input.text().strip(),
        }

    def _save(self) -> None:
        cfg = load_user_config()
        cfg["model_config"] = self._collect()
        save_user_config(cfg)
        reload_settings()
        QMessageBox.information(self, "保存成功", "配置已保存并生效。")
        self.accept()

    def _test_connection(self) -> None:
        """用当前填写的配置测试是否能连通模型服务。"""
        from ..models.base import VLMRequest
        from ..models.registry import get_backend
        from ..settings import ModelConfig

        mc = ModelConfig(**self._collect())
        if not mc.api_key:
            QMessageBox.warning(self, "提示", "请先填写 API Key。")
            return

        backend = get_backend(mc, "openai")
        try:
            resp = backend.chat(
                VLMRequest(
                    system="你是测试助手。",
                    prompt="请只回复两个字：正常",
                    max_tokens=16,
                )
            )
            QMessageBox.information(
                self, "连接成功", f"模型已响应：{resp.text[:50]}"
            )
        except Exception as e:
            QMessageBox.critical(self, "连接失败", str(e))
