# -*- coding: utf-8 -*-
"""system 插件 UI 组件 — websearch 搜索 API key 配置卡（Phase D 设置卡片扩展点）。

插件自包含：配置读写走 tools/web_tools.py 的 get/set_api_key_config
（用户数据目录 tools/web_search_keys.json），主程序零改动。
样式对齐设置面板：qfluentwidgets SettingCard 输入行 + 系统字体 + 当前生效 key 回显。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, LineEdit, PrimaryPushButton, SettingCard, StrongBodyLabel

from plugins.system.tools.web_tools import (
    _DEFAULT_TAVILY_KEY,
    _DEFAULT_TINYFISH_KEY,
    get_api_key_config,
    set_api_key_config,
)


def _unified_font(size: int = 13):
    """系统统一字体（设置面板全局字体，避免默认 Qt 字体）"""
    from app.utils.utils import get_unified_font

    return get_unified_font(size)


class _KeyInputRow(SettingCard):
    """单个搜索服务 key 输入行（对齐设置面板 SettingCard 风格）"""

    def __init__(self, icon, title, content, parent=None):
        super().__init__(icon, title, content, parent)
        self.setFont(_unified_font())
        self.line_edit = LineEdit(self)
        self.line_edit.setFixedWidth(320)
        self.line_edit.setClearButtonEnabled(True)
        self.hBoxLayout.addWidget(self.line_edit, 0, Qt.AlignRight)


class WebSearchKeySettingsCard(QWidget):
    """websearch 两个 API key（Tavily / TinyFish）配置卡，分组在一起

    结构：标题 + 两行输入（SettingCard 风格）+ 保存按钮。
    输入框回显**当前生效 key**（配置优先，未配置显示内置默认），
    保存后持久化；清空输入再保存 → 回退内置默认。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_current()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.setFont(_unified_font())

        # 标题
        title = StrongBodyLabel("网页搜索 API Key", self)
        title.setFont(_unified_font(14))
        layout.addWidget(title)

        # 两个输入行（分组在一起）
        self._tavily_row = _KeyInputRow(FluentIcon.GLOBE, "Tavily 搜索", "TAVILY_API_KEY", self)
        self._tinyfish_row = _KeyInputRow(FluentIcon.SEARCH, "TinyFish 搜索", "TINYFISH_API_KEY", self)
        layout.addWidget(self._tavily_row)
        layout.addWidget(self._tinyfish_row)

        # 保存按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.save_btn = PrimaryPushButton("保存配置", self)
        from app.widgets.cards.settings.llm_settings_card import ButtonStyles

        self.save_btn.setStyleSheet(ButtonStyles.primary_action())
        self.save_btn.setFixedWidth(120)
        self.save_btn.setFont(_unified_font())
        self.save_btn.clicked.connect(self._save)
        btn_row.addWidget(self.save_btn)
        layout.addLayout(btn_row)

    def _current_keys(self) -> dict:
        """当前生效 key：配置优先，未配置显示内置默认（用户可见真实在用 key）"""
        cfg = get_api_key_config()
        return {
            "tavily_api_key": cfg["tavily_api_key"] or _DEFAULT_TAVILY_KEY,
            "tinyfish_api_key": cfg["tinyfish_api_key"] or _DEFAULT_TINYFISH_KEY,
        }

    def _load_current(self):
        """打开时回显当前生效 key（含内置默认）"""
        keys = self._current_keys()
        self._tavily_row.line_edit.setText(keys["tavily_api_key"])
        self._tinyfish_row.line_edit.setText(keys["tinyfish_api_key"])

    def _save(self):
        """保存：写入配置；输入为空串 → 清除配置项（回退内置默认）"""
        set_api_key_config(
            tavily_api_key=self._tavily_row.line_edit.text().strip(),
            tinyfish_api_key=self._tinyfish_row.line_edit.text().strip(),
        )
        # 保存后刷新显示（空输入 → 显示内置默认）
        self._load_current()


def register_ui(registry):
    """system 插件 UI 注册入口（被 UIPluginRegistry.load_plugin 调用）

    Phase D 设置卡片扩展点：注册到设置面板插件分区。
    """
    registry.register_settings_card(
        "system", "websearch-keys", "网页搜索 API Key", WebSearchKeySettingsCard
    )
