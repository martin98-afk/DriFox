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


def test_register_floating_card():
    """注册浮动卡片"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()

    class FakeCard:
        def __init__(self):
            pass

    reg.register_floating_card(
        plugin_name="plug-a",
        card_id="plug-a:mycard",
        widget_class=FakeCard,
        container="top",
        title="我的卡片",
    )
    cards = reg.get_floating_cards()
    assert "plug-a:mycard" in cards
    assert cards["plug-a:mycard"].container == "top"
    assert cards["plug-a:mycard"].title == "我的卡片"
    reg.reset()


def test_floating_card_auto_registers_command():
    """注册浮动卡片自动注册对应命令"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.set_main_widget(_FakeMainWidget())

    class FakeCard:
        def __init__(self):
            pass

    reg.register_floating_card(
        plugin_name="system",
        card_id="mycard",
        widget_class=FakeCard,
        container="top",
        title="My Card",
    )
    # 检查命令已注册
    from app.core.command_manager import CommandManager

    cmd_mgr = CommandManager.get_instance()
    # 命令应使用短名（system 插件）
    assert cmd_mgr.has_command("mycard") is True
    cmd_mgr.unregister("mycard")
    reg.reset()


def test_floating_card_user_plugin_namespaced_command():
    """用户插件的命令带命名空间前缀"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.set_main_widget(_FakeMainWidget())

    class FakeCard:
        pass

    reg.register_floating_card(
        plugin_name="user-plugin",
        card_id="mycard",
        widget_class=FakeCard,
        container="top",
    )
    from app.core.command_manager import CommandManager

    cmd_mgr = CommandManager.get_instance()
    # 用户插件：card_id 不含前缀时，命令名前缀
    assert cmd_mgr.has_command("user-plugin:mycard") is True
    cmd_mgr.unregister("user-plugin:mycard")
    reg.reset()


class _FakeMainWidget:
    """测试用 main_widget stub"""

    _window_id = "test"

    def _card_manager(self):
        return None


def test_load_plugin_invokes_register_ui(tmp_path, monkeypatch):
    """load_plugin 调用插件 ui/__init__.py 中的 register_ui"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()

    # 创建临时插件目录
    plugin_dir = tmp_path / "plug-x"
    ui_dir = plugin_dir / "ui"
    ui_dir.mkdir(parents=True)
    (ui_dir / "__init__.py").write_text(
        """
from app.core.ui_plugin_registry import UIPluginRegistry

def register_ui(registry: UIPluginRegistry):
    registry.register_content_renderer(
        plugin_name='plug-x', type_name='t1',
        render_func=lambda d, c: 'ok', priority=0
    )
""",
        encoding="utf-8",
    )

    ok = reg.load_plugin("plug-x", plugin_dir)
    assert ok is True
    assert reg.is_loaded("plug-x") is True
    assert reg.get_content_renderer("t1") is not None
    reg.reset()


def test_unload_plugin_clears_registrations():
    """unload_plugin 清理该插件的所有注册"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.set_main_widget(_FakeMainWidget())

    class FakeCard:
        pass

    reg.register_content_renderer("plug-y", "t1", lambda d, c: "x", priority=1)
    reg.register_floating_card("plug-y", "card1", FakeCard, "top", title="Card 1")
    reg._loaded_plugins.add("plug-y")

    reg.unload_plugin("plug-y")
    assert reg.get_content_renderer("t1") is None
    assert "card1" not in reg.get_floating_cards()
    assert "plug-y" not in reg._loaded_plugins

    from app.core.command_manager import CommandManager

    cmd_mgr = CommandManager.get_instance()
    assert cmd_mgr.has_command("card1") is False
    reg.reset()


def test_load_plugin_raises_for_missing_init():
    """ui/__init__.py 不存在时加载失败"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    ok = reg.load_plugin("nonexistent", None)
    assert ok is False
    reg.reset()


# ═══════════════════════════════════════════════════════════════
# 插件卡片 → 系统卡片注册（隐藏输入区）
# ═══════════════════════════════════════════════════════════════


class _FakeContainer:
    """模拟 BottomCardContainer / TopCardContainer 的最小接口"""

    def __init__(self):
        self.added: list = []  # 记录 add_card(card_id, widget)

    def add_card(self, card_id, widget):
        self.added.append((card_id, widget))


class _FakeCardManager:
    """模拟 CardManager 的最小接口"""

    def __init__(self):
        self.registered: list = []  # (window_id, container_type, card_id, widget)
        self.shown_callbacks: dict = {}  # card_id -> [callback]
        self.hidden_callbacks: dict = {}  # card_id -> [callback]

    def register_card(self, window_id, container_type, card_id, widget):
        self.registered.append((window_id, container_type, card_id, widget))

    def toggle_card(self, card_id, window_id):
        pass

    def on_card_shown(self, window_id, card_id, callback):
        self.shown_callbacks.setdefault(card_id, []).append(callback)

    def on_card_hidden(self, window_id, card_id, callback):
        self.hidden_callbacks.setdefault(card_id, []).append(callback)

    def hide_card(self, card_id, window_id):
        pass


class _FakeMainWidgetWithSystemCard:
    """带 register_system_card 跟踪的 main_widget stub

    模拟真实 MainWidget 暴露的接口：_window_id / _card_manager /
    _top_card_container / _bottom_card_container / register_system_card。
    """

    def __init__(self):
        self._window_id = "test"
        self._card_manager = _FakeCardManager()
        self._top_card_container = _FakeContainer()
        self._bottom_card_container = _FakeContainer()
        # 记录 register_system_card 调用
        self.system_card_registered: list = []

    def register_system_card(self, card_id: str) -> None:
        self.system_card_registered.append(card_id)


def test_show_floating_card_registers_as_system_card():
    """_show_floating_card 首次显示时应调用 register_system_card，让插件卡片像系统配置卡片一样隐藏输入区

    容器选择：plugin-marketplace 和 plugin-manager 已迁移为 container="bottom"，
    因此 widget 应被加入 _bottom_card_container，并触发 register_system_card。
    """
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    main_widget = _FakeMainWidgetWithSystemCard()
    reg.set_main_widget(main_widget)

    class FakeCard:
        def __init__(self, parent=None):
            self.parent = parent

    reg.register_floating_card(
        plugin_name="plug-bot",
        card_id="plug-bot",
        widget_class=FakeCard,
        container="bottom",
        title="底部插件卡片",
    )

    # 首次调用 _show_floating_card：应创建 widget、加入容器、注册为系统卡片
    reg._show_floating_card("plug-bot")

    # 1) widget 已加入 _bottom_card_container
    assert len(main_widget._bottom_card_container.added) == 1
    assert main_widget._bottom_card_container.added[0][0] == "plug-bot"
    # 2) CardManager 已注册该卡片
    assert len(main_widget._card_manager.registered) == 1
    assert main_widget._card_manager.registered[0][2] == "plug-bot"
    # 3) 已调用 register_system_card（隐藏输入区）
    assert "plug-bot" in main_widget.system_card_registered

    # 二次调用：widget 已缓存，不应重复调用 register_system_card
    reg._show_floating_card("plug-bot")
    assert main_widget.system_card_registered.count("plug-bot") == 1

    # 清理
    from app.core.command_manager import CommandManager

    cmd_mgr = CommandManager.get_instance()
    cmd_mgr.unregister("plug-bot:plug-bot")
    reg.reset()


def test_show_floating_card_works_without_register_system_card_api():
    """_show_floating_card 在旧版 main_widget（无 register_system_card API）下应安全降级，不抛异常

    向前兼容：旧版本/测试 stub 可能不暴露 register_system_card。
    """
    reg = UIPluginRegistry.get_instance()
    reg.reset()

    class _LegacyFakeMainWidget:
        # 故意不暴露 register_system_card，模拟旧版/简化 stub
        _window_id = "legacy"

        def __init__(self):
            self._card_manager = _FakeCardManager()
            self._bottom_card_container = _FakeContainer()

    legacy = _LegacyFakeMainWidget()
    reg.set_main_widget(legacy)

    class FakeCard:
        def __init__(self, parent=None):
            pass

    reg.register_floating_card(
        plugin_name="plug-old",
        card_id="plug-old",
        widget_class=FakeCard,
        container="bottom",
    )

    # 不应抛 AttributeError
    reg._show_floating_card("plug-old")

    # 仍然成功添加了卡片
    assert len(legacy._bottom_card_container.added) == 1

    # 清理
    from app.core.command_manager import CommandManager

    cmd_mgr = CommandManager.get_instance()
    cmd_mgr.unregister("plug-old:plug-old")
    reg.reset()
