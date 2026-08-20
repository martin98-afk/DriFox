# -*- coding: utf-8 -*-
"""声明式插件配置自动渲染卡（E1）。

由 PluginConfigSchema 驱动：text→LineEdit / password→PasswordLineEdit /
bool→SwitchButton，末尾统一保存按钮。保存后回显当前生效值
（空输入=清除→回默认，对齐 websearch 旧卡语义）。
注册方式：PluginManager 扫描 config_schema 后调
register_settings_card(..., make_card_class(plugin_name))，插件零 UI 代码。
"""

from __future__ import annotations

from typing import Dict

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    SettingCard,
    StrongBodyLabel,
    SwitchButton,
)

from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry


def _unified_font(size: int = 13):
    from app.utils.utils import get_unified_font

    return get_unified_font(size)


class _ConfigRow(SettingCard):
    """通用配置行：控件由调用方创建并加入右侧"""

    def __init__(self, title: str, content: str, control: QWidget, parent=None):
        super().__init__(FluentIcon.SETTING, title, content, parent)
        self.setFont(_unified_font())
        control.setFont(_unified_font())
        self.hBoxLayout.addWidget(control, 0, Qt.AlignRight)


class PluginConfigCard(QWidget):
    """schema 驱动的插件配置卡（无 schema 时渲染为空，不报错）"""

    def __init__(self, plugin_name: str, parent=None):
        super().__init__(parent)
        self._plugin_name = plugin_name
        self._rows: Dict[str, QWidget] = {}  # key → 输入控件
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.setFont(_unified_font())

        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return

        title = StrongBodyLabel(schema.title, self)
        title.setFont(_unified_font(14))
        layout.addWidget(title)

        for f in schema.fields:
            if f.type == "bool":
                switch = SwitchButton()
                row = _ConfigRow(f.label, f.description, switch)
                self._rows[f.key] = switch
            else:
                edit = PasswordLineEdit() if f.type == "password" else LineEdit()
                edit.setClearButtonEnabled(True)
                if f.placeholder:
                    edit.setPlaceholderText(f.placeholder)
                edit.setFixedWidth(320)
                row = _ConfigRow(f.label, f.description or f.placeholder, edit)
                self._rows[f.key] = edit
            layout.addWidget(row)

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

        self._echo()

    def _echo(self):
        """回显当前生效值（默认兜底可见）"""
        store = PluginConfigStore()
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return
        for f in schema.fields:
            control = self._rows.get(f.key)
            if control is None:
                continue
            val = store.get(self._plugin_name, f.key)
            if f.type == "bool":
                control.setChecked(bool(val))
            else:
                control.setText(str(val if val is not None else ""))

    def _save(self):
        """保存：空文本=清除（回默认）；保存后刷新回显"""
        store = PluginConfigStore()
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return
        values = {}
        for f in schema.fields:
            control = self._rows.get(f.key)
            if control is None:
                continue
            if f.type == "bool":
                values[f.key] = control.isChecked()
            else:
                values[f.key] = control.text().strip()
        store.set_values(self._plugin_name, values)
        self._echo()


def make_card_class(plugin_name: str) -> type:
    """生成绑定 plugin_name 的无参构造卡片类（register_settings_card 的 widget_class 约定）"""

    class _BoundConfigCard(PluginConfigCard):
        def __init__(self, parent=None):
            super().__init__(plugin_name, parent)

    _BoundConfigCard.__name__ = f"PluginConfigCard[{plugin_name}]"
    return _BoundConfigCard
