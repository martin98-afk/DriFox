# -*- coding: utf-8 -*-
r"""BottomToolbarModule：工具栏条/模型按钮/capsule/光晕——属性契约 + compose 端到端

契约集提取命令（段内 self.x = 赋值全集）：
python -X utf8 -c "import re; lines=open('app/main_widget.py',encoding='utf-8').read().split(chr(10)); pat=re.compile(r'self\.([\w]+)\s*(?::[\w\[\], ]+)?\s*='); attrs=[pat.match(l.strip()).group(1) for l in lines[3416:3450] if pat.match(l.strip())]; print(chr(10).join(sorted(set(attrs))))"
"""

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


def test_bottom_toolbar_module_id():
    from app.widgets.modules.bottom_toolbar_module import BottomToolbarModule

    assert BottomToolbarModule.module_id == "bottom_toolbar"


def test_compose_builds_bottom_toolbar(fresh_registry, qapp):
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    from app.widgets.modules.bottom_toolbar_module import BottomToolbarModule
    from app.widgets.ui_composition import compose

    fresh_registry.register_ui_module("bottom_toolbar", BottomToolbarModule, plugin_name="system")

    class _Host(QWidget):
        pass

    host = _Host()
    # 预置 build 读取的 host 属性（主程序在 input_card 段 / 其他方法已创建）
    host._bottom_input_container = QWidget(host)
    QVBoxLayout(host._bottom_input_container)
    host._input_card = QWidget(host)
    host._input_card_wrapper = QWidget(host)
    host.balance_display = QWidget(host)
    host.coding_plan_ring = QWidget(host)
    host.context_usage_ring = QWidget(host)
    # host 方法桩（build 尾调用 / lambda 连接，真实 MainWidget 提供）
    for _m in (
        "_toggle_model_selector_card",
        "_toggle_model_config_card",
        "_get_model_btn_text_style",
        "_cycle_effort_level",
        "_get_settings_effort_style",
        "_toggle_tool_control_card",
        "_on_tool_restore",
        "_show_soul_memory",
        "_toggle_history_card",
        "_create_new_session",
        "_build_plugin_input_buttons",
        "_apply_bottom_input_stack_style",
        "_position_bottom_toolbar",
        "_refresh_tool_toggle_btn",
    ):
        setattr(host, _m, lambda *a, **k: "")

    report = compose(host, ["bottom_toolbar"], root_layout_factory=lambda h: None)

    assert report["bottom_toolbar"] == "system"
    _CONTRACT_ATTRS = [
        "_bottom_toolbar_strip",
        "_model_btn_container",
        "current_model_btn",
        "settings_btn",
        "effort_btn",
        "_tool_toggle_btn",
        "_toolbar_capsule",
        "memory_btn",
        "history_btn",
        "new_session_btn",
        "_input_glow_underlay",
        "_model_btn_icon",
        "_model_btn_text",
        "_model_sep_name",
        "_model_sep_usage",
        "_settings_btn_icon",
        "_settings_effort_label",
        "_tool_danger_label",
        "_tool_safe_label",
        "_tool_restore_btn",
        "_bottom_toolbar_shadow",
        "_input_card_primary_shadow",
        "_input_card_ambient_shadow",
        "_current_provider_name",
        "_current_model_name",
        "_user_manually_selected_model",
        "_input_card_focused",
        "_input_area_collapsed",
        "_plugin_input_buttons",
    ]
    for _attr in _CONTRACT_ATTRS:
        assert hasattr(host, _attr), f"missing host attribute: {_attr}"
