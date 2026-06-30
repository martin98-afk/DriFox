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


def test_register_content_renderer():
    """注册内容块渲染器"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_content_renderer(
        plugin_name="plug-a",
        type_name="my_chart",
        render_func=lambda d, ctx: f"<div>{d}</div>",
        priority=5,
    )
    info = reg.get_content_renderer("my_chart")
    assert info is not None
    assert info.plugin_name == "plug-a"
    assert info.priority == 5
    assert info.render_func({"x": 1}, None) == "<div>{'x': 1}</div>"
    reg.reset()


def test_register_content_renderer_overrides_on_higher_priority():
    """高优先级覆盖低优先级"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_content_renderer("p1", "shared", lambda d, c: "A", priority=1)
    reg.register_content_renderer("p2", "shared", lambda d, c: "B", priority=10)
    assert reg.get_content_renderer("shared").plugin_name == "p2"
    reg.reset()


def test_register_content_renderer_same_priority_warns():
    """同优先级时后注册覆盖前注册（保持稳定）"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_content_renderer("p1", "shared", lambda d, c: "A", priority=1)
    reg.register_content_renderer("p2", "shared", lambda d, c: "B", priority=1)
    assert reg.get_content_renderer("shared").plugin_name == "p2"
    reg.reset()


def test_register_message_factory():
    """注册消息工厂"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()

    class FakeWidget:
        pass

    reg.register_message_factory(
        plugin_name="plug-a",
        name="custom_widget_factory",
        condition_func=lambda m: m.get("role") == "system",
        factory_func=lambda m, parent: FakeWidget(),
        priority=10,
    )
    factories = reg.get_message_factories()
    assert len(factories) == 1
    assert factories[0].plugin_name == "plug-a"
    assert factories[0].condition_func({"role": "system"}) is True
    assert factories[0].condition_func({"role": "user"}) is False
    reg.reset()


def test_message_factories_sorted_by_priority():
    """工厂按 priority 降序排列"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_message_factory("p1", "f1", lambda m: True, lambda m, p: None, priority=1)
    reg.register_message_factory("p2", "f2", lambda m: True, lambda m, p: None, priority=10)
    reg.register_message_factory("p3", "f3", lambda m: True, lambda m, p: None, priority=5)
    factories = reg.get_message_factories()
    assert [f.name for f in factories] == ["f2", "f3", "f1"]
    reg.reset()
