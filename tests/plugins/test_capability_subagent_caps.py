# -*- coding: utf-8 -*-
"""P2 新增能力在能力池中的可见性与形状

插件声明 storage.db / model.config_for / config.write 后，ui 与 tool 两个
surface 都必须取到实现（catalog 登记 + 两套 provider 注册的联调守卫）。
"""

import pytest

from app.plugins.capabilities.providers.db_guard import PluginDBProxy

_NEW_CAPS = frozenset({"storage.db", "model.config_for", "config.write"})


@pytest.fixture(scope="module")
def registry():
    from app.plugins.capabilities.providers import tool_core, ui_core
    from app.plugins.capabilities.registry import CapabilityRegistry

    reg = CapabilityRegistry()
    ui_core.register(reg)
    tool_core.register(reg)
    yield reg
    reg.clear()


def test_ui_surface_exposes_new_capabilities(registry):
    pool = registry.build_pool(surface="ui", host=object(), plugin="demo", declared=_NEW_CAPS)

    assert isinstance(pool["storage.db"], PluginDBProxy)
    assert pool["storage.db"].table_prefix == "plugin_demo_"
    assert callable(pool["config.write"])
    assert callable(pool["model.config_for"])


def test_tool_surface_exposes_new_capabilities(registry, monkeypatch):
    pool = registry.build_pool(surface="tool", host=object(), plugin="demo", declared=_NEW_CAPS)

    assert isinstance(pool["storage.db"], PluginDBProxy)
    assert pool["storage.db"].table_prefix == "plugin_demo_"
    # tool 侧的模型解析依赖活跃窗口；无窗口时返回恒 None 的实现
    monkeypatch.setattr(
        "app.plugins.capabilities.resolve.active_main_widget", lambda: None
    )
    assert pool["model.config_for"]("gpt-4o") is None


def test_undeclared_capability_denied(registry):
    from app.plugins.capabilities.pool import CapabilityDenied

    pool = registry.build_pool(surface="ui", host=object(), plugin="demo", declared=frozenset())

    with pytest.raises(CapabilityDenied):
        _ = pool["storage.db"]
