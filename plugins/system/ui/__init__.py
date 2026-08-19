# -*- coding: utf-8 -*-
"""system 插件 UI 组件 — websearch 搜索 API key 配置卡（Phase D 设置卡片扩展点）。

插件自包含：配置读写走 tools/web_tools.py 的 get/set_api_key_config
（用户数据目录 tools/web_search_keys.json），主程序零改动。
"""

from PyQt5.QtWidgets import QLineEdit, QPushButton, QVBoxLayout, QWidget

from plugins.system.tools.web_tools import get_api_key_config, set_api_key_config


class WebSearchKeySettingsCard(QWidget):
    """websearch 两个 API key（Tavily / TinyFish）配置卡

    注册到设置面板「插件设置」分区（LLMSettingsCard 末尾），
    输入后点保存 → set_api_key_config 持久化；打开时回显当前配置。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_current()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.tavily_edit = QLineEdit(self)
        self.tavily_edit.setPlaceholderText("Tavily API Key（TAVILY_API_KEY）")
        self.tinyfish_edit = QLineEdit(self)
        self.tinyfish_edit.setPlaceholderText("TinyFish API Key（TINYFISH_API_KEY）")

        self.save_btn = QPushButton("保存", self)
        self.save_btn.clicked.connect(self._save)

        layout.addWidget(self.tavily_edit)
        layout.addWidget(self.tinyfish_edit)
        layout.addWidget(self.save_btn)

    def _load_current(self):
        """打开时回显当前配置（环境变量覆盖时显示配置值，空串=未注册）"""
        cfg = get_api_key_config()
        self.tavily_edit.setText(cfg["tavily_api_key"])
        self.tinyfish_edit.setText(cfg["tinyfish_api_key"])

    def _save(self):
        set_api_key_config(
            tavily_api_key=self.tavily_edit.text().strip(),
            tinyfish_api_key=self.tinyfish_edit.text().strip(),
        )


def register_ui(registry):
    """system 插件 UI 注册入口（被 UIPluginRegistry.load_plugin 调用）

    Phase D 设置卡片扩展点：注册到设置面板插件分区。
    """
    registry.register_settings_card(
        "system", "websearch-keys", "网页搜索 API Key", WebSearchKeySettingsCard
    )
