# -*- coding: utf-8 -*-
"""UIComposition 装配器：按 module_ids 顺序构建模块，记录胜负者/失败"""

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.ui_composition import compose


class _FakeHost:
    pass


class _M1:
    def __init__(self):
        self.built = False

    def build(self, host):
        host.m1_built = True
        self.built = True

    def teardown(self, host):
        host.m1_built = False


class _M2:
    def __init__(self):
        pass

    def build(self, host):
        host.m2_built = True


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


class TestCompose:
    def test_compose_builds_in_order(self, fresh_registry):
        fresh_registry.register_ui_module("m1", _M1, plugin_name="system")
        fresh_registry.register_ui_module("m2", _M2, plugin_name="system")
        host = _FakeHost()
        report = compose(host, ["m1", "m2"], root_layout_factory=lambda h: None)
        assert host.m1_built and host.m2_built
        assert report["m1"] == "system"
        assert report["m2"] == "system"

    def test_compose_unknown_module_id_skipped(self, fresh_registry):
        fresh_registry.register_ui_module("m1", _M1, plugin_name="system")
        host = _FakeHost()
        report = compose(host, ["m1", "ghost"], root_layout_factory=lambda h: None)
        assert report.get("m1") == "system"
        assert report.get("ghost") is None  # 未注册 module 占位 None

    def test_compose_uses_winner(self, fresh_registry):
        class _Plugin:
            def build(self, host):
                host.m1_built = "plugin"

        fresh_registry.register_ui_module("m1", _M1, plugin_name="system", priority=0)
        fresh_registry.register_ui_module("m1", _Plugin, plugin_name="demo", priority=100)
        host = _FakeHost()
        compose(host, ["m1"], root_layout_factory=lambda h: None)
        assert host.m1_built == "plugin"

    def test_root_layout_factory_invoked(self, fresh_registry):
        """无根布局时调用 root_layout_factory 创建（用于测试或自定义宿主）"""
        fresh_registry.register_ui_module("m1", _M1, plugin_name="system")
        host = _FakeHost()
        called = []

        def _factory(h):
            called.append(h)
            h.layout = lambda: None

        compose(host, ["m1"], root_layout_factory=_factory)
        assert called == [host]

    def test_root_layout_factory_none_is_skipped(self, fresh_registry):
        """root_layout_factory 返回 None 不调用 host.layout()——用于主程序已有根布局场景"""
        fresh_registry.register_ui_module("m1", _M1, plugin_name="system")
        host = _FakeHost()
        host.layout = lambda: None  # 已就绪
        compose(host, ["m1"], root_layout_factory=lambda h: None)
        assert host.m1_built
