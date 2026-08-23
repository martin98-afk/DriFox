# -*- coding: utf-8 -*-
"""setup_ui 模块化收敛验证：五模块注册齐 + 顺序 + 加载"""

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

ORDER = ["title_bar", "chat_area", "system_cards", "input_card", "bottom_toolbar"]


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


def test_system_modules_registered(fresh_registry):
    """5 系统模块均注册到 UIPluginRegistry"""
    from app.widgets.modules.bottom_toolbar_module import BottomToolbarModule
    from app.widgets.modules.chat_area_module import ChatAreaModule
    from app.widgets.modules.input_card_module import InputCardModule
    from app.widgets.modules.system_cards_module import SystemCardsModule
    from app.widgets.modules.title_bar_module import TitleBarModule

    from app.main_widget import _SYSTEM_MODULE_ORDER, _register_system_ui_modules

    _register_system_ui_modules()
    assert list(_SYSTEM_MODULE_ORDER) == ORDER
    assert isinstance(fresh_registry.get_ui_module("chat_area"), ChatAreaModule)
    assert isinstance(fresh_registry.get_ui_module("title_bar"), TitleBarModule)
    assert isinstance(fresh_registry.get_ui_module("system_cards"), SystemCardsModule)
    assert isinstance(fresh_registry.get_ui_module("input_card"), InputCardModule)
    assert isinstance(fresh_registry.get_ui_module("bottom_toolbar"), BottomToolbarModule)


def test_system_module_priority_is_base(monkeypatch):
    """系统模块 priority=0；插件 priority>=100 覆盖"""
    from app.main_widget import _register_system_ui_modules

    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    _register_system_ui_modules()
    for mid in ("title_bar", "chat_area", "system_cards", "input_card", "bottom_toolbar"):
        slot = reg._ui_modules.get(mid, [])
        assert any(name == "system" and priority == 0 for name, priority, _f in slot), (
            f"{mid} 缺少 system priority=0 实现"
        )


def test_plugin_override_beats_system(monkeypatch):
    """插件 priority>=100 覆盖系统 priority=0；get_ui_module 返回插件实现"""
    from app.plugins.contracts.ui_module import UIModule
    from app.main_widget import _register_system_ui_modules

    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))

    class _PluginTitleBar(UIModule):
        module_id = "title_bar"

        def build(self, host):
            setattr(host, "_overridden_by", "demo")

    _register_system_ui_modules()
    reg.register_ui_module("title_bar", _PluginTitleBar, plugin_name="demo", priority=100)
    # 胜者 = plugin demo（priority=100 > system priority=0）
    winner = reg.get_ui_module("title_bar")
    assert isinstance(winner, _PluginTitleBar)


def test_unload_reverts_to_system(monkeypatch):
    """插件卸载后回退到系统实现"""
    from app.plugins.contracts.ui_module import UIModule
    from app.main_widget import _register_system_ui_modules

    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))

    class _PluginTitleBar(UIModule):
        module_id = "title_bar"

        def build(self, host):
            pass

    _register_system_ui_modules()
    reg.register_ui_module("title_bar", _PluginTitleBar, plugin_name="demo", priority=100)
    assert isinstance(reg.get_ui_module("title_bar"), _PluginTitleBar)
    reg.unload_plugin("demo")
    # 卸载后胜者回退系统
    winner = reg.get_ui_module("title_bar")
    from app.widgets.modules.title_bar_module import TitleBarModule

    assert isinstance(winner, TitleBarModule)