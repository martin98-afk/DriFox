# -*- coding: utf-8 -*-
"""声明式配置自动渲染卡（QApplication 离屏渲染）+ PluginManager 接线。"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.widgets.cards.settings.plugin_config_card import (
    PluginConfigCard,
    make_card_class,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def schema_env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    reg = PluginConfigRegistry.get_instance()
    reg.register(
        parse_config_schema(
            "plug-ui",
            {
                "title": "UI 测试",
                "fields": [
                    {"key": "name", "label": "名称", "type": "text", "default": "abc"},
                    {"key": "secret", "label": "密钥", "type": "password", "default": "sk-1"},
                    {"key": "on", "label": "开关", "type": "bool", "default": False},
                ],
            },
        )
    )
    yield tmp_path
    reg.unregister_plugin("plug-ui")


def test_card_renders_all_field_rows(qapp, schema_env):
    card = PluginConfigCard("plug-ui")
    # 三行字段控件 + 标题 + 保存按钮
    assert card._rows["name"] is not None
    assert card._rows["secret"] is not None
    assert card._rows["on"] is not None
    assert card.save_btn is not None


def test_card_echoes_effective_values(qapp, schema_env):
    card = PluginConfigCard("plug-ui")
    assert card._rows["name"].text() == "abc"  # 默认值回显
    assert card._rows["secret"].text() == "sk-1"
    assert card._rows["on"].isChecked() is False


def test_card_save_persists(qapp, schema_env):
    card = PluginConfigCard("plug-ui")
    card._rows["name"].setText("changed")
    card._save()
    assert PluginConfigStore().get("plug-ui", "name") == "changed"


def test_make_card_class_zero_arg_construction(qapp, schema_env):
    cls = make_card_class("plug-ui")
    widget = cls()  # register_settings_card 的 widget_class 约定：无参构造
    assert isinstance(widget, PluginConfigCard)
    assert widget._plugin_name == "plug-ui"


def test_card_without_schema_renders_empty(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    card = PluginConfigCard("never-registered")
    assert card._rows == {}


def test_plugin_manager_registers_config_schema(tmp_path, monkeypatch):
    """plugin.json 含 config_schema → 扫描后注册表可见 + 设置卡已注册"""
    import json

    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    plug_dir = tmp_path / "plug-cfg"
    (plug_dir / ".drifox-plugin").mkdir(parents=True)
    (plug_dir / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "plug-cfg",
                "version": "1.0.0",
                "config_schema": {
                    "title": "C 卡",
                    "fields": [{"key": "k", "label": "K", "type": "text"}],
                },
            }
        ),
        encoding="utf-8",
    )

    from app.plugins.managers.plugin_manager import PluginManager

    pm = PluginManager()
    pm._scan_one_plugin_dir(plug_dir, "user")

    reg = PluginConfigRegistry.get_instance()
    try:
        assert reg.get("plug-cfg") is not None
        ui = UIPluginRegistry.get_instance()
        cards = [c.card_id for c in ui.get_settings_cards()]
        assert "plug-cfg-config" in cards
    finally:
        # 清理：保留测试隔离
        reg.unregister_plugin("plug-cfg")
        # 清理自动卡注册（保持 UI registry 干净）
        UIPluginRegistry.get_instance()._settings_cards.pop("plug-cfg-config", None)
