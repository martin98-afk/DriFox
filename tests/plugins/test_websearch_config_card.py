# -*- coding: utf-8 -*-
"""websearch 配置设置卡测试：注册到设置面板插件分区 + 保存闭环

链路（与真实 UI 一致）：
- plugins/system/ui/__init__.py register_ui → UIPluginRegistry.load_plugin("system")
  → get_settings_cards() 含 websearch-keys
- 卡片实例化：输入两个 key → 保存 → set_api_key_config 持久化 → 读回
"""

import pytest

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


def _system_ui_path():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent.parent / "plugins" / "system"


def test_register_ui_loads_settings_card(fresh_registry):
    """load_plugin("system") → 设置卡片注册到插件分区"""
    assert fresh_registry.load_plugin("system", _system_ui_path()) is True
    cards = fresh_registry.get_settings_cards()
    assert [c.card_id for c in cards] == ["websearch-keys"]
    assert cards[0].title == "网页搜索 API Key"
    assert cards[0].plugin_name == "system"


def test_settings_card_save_flow(qtbot, fresh_registry, isolated_config):
    """卡片输入两个 key → 保存 → 配置持久化 → 读回（UI 闭环）"""
    from plugins.system.ui import WebSearchKeySettingsCard

    card = WebSearchKeySettingsCard()
    qtbot.addWidget(card)
    card.tavily_edit.setText("tvly-ui-key")
    card.tinyfish_edit.setText("tf-ui-key")
    card.save_btn.click()

    from plugins.system.tools.web_tools import get_api_key_config

    cfg = get_api_key_config()
    assert cfg == {"tavily_api_key": "tvly-ui-key", "tinyfish_api_key": "tf-ui-key"}


def test_settings_card_loads_existing(qtbot, fresh_registry, isolated_config):
    """已注册配置 → 打开卡片回显"""
    from plugins.system.tools.web_tools import set_api_key_config
    from plugins.system.ui import WebSearchKeySettingsCard

    set_api_key_config(tavily_api_key="tvly-existing", tinyfish_api_key="tf-existing")
    card = WebSearchKeySettingsCard()
    qtbot.addWidget(card)
    assert card.tavily_edit.text() == "tvly-existing"
    assert card.tinyfish_edit.text() == "tf-existing"


def test_settings_card_empty_default(qtbot, fresh_registry, isolated_config):
    """未注册 → 输入框空（回退默认值路径，UI 不显示内置 key）"""
    from plugins.system.ui import WebSearchKeySettingsCard

    card = WebSearchKeySettingsCard()
    qtbot.addWidget(card)
    assert card.tavily_edit.text() == ""
    assert card.tinyfish_edit.text() == ""


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
