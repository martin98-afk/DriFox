# -*- coding: utf-8 -*-
"""声明式插件配置自动渲染卡（E1）。

折叠卡 + 字段独立保存（重写）：
- 一张 ExpandSettingCard，标题 = schema.title，副标题 = 字段数概览
- 展开后内部用表单：每行 label + LineEdit（password → PasswordLineEdit）
- bool 字段以 SwitchButton 形式挂到 header 右侧（快速切换）
- 每个字段 editingFinished / checkedChanged 触发即时保存，无需底部「保存配置」按钮
- 字体大小 / 字族由 _apply_runtime_ui_settings → apply_font_size_to_widget
  递归处理（保持与全站一致；不再在控件构造期 setFont，避免 pointSize/pixelSize
  错位导致的"字体未应用系统字号 / 密码字体过大"问题）
- 回显当前生效值（空输入=清除→回默认，对齐 websearch 旧卡语义）

注册方式：PluginManager 扫描 config_schema 后调
register_settings_card(..., make_card_class(plugin_name))，插件零 UI 代码。
"""

from __future__ import annotations

from typing import Dict

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget
from qfluentwidgets import (
    ExpandSettingCard,
    FluentIcon,
    LineEdit,
    PasswordLineEdit,
    SwitchButton,
    isDarkTheme,
)

from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry


class _FieldRow(QWidget):
    """字段行：左侧 label，右侧输入控件。"""

    def __init__(self, label_text: str, control: QWidget, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)

        self._label = QLabel(label_text, self)
        self._label.setObjectName("fieldLabel")
        self._label.setMinimumWidth(120)
        layout.addWidget(self._label, 0, Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(control, 1, Qt.AlignRight)


class PluginConfigCard(ExpandSettingCard):
    """schema 驱动的折叠式插件配置卡（无 schema 时渲染为空，不报错）。

    继承 ExpandSettingCard 复用标题/折叠/header 样式；通过 viewLayout
    把行添加到内部 view；bool 字段挂到 header 右侧作为 SwitchButton。
    """

    def __init__(self, plugin_name: str, parent=None):
        self._plugin_name = plugin_name
        self._rows: Dict[str, QWidget] = {}  # key → 输入控件（兼容旧测试）
        self._bool_switches: Dict[str, SwitchButton] = {}  # key → switch 实例
        # 文本字段回显基线：记录最近一次 setText 的值，用于判断"内容是否真正变化"
        self._echoed: Dict[str, str] = {}

        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        title_text = schema.title if schema else "插件配置"
        # 副标题：字段数概览（无 schema 时给空串，避免占位）
        if schema and schema.fields:
            content_text = f"共 {len(schema.fields)} 项配置，点开展开"
        else:
            content_text = ""

        icon = self._resolve_plugin_icon() or FluentIcon.SETTING
        super().__init__(icon, title_text, content_text, parent)
        # viewLayout 边距收紧，让多行表单更紧凑
        self.viewLayout.setContentsMargins(48, 8, 48, 8)
        self.viewLayout.setSpacing(4)

        if schema is None:
            return

        for f in schema.fields:
            if f.type == "bool":
                switch = SwitchButton(self.card)
                self._bool_switches[f.key] = switch
                self._rows[f.key] = switch
                self.card.addWidget(switch)
                switch.setOnText(f.label)
                switch.setOffText(f.label)
                switch.checkedChanged.connect(lambda _checked, _k=f.key: self._on_field_changed(_k))
            else:
                edit = PasswordLineEdit() if f.type == "password" else LineEdit()
                edit.setClearButtonEnabled(True)
                if f.placeholder:
                    edit.setPlaceholderText(f.placeholder)
                row = _FieldRow(f.label, edit, self.view)
                self._rows[f.key] = edit
                edit.editingFinished.connect(lambda _k=f.key: self._on_field_changed(_k))
                self.viewLayout.addWidget(row)

        self._echo()

    def _resolve_plugin_icon(self):
        """读取插件自身 icon（按当前主题取 light/dark），失败回退 None。

        对齐 tab_panel._get_plugin_icon：经 PluginManager 拿 icon_config，
        主题图标缺失/读取失败时静默回退 FluentIcon.SETTING。
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                return None
            plugin = pm.get_plugin(self._plugin_name)
            icon_config = getattr(plugin, "icon_config", None) if plugin else None
            if not icon_config:
                return None
            icon_path = icon_config.get("dark" if isDarkTheme() else "light")
            return QIcon(str(icon_path)) if icon_path else None
        except Exception:
            return None

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
                text = str(val if val is not None else "")
                control.setText(text)
                self._echoed[f.key] = text

    def _on_field_changed(self, key: str):
        """单字段即时保存：空文本=清除（回默认），保存后刷新回显。

        用 editingFinished / checkedChanged 触发，自动持久化，无需底部"保存配置"按钮。
        仅当内容真正变化时才写盘：editingFinished 在聚焦→失焦（未修改）时
        也会触发，若此时无脑写回输入框回显值，会覆盖用户手动编辑 config.json
        或其他实例的修改（双实例共享配置场景）。
        """
        store = PluginConfigStore()
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return
        target_field = next((f for f in schema.fields if f.key == key), None)
        if target_field is None:
            return
        control = self._rows.get(key)
        if control is None:
            return
        if target_field.type == "bool":
            value: object = control.isChecked()
        else:
            value = control.text().strip()
            # 无变化 → 跳过写盘：editingFinished 在聚焦→失焦（未修改）时也会触发，
            # 若输入框内容与回显基线一致（用户没真正编辑），写回会覆盖
            # 外部/其他实例对 config.json 的手动修改（双实例共享配置场景）。
            if value == self._echoed.get(key, ""):
                return
        store.set_values(self._plugin_name, {key: value})
        # 文本字段保存后回显（清除 → 默认值显式可见）；bool 无需回显
        if target_field.type != "bool":
            self._echo_field(key)

    def _echo_field(self, key: str):
        """单字段回显（仅文本字段，避免循环触发）"""
        store = PluginConfigStore()
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return
        target_field = next((f for f in schema.fields if f.key == key), None)
        if target_field is None or target_field.type == "bool":
            return
        control = self._rows.get(key)
        if control is None:
            return
        val = store.get(self._plugin_name, key)
        # 阻断 editingFinished 循环：临时 disconnect
        try:
            control.editingFinished.disconnect()
        except TypeError, RuntimeError:
            pass
        text = str(val if val is not None else "")
        control.setText(text)
        self._echoed[key] = text
        control.editingFinished.connect(lambda _k=key: self._on_field_changed(_k))


def make_card_class(plugin_name: str) -> type:
    """生成绑定 plugin_name 的无参构造卡片类（register_settings_card 的 widget_class 约定）"""

    class _BoundConfigCard(PluginConfigCard):
        def __init__(self, parent=None):
            super().__init__(plugin_name, parent)

    _BoundConfigCard.__name__ = f"PluginConfigCard[{plugin_name}]"
    return _BoundConfigCard
