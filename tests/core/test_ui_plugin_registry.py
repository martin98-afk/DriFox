# -*- coding: utf-8 -*-
"""UIPluginRegistry 单元测试"""
import pytest
from app.core.ui_plugin_registry import (
    UIPluginRegistry,
    ContentRendererInfo,
    MessageFactoryInfo,
    FloatingCardInfo,
)


def test_registry_singleton():
    """UIPluginRegistry 必须是单例"""
    a = UIPluginRegistry.get_instance()
    b = UIPluginRegistry.get_instance()
    assert a is b


def test_initial_state_is_empty():
    """初始化后所有注册表为空"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()  # 测试隔离
    assert reg.get_content_renderer("any") is None
    assert reg.get_message_factories() == []
    assert reg.get_floating_cards() == {}
    assert reg.list_loaded_plugins() == []
    reg.reset()  # 清理


def test_dataclass_construction():
    """数据类可正常构造"""
    r = ContentRendererInfo(
        plugin_name="test",
        type_name="t1",
        render_func=lambda d, ctx: "<html/>",
        priority=10,
    )
    assert r.plugin_name == "test"
    assert r.priority == 10
