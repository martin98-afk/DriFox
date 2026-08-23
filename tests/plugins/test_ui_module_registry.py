# -*- coding: utf-8 -*-
"""UIModule 槽注册：factory 延迟构造 + priority 覆盖 + unload 清理"""

import pytest

from app.plugins.contracts.ui_module import UIModule
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


class _FakeHost:
    pass


class _SysChatArea(UIModule):
    module_id = "chat_area"

    def build(self, host):
        host.built_by = "system"


class _PluginChatArea(UIModule):
    module_id = "chat_area"

    def build(self, host):
        host.built_by = "plugin"


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


class TestUIModuleRegistry:
    def test_factory_not_invoked_on_register(self, fresh_registry):
        calls = []

        def factory():
            calls.append(1)
            return _SysChatArea()

        fresh_registry.register_ui_module("chat_area", factory, plugin_name="system")
        assert calls == []  # 注册不构造
        fresh_registry.get_ui_module("chat_area")
        assert calls == [1]

    def test_plugin_overrides_system_by_priority(self, fresh_registry):
        fresh_registry.register_ui_module("chat_area", _SysChatArea, plugin_name="system", priority=0)
        fresh_registry.register_ui_module("chat_area", _PluginChatArea, plugin_name="demo", priority=100)
        host = _FakeHost()
        fresh_registry.get_ui_module("chat_area").build(host)
        assert host.built_by == "plugin"

    def test_same_priority_last_wins(self, fresh_registry):
        fresh_registry.register_ui_module("chat_area", _SysChatArea, plugin_name="a", priority=10)
        fresh_registry.register_ui_module("chat_area", _PluginChatArea, plugin_name="b", priority=10)
        host = _FakeHost()
        fresh_registry.get_ui_module("chat_area").build(host)
        assert host.built_by == "plugin"

    def test_unload_restores_system(self, fresh_registry):
        fresh_registry.register_ui_module("chat_area", _SysChatArea, plugin_name="system", priority=0)
        fresh_registry.register_ui_module("chat_area", _PluginChatArea, plugin_name="demo", priority=100)
        fresh_registry.unload_plugin("demo")
        host = _FakeHost()
        fresh_registry.get_ui_module("chat_area").build(host)
        assert host.built_by == "system"

    def test_unload_system_module_removes_slot(self, fresh_registry):
        fresh_registry.register_ui_module("chat_area", _SysChatArea, plugin_name="system")
        fresh_registry.unload_plugin("system")
        assert fresh_registry.get_ui_module("chat_area") is None

    def test_list_ids(self, fresh_registry):
        fresh_registry.register_ui_module("chat_area", _SysChatArea, plugin_name="system")
        fresh_registry.register_ui_module("title_bar", _SysChatArea, plugin_name="system")
        assert fresh_registry.list_ui_module_ids() == ["chat_area", "title_bar"]
