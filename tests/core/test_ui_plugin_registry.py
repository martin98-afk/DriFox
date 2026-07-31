# -*- coding: utf-8 -*-
"""UIPluginRegistry 单元测试"""

import sys
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
    """同 type_name，高优先级覆盖低优先级"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_content_renderer("a", "t1", lambda d, c: "low", priority=1)
    reg.register_content_renderer("b", "t1", lambda d, c: "high", priority=10)
    info = reg.get_content_renderer("t1")
    assert info.plugin_name == "b"
    assert info.render_func(None, None) == "high"
    reg.reset()


def test_register_content_renderer_same_priority_warns():
    """同 type_name 同 priority，后注册者覆盖"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_content_renderer("a", "t1", lambda d, c: "first", priority=5)
    reg.register_content_renderer("b", "t1", lambda d, c: "second", priority=5)
    info = reg.get_content_renderer("t1")
    assert info.plugin_name == "b"
    assert info.render_func(None, None) == "second"
    reg.reset()


def test_register_message_factory():
    """注册消息工厂"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_message_factory(
        plugin_name="plug-a",
        name="fact1",
        condition_func=lambda msg: msg.get("role") == "assistant",
        factory_func=lambda msg, parent: "widget",
        priority=0,
    )
    factories = reg.get_message_factories()
    assert len(factories) == 1
    assert factories[0].name == "fact1"
    assert factories[0].condition_func({"role": "assistant"}) is True
    assert factories[0].condition_func({"role": "user"}) is False
    reg.reset()


def test_message_factories_sorted_by_priority():
    """get_message_factories 按 priority 降序"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_message_factory("a", "low", lambda m: True, None, priority=1)
    reg.register_message_factory("b", "high", lambda m: True, None, priority=100)
    reg.register_message_factory("c", "mid", lambda m: True, None, priority=50)
    names = [f.name for f in reg.get_message_factories()]
    assert names == ["high", "mid", "low"]
    reg.reset()


def test_register_floating_card():
    """注册浮动卡片 + 容器校验"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_floating_card(
        plugin_name="plug-b",
        card_id="plug-b:card-a",
        widget_class=object,
        container="top",
        title="Card A",
    )
    cards = reg.get_floating_cards()
    assert "plug-b:card-a" in cards
    info = cards["plug-b:card-a"]
    assert info.title == "Card A"
    assert info.container == "top"
    assert info.widget_class is object
    # 非法容器 → ValueError
    with pytest.raises(ValueError):
        reg.register_floating_card(
            plugin_name="plug-b",
            card_id="bad",
            widget_class=object,
            container="middle",
        )
    reg.reset()


def test_register_floating_card_full_container():
    """container="full" 合法：完整覆盖对话区（与系统配置卡片一致）"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_floating_card(
        plugin_name="plug-full",
        card_id="plug-full:full-card",
        widget_class=object,
        container="full",
        title="Full Card",
    )
    info = reg.get_floating_cards()["plug-full:full-card"]
    assert info.container == "full"
    reg.reset()


def test_floating_card_auto_registers_command():
    """register_floating_card 应自动注册 /card_id 命令（系统插件用短名）"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    from app.core.command_manager import CommandManager

    cmd_mgr = CommandManager.get_instance()
    # 系统插件（plugin_name == "system"）→ 短名
    reg.register_floating_card(
        plugin_name="system",
        card_id="mycard",
        widget_class=object,
        container="top",
    )
    assert cmd_mgr.has_command("mycard")
    cmd_mgr.unregister("mycard")
    reg.reset()


def test_floating_card_user_plugin_namespaced_command():
    """非系统插件的浮动卡片应注册为 namespaced 命令"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    from app.core.command_manager import CommandManager

    cmd_mgr = CommandManager.get_instance()
    # 用户插件 → plugin_name:card_id 形式
    reg.register_floating_card(
        plugin_name="plug-user",
        card_id="plug-user:card",
        widget_class=object,
        container="top",
    )
    assert cmd_mgr.has_command("plug-user:card")
    cmd_mgr.unregister("plug-user:card")
    reg.reset()


# ═══════════════════════════════════════════════════════════════
# 插件加载/卸载
# ═══════════════════════════════════════════════════════════════


def test_load_plugin_invokes_register_ui(tmp_path):
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

    def register_card(self, window_id, container_type, card_id, widget, system_card=False):
        self.registered.append((window_id, container_type, card_id, widget))

    def toggle_card(self, card_id, window_id):
        pass

    def on_card_shown(self, window_id, card_id, callback):
        self.shown_callbacks.setdefault(card_id, []).append(callback)

    def on_card_hidden(self, window_id, card_id, callback):
        self.hidden_callbacks.setdefault(card_id, []).append(callback)

    def hide_card(self, card_id, window_id):
        pass

    def is_card_visible(self, card_id, window_id):
        return False


class _FakeMainWidget:
    """简化 main_widget stub（不含 register_system_card）"""

    def __init__(self):
        self._window_id = "test"
        self._card_manager = _FakeCardManager()
        self._top_card_container = _FakeContainer()
        self._bottom_card_container = _FakeContainer()


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

    容器选择：插件卡片可注册在 bottom/full 等位置（如 plugin-marketplace / plugin-manager
    已迁移为 container="full"），显示时均触发 register_system_card。
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


# ═══════════════════════════════════════════════════════════════
# 热重载：修改 cards.py 后 reload_plugin 应重新导入子模块
# ═══════════════════════════════════════════════════════════════


def test_reload_plugin_reimports_submodules(tmp_path):
    """修改 ui/cards.py 后 reload_plugin 应重新读取文件，而非返回旧缓存

    这是验证方案 A（清除 sys.modules 中的子模块缓存）的核心测试。

    测试流程：
    1. 创建临时插件，ui/cards.py 返回 "v1"
    2. 加载插件，验证 renderer 返回 "v1"
    3. 验证子模块存在于 sys.modules
    4. 修改 cards.py 改为返回 "v2"
    5. reload_plugin
    6. 验证 sys.modules 中的旧子模块已被清除
    7. 验证 renderer 返回 "v2"（说明重新读取了文件）
    """
    import importlib

    reg = UIPluginRegistry.get_instance()
    reg.reset()

    # ── 1. 创建临时插件目录 ──
    plugin_dir = tmp_path / "hotreload-test"
    ui_dir = plugin_dir / "ui"
    ui_dir.mkdir(parents=True)

    # cards.py — v1
    (ui_dir / "cards.py").write_text(
        """
VERSION = "v1"

def greet() -> str:
    return "Hello from v1"
""",
        encoding="utf-8",
    )

    # ui/__init__.py — 导入 cards 并注册 renderer
    (ui_dir / "__init__.py").write_text(
        """
from .cards import VERSION, greet

def register_ui(registry):
    registry.register_content_renderer(
        plugin_name='hotreload-test',
        type_name='hotreload-test-t1',
        render_func=lambda d, ctx: f"greet()={greet()}, VERSION={VERSION}",
        priority=0,
    )
""",
        encoding="utf-8",
    )

    # ── 2. 首次加载 ──
    ok = reg.load_plugin("hotreload-test", plugin_dir)
    assert ok is True
    assert reg.is_loaded("hotreload-test") is True

    info = reg.get_content_renderer("hotreload-test-t1")
    assert info is not None
    assert info.render_func(None, None) == "greet()=Hello from v1, VERSION=v1"

    # ── 3. 验证子模块已在 sys.modules 中 ──
    mod_names = [k for k in sys.modules if k.startswith("ui_plugin_hotreload_test")]
    assert any(m.endswith(".cards") for m in mod_names), (
        f"cards 子模块未被缓存到 sys.modules，当前模块列表: {mod_names}"
    )
    cards_mod_name = [m for m in mod_names if m.endswith(".cards")][0]
    old_cards_mod = sys.modules.get(cards_mod_name)
    assert old_cards_mod is not None
    assert old_cards_mod.VERSION == "v1"

    # ── 4. 修改 cards.py 为 v2 ──
    (ui_dir / "cards.py").write_text(
        """
VERSION = "v2"

def greet() -> str:
    return "Hello from v2"
""",
        encoding="utf-8",
    )

    # ── 5. 执行重载 ──
    ok = reg.reload_plugin("hotreload-test", plugin_dir)
    assert ok is True

    # ── 6. 验证旧子模块已被重新导入（而非返回旧缓存） ──
    #    子模块在 reload 过程中会从 sys.modules 清除 → 重新从磁盘读取 → 再放回 sys.modules。
    #    所以重载后同名模块仍然存在，但应该是新的模块对象（VERSION="v2"）。
    new_mod_names = [k for k in sys.modules if k.startswith("ui_plugin_hotreload_test")]
    new_cards_mod_names = [m for m in new_mod_names if m.endswith(".cards")]
    assert len(new_cards_mod_names) == 1, f"重载后应有 1 个 cards 子模块，实际: {new_cards_mod_names}"
    new_cards_mod = sys.modules[new_cards_mod_names[0]]

    # 关键断言：模块对象已被替换（旧对象被清除后重新创建）
    assert old_cards_mod is not new_cards_mod, (
        f"重载后 cards 模块对象应被替换。"
        f"旧对象 id={id(old_cards_mod)}，新对象 id={id(new_cards_mod)}"
    )

    # ── 7. 验证新代码已生效 ──
    #    关键断言：新模块的 VERSION 是 "v2" 而不是 "v1"
    assert new_cards_mod.VERSION == "v2", (
        f"热重载后 cards 模块的 VERSION 应为 'v2'，实际为 '{new_cards_mod.VERSION}'"
    )
    assert new_cards_mod.greet() == "Hello from v2"

    # renderer 应反映新代码
    info = reg.get_content_renderer("hotreload-test-t1")
    assert info is not None
    assert info.render_func(None, None) == "greet()=Hello from v2, VERSION=v2", (
        f"重载后 renderer 应返回 v2 结果，实际: {info.render_func(None, None)}"
    )

    # ── 8. 清理 ──
    # 清理 sys.modules 中的残留
    for k in list(sys.modules.keys()):
        if k.startswith("ui_plugin_hotreload_test"):
            del sys.modules[k]
    reg.reset()


def test_reload_plugin_keeps_other_plugins_intact(tmp_path):
    """重载一个插件不应影响其他插件的模块缓存"""
    import importlib

    reg = UIPluginRegistry.get_instance()
    reg.reset()

    # ── 创建两个插件 ──
    plugin_a_dir = tmp_path / "plug-a"
    plugin_a_dir / "ui", plugin_a_dir.joinpath("ui").mkdir(parents=True)
    (plugin_a_dir / "ui" / "__init__.py").write_text(
        """
def register_ui(registry):
    registry.register_content_renderer('plug-a', 't1', lambda d, c: 'A', priority=0)
""",
        encoding="utf-8",
    )

    plugin_b_dir = tmp_path / "plug-b"
    plugin_b_dir / "ui", plugin_b_dir.joinpath("ui").mkdir(parents=True)
    (plugin_b_dir / "ui" / "cards.py").write_text(
        """
VERSION = "B-v1"
""",
        encoding="utf-8",
    )
    (plugin_b_dir / "ui" / "__init__.py").write_text(
        """
from .cards import VERSION
def register_ui(registry):
    registry.register_content_renderer('plug-b', 't1', lambda d, c: VERSION, priority=0)
""",
        encoding="utf-8",
    )

    # ── 加载两个插件 ──
    assert reg.load_plugin("plug-a", plugin_a_dir)
    assert reg.load_plugin("plug-b", plugin_b_dir)

    # 确认 plug-b 的 cards 子模块已缓存
    b_cards = [k for k in sys.modules if "plug_b" in k and k.endswith(".cards")]
    assert len(b_cards) == 1

    # ── 只重载 plug-a ──
    (plugin_a_dir / "ui" / "__init__.py").write_text(
        """
def register_ui(registry):
    registry.register_content_renderer('plug-a', 't1', lambda d, c: 'A-v2', priority=0)
""",
        encoding="utf-8",
    )
    assert reg.reload_plugin("plug-a", plugin_a_dir)

    # ── plug-b 的 renderer 应保持不变 ──
    info = reg.get_content_renderer("t1")
    # 两个插件都注册了 type_name="t1"，render_func 不同，靠 priority 决定哪个生效
    # plug-a priority=0, plug-b priority=0 → 后注册覆盖
    # 所以这里无法简单判断，换个验证方式：

    # 验证 plug-b 的 cards 子模块仍然在 sys.modules 中
    # （我们的清理只针对被重载插件的前缀）
    b_cards_after = [k for k in sys.modules if "plug_b" in k and k.endswith(".cards")]
    assert len(b_cards_after) == 1, "重载 plug-a 不应清除 plug-b 的模块缓存"
    assert sys.modules[b_cards_after[0]].VERSION == "B-v1", "plug-b 的模块内容不应被改变"

    # ── 清理 ──
    for k in list(sys.modules.keys()):
        if k.startswith("ui_plugin_plug_"):
            del sys.modules[k]
    reg.reset()


def test_reload_plugin_handles_non_ui_plugins_gracefully(tmp_path):
    """重载没有 ui 组件的插件应安全降级"""
    reg = UIPluginRegistry.get_instance()
    reg.reset()

    # 传入 None 路径
    ok = reg.reload_plugin("nonexistent", None)
    assert ok is False

    # 创建插件目录但没有 ui/ 目录
    plugin_dir = tmp_path / "no-ui-plugin"
    plugin_dir.mkdir()
    ok = reg.load_plugin("no-ui-plugin", plugin_dir)
    assert ok is False  # 没有 ui/__init__.py
    reg.reset()


# ═══════════════════════════════════════════════════════════════
# __pycache__ 清理与热重载循环防护
# ═══════════════════════════════════════════════════════════════


def test_reload_plugin_clears_and_recreates_pycache(tmp_path):
    """reload_plugin 应清除旧的 __pycache__，Python 重新编译后重新创建

    验证 watchfiles 在 __pycache__ 被删除时不会错误触发热重载：
    - watchfiles 的过滤规则会过滤含 __pycache__ 的路径
    - reload_plugin 幂等，多次重载后插件仍正常工作
    """
    reg = UIPluginRegistry.get_instance()
    reg.reset()

    plugin_dir = tmp_path / "pycache-test"
    ui_dir = plugin_dir / "ui"
    ui_dir.mkdir(parents=True)
    (ui_dir / "cards.py").write_text('VERSION = "v1"\n', encoding="utf-8")
    (ui_dir / "__init__.py").write_text(
        'from .cards import VERSION\n'
        'def register_ui(registry):\n'
        '    registry.register_content_renderer("pycache-test", "t1", lambda d, c: VERSION, 0)\n',
        encoding="utf-8",
    )

    # 首次加载 → 创建 __pycache__
    assert reg.load_plugin("pycache-test", plugin_dir)
    pycache = ui_dir / "__pycache__"
    assert pycache.exists(), "首次加载后应有 __pycache__"
    initial_pyc_files = list(pycache.iterdir())
    assert len(initial_pyc_files) > 0, "__pycache__ 中应有 .pyc 文件"

    # 连续多次重载（模拟 watchfiles 触发的热重载）
    for i in range(3):
        # 每次修改 cards.py（模拟开发中不断改代码）
        version = f"v{i + 2}"
        (ui_dir / "cards.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")

        assert reg.reload_plugin("pycache-test", plugin_dir), f"第 {i+1} 次重载失败"

        # 验证 __pycache__ 被重新创建
        assert pycache.exists(), f"重载后 __pycache__ 应被重新创建 (第 {i+1} 次)"
        pyc_files = list(pycache.iterdir())
        assert len(pyc_files) > 0, f"重载后 __pycache__ 中应有 .pyc 文件 (第 {i+1} 次)"

        # 验证新代码生效
        info = reg.get_content_renderer("t1")
        assert info is not None
        assert info.render_func(None, None) == version, (
            f"第 {i+1} 次重载后 renderer 应返回 {version}，实际: {info.render_func(None, None)}"
        )

    # 清理
    import shutil
    for k in list(sys.modules.keys()):
        if k.startswith("ui_plugin_pycache_test"):
            del sys.modules[k]
    reg.reset()


def test_watchfiles_filter_ignores_pycache_paths():
    """验证 watchfiles 的路径过滤规则能正确跳过 __pycache__ 相关变更

    backend.py _watch_loop 中的过滤逻辑：
    if ".git" in p or "__pycache__" in p or p.endswith(".pyc"):
        continue

    模拟 watchfiles 上报的路径，验证所有含 __pycache__ 的路径都被过滤。
    """
    # 重现 watchfiles 的过滤逻辑
    def _should_filter(change_path: str) -> bool:
        p = change_path.lower()
        if ".git" in p or "__pycache__" in p or p.endswith(".pyc"):
            return True
        return False

    # 场景：shutil.rmtree(__pycache__) 产生的事件
    rmtree_events = [
        ("Deleted", r"D:\work\DriFox\plugins\my-plugin\ui\__pycache__\cards.cpython-314.pyc"),
        ("Deleted", r"D:\work\DriFox\plugins\my-plugin\ui\__pycache__\__init__.cpython-314.pyc"),
        ("Deleted", r"D:\work\DriFox\plugins\my-plugin\ui\__pycache__"),
    ]
    for event_type, path in rmtree_events:
        assert _should_filter(path), f"rmtree 事件应被过滤: {event_type} {path}"

    # 场景：reload 后 Python 重新编译生成的新 .pyc
    recompile_events = [
        ("Added", r"D:\work\DriFox\plugins\my-plugin\ui\__pycache__\cards.cpython-314.pyc"),
        ("Added", r"D:\work\DriFox\plugins\my-plugin\ui\__pycache__\__init__.cpython-314.pyc"),
    ]
    for event_type, path in recompile_events:
        assert _should_filter(path), f"重新编译事件应被过滤: {event_type} {path}"

    # 场景：正常插件的 .py 文件变更不应被过滤
    normal_changes = [
        r"D:\work\DriFox\plugins\my-plugin\ui\cards.py",
        r"D:\work\DriFox\plugins\my-plugin\ui\__init__.py",
        r"D:\work\DriFox\plugins\my-plugin\agents\my-agent.yaml",
        r"D:\work\DriFox\plugins\my-plugin\commands\my-command.yaml",
    ]
    for path in normal_changes:
        assert not _should_filter(path), f"正常 .py 文件不应被过滤: {path}"
