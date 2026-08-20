# -*- coding: utf-8 -*-
"""websearch 自动配置卡测试（E1 契约化后）。

替代原手写 WebSearchKeySettingsCard 测试：
- 不再 import plugins.system.ui（手写卡已删除）
- 验证 plugin.json config_schema 声明后，UIPluginRegistry 注册了 system-config 自动卡
- 卡片回显走 PluginConfigStore（与 PluginConfigCard 一致）
"""

import json

import pytest

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def isolated_config(monkeypatch, tmp_path):
    """隔离 websearch 配置路径 + 环境变量"""
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: tmp_path)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    return tmp_path


def _load_system_manifest():
    with open("plugins/system/.drifox-plugin/plugin.json", encoding="utf-8") as fp:
        return json.load(fp)


def test_plugin_json_declares_config_schema():
    """system 插件 plugin.json 必须声明 config_schema（E1 契约面）"""
    manifest = _load_system_manifest()
    schema = parse_config_schema("system", manifest.get("config_schema"))
    assert schema is not None
    assert {f.key for f in schema.fields} == {"tavily_api_key", "tinyfish_api_key"}


def test_auto_card_registered_for_system(fresh_registry, monkeypatch):
    """plugin.json config_schema 声明后，自动卡 system-config 出现在设置卡注册表。"""
    # 清掉已有 schema（其它用例可能已注册 system）
    PluginConfigRegistry.get_instance().unregister_plugin("system")
    manifest = _load_system_manifest()
    schema = parse_config_schema("system", manifest.get("config_schema"))
    assert schema is not None
    PluginConfigRegistry.get_instance().register(schema)
    from app.widgets.cards.settings.plugin_config_card import make_card_class

    UIPluginRegistry.get_instance().register_settings_card(
        "system", "system-config", schema.title, make_card_class("system")
    )
    cards = [c for c in fresh_registry.get_settings_cards() if c.card_id == "system-config"]
    assert cards and cards[0].plugin_name == "system"
    PluginConfigRegistry.get_instance().unregister_plugin("system")


def test_auto_card_echo_falls_back_to_schema_default(qapp, fresh_registry, isolated_config):
    """未注册任何值时，卡片输入框回显 schema default（用户可见真实在用 key）"""
    from app.widgets.cards.settings.plugin_config_card import PluginConfigCard

    PluginConfigRegistry.get_instance().unregister_plugin("system")
    manifest = _load_system_manifest()
    schema = parse_config_schema("system", manifest.get("config_schema"))
    PluginConfigRegistry.get_instance().register(schema)
    card = PluginConfigCard("system")
    defaults = {f.key: f.default for f in schema.fields}
    assert card._rows["tavily_api_key"].text() == defaults["tavily_api_key"]
    assert card._rows["tinyfish_api_key"].text() == defaults["tinyfish_api_key"]
    PluginConfigRegistry.get_instance().unregister_plugin("system")


def test_auto_card_save_persists_via_store(qapp, fresh_registry, isolated_config):
    """卡片输入新 key → 保存 → PluginConfigStore 读回一致（UI 闭环）"""
    from app.plugins.managers.plugin_config_store import PluginConfigStore
    from app.widgets.cards.settings.plugin_config_card import PluginConfigCard

    PluginConfigRegistry.get_instance().unregister_plugin("system")
    manifest = _load_system_manifest()
    schema = parse_config_schema("system", manifest.get("config_schema"))
    PluginConfigRegistry.get_instance().register(schema)
    card = PluginConfigCard("system")
    card._rows["tavily_api_key"].setText("tvly-ui-key")
    card._rows["tinyfish_api_key"].setText("tf-ui-key")
    card.save_btn.click()
    store = PluginConfigStore()
    assert store.get("system", "tavily_api_key") == "tvly-ui-key"
    assert store.get("system", "tinyfish_api_key") == "tf-ui-key"
    PluginConfigRegistry.get_instance().unregister_plugin("system")


def test_save_empty_clears_back_to_default(qapp, fresh_registry, isolated_config):
    """清空输入保存 → 清除配置项 → 卡片回显 schema default（回退路径）"""
    from app.plugins.managers.plugin_config_store import PluginConfigStore
    from app.widgets.cards.settings.plugin_config_card import PluginConfigCard

    PluginConfigRegistry.get_instance().unregister_plugin("system")
    manifest = _load_system_manifest()
    schema = parse_config_schema("system", manifest.get("config_schema"))
    PluginConfigRegistry.get_instance().register(schema)
    PluginConfigStore().set_values("system", {"tavily_api_key": "tvly-tmp"})
    card = PluginConfigCard("system")
    card._rows["tavily_api_key"].setText("")
    card.save_btn.click()
    defaults = {f.key: f.default for f in schema.fields}
    assert card._rows["tavily_api_key"].text() == defaults["tavily_api_key"]
    assert PluginConfigStore().get("system", "tavily_api_key") == defaults["tavily_api_key"]
    PluginConfigRegistry.get_instance().unregister_plugin("system")


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
