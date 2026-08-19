# -*- coding: utf-8 -*-
"""回归测试：插件市场更新完成后必须重新加载插件 UI

复现背景：_async_update 下载前先 _unload_plugin_ui_on_gui 卸载 UI，
但目录替换产生的 watchfiles 事件是目录级（deleted/added 落在插件根路径），
组件识别为空 → _reload_single_plugin(name, "") 空组件跳过所有子系统，
UI 组件保持卸载态，直到重启或手动禁用再启用才恢复。

修复：_on_update_done 成功/失败分支均调用 _reload_plugin_ui_on_gui
显式重载 UI（rescan 刷新元数据 + load_plugin 加载）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

from app.plugins.managers import plugin_manager as pm_mod  # noqa: E402
from app.plugins.registries import ui_plugin_registry as reg_mod  # noqa: E402


def test_update_reload_reloads_plugin_ui(monkeypatch):
    """更新完成后必须重载 UI：rescan 元数据 + load_plugin 加载新版"""
    from ui.cards import MarketplaceCard

    calls = []

    class FakePlugin:
        path = "plugin-path"

        def has_component(self, comp):
            return comp == "ui"

    class FakePM:
        def __init__(self):
            self.plugin = FakePlugin()

        def rescan_plugin(self, name):
            calls.append(("rescan", name))

        def get_plugin(self, name):
            return self.plugin

    class FakeRegistry:
        def load_plugin(self, name, path):
            calls.append(("load", name, str(path)))

    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", staticmethod(lambda: FakePM()))
    monkeypatch.setattr(reg_mod.UIPluginRegistry, "get_instance", staticmethod(lambda: FakeRegistry()))

    card = MarketplaceCard.__new__(MarketplaceCard)
    card._reload_plugin_ui_on_gui("demo")

    assert calls == [("rescan", "demo"), ("load", "demo", "plugin-path")], (
        f"更新后未重载 UI，实际调用: {calls}"
    )


def test_update_reload_skips_ui_without_ui_component(monkeypatch):
    """无 ui 组件的插件：只 rescan 元数据，不调用 load_plugin"""
    from ui.cards import MarketplaceCard

    calls = []

    class FakePlugin:
        path = "plugin-path"

        def has_component(self, comp):
            return False

    class FakePM:
        def __init__(self):
            self.plugin = FakePlugin()

        def rescan_plugin(self, name):
            calls.append(("rescan", name))

        def get_plugin(self, name):
            return self.plugin

    class FakeRegistry:
        def load_plugin(self, name, path):
            calls.append(("load", name, str(path)))

    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", staticmethod(lambda: FakePM()))
    monkeypatch.setattr(reg_mod.UIPluginRegistry, "get_instance", staticmethod(lambda: FakeRegistry()))

    card = MarketplaceCard.__new__(MarketplaceCard)
    card._reload_plugin_ui_on_gui("demo")

    assert calls == [("rescan", "demo")], f"无 ui 组件不应 load_plugin，实际: {calls}"


def test_update_reload_tolerates_failure(monkeypatch):
    """rescan/load 抛异常不得上抛（更新主流程不受影响）"""
    from ui.cards import MarketplaceCard

    class BrokenPM:
        def rescan_plugin(self, name):
            raise RuntimeError("disk error")

        def get_plugin(self, name):
            raise RuntimeError("disk error")

    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", staticmethod(lambda: BrokenPM()))
    # 不 mock UIPluginRegistry：不应被触达（rescan 已抛）

    card = MarketplaceCard.__new__(MarketplaceCard)
    card._reload_plugin_ui_on_gui("demo")  # 不应抛异常



class FakeStatusLabel:
    """最小状态栏桩：_on_update_done 只 setText 清空文案，无需真实 widget"""

    def setText(self, text):
        self._text = text

    def setStyleSheet(self, css):
        pass


def test_update_done_skips_reload_when_keep_disabled(monkeypatch):
    """禁用插件更新完成：keep_disabled=True → 不重载 UI（保持禁用）"""
    from ui.cards import MarketplaceCard

    card = MarketplaceCard.__new__(MarketplaceCard)
    card._alive = lambda: True  # __new__ 构造无 C++ 对象，_alive 恒 False → 强制存活
    card.window = lambda: None  # 跳过 self.window()（QWidget 未初始化）
    card._status_label = FakeStatusLabel()
    reload_calls = []
    card._reload_plugin_ui_on_gui = lambda name: reload_calls.append(name)
    card._refresh_row_states = lambda: None
    card._update_row_state = lambda *a, **k: None
    # 禁用 InfoBar 真实弹窗
    import ui.cards as cards_mod

    monkeypatch.setattr(cards_mod, "InfoBar", type("FakeInfoBar", (), {"success": staticmethod(lambda *a, **k: None), "error": staticmethod(lambda *a, **k: None)}))

    card._on_update_done({"name": "demo", "keep_disabled": True}, True)
    assert reload_calls == [], "禁用插件更新成功不得重载 UI"

    card._on_update_done({"name": "demo", "keep_disabled": True}, False)
    assert reload_calls == [], "禁用插件更新失败也不得重载 UI"


def test_update_done_reloads_when_not_disabled(monkeypatch):
    """启用插件更新完成：keep_disabled=False → 照常重载 UI"""
    from ui.cards import MarketplaceCard

    card = MarketplaceCard.__new__(MarketplaceCard)
    card._alive = lambda: True  # __new__ 构造无 C++ 对象，_alive 恒 False → 强制存活
    card.window = lambda: None  # 跳过 self.window()（QWidget 未初始化）
    card._status_label = FakeStatusLabel()
    reload_calls = []
    card._reload_plugin_ui_on_gui = lambda name: reload_calls.append(name)
    card._refresh_row_states = lambda: None
    card._update_row_state = lambda *a, **k: None
    import ui.cards as cards_mod

    monkeypatch.setattr(cards_mod, "InfoBar", type("FakeInfoBar", (), {"success": staticmethod(lambda *a, **k: None), "error": staticmethod(lambda *a, **k: None)}))

    card._on_update_done({"name": "demo", "keep_disabled": False}, True)
    assert reload_calls == ["demo"], "启用插件更新成功必须重载 UI"

    card._on_update_done({"name": "demo", "keep_disabled": False}, False)
    assert reload_calls == ["demo", "demo"], "启用插件更新失败也必须重载 UI（恢复旧版）"
