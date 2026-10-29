# -*- coding: utf-8 -*-
"""回归测试：注册表卡片标题变化要同步到已挂的标题栏 tab

复现背景：full 卡在标题栏 tab 的文字在 add_tab 时固化。插件市场把待更新数
写进 floating card title（「插件市场 (N)」↔「插件市场」），侧栏计数更新后
已打开的 tab 文字不变（用户视角：自动更新完插件 tab 文字没动）。

修复：CustomTitleBar.set_tab_text + TabManagerWindow._sync_replace_tab_titles
（_update_shared_launcher / showEvent 补刷 / 卡片重新显示时纠正）。
"""

from collections import OrderedDict
from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QWidget

from app.widgets.custom_title_bar import CustomTitleBar
from app.widgets.tab_manager_window import TabManagerWindow


@pytest.fixture
def container(qtbot):
    w = QWidget()
    qtbot.addWidget(w)
    return w


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置单例与 TrayManager 引用"""
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


def test_set_tab_text_updates_label(qtbot, container):
    """set_tab_text 更新已挂 tab 文字（幂等，重复调用不重建按钮）"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("plugin-marketplace", "插件市场 (2)", closable=True)

    btn = tb._tabs["plugin-marketplace"]
    tb.set_tab_text("plugin-marketplace", "插件市场")
    assert btn._label.text() == "插件市场"
    # 同一个按钮对象：改文字不得走 remove+add（避免闪烁/不丢关闭钮）
    assert tb._tabs["plugin-marketplace"] is btn

    tb.set_tab_text("plugin-marketplace", "插件市场")  # 幂等
    assert btn._label.text() == "插件市场"


def test_set_tab_text_unknown_id_is_noop(qtbot, container):
    """tab 不存在时静默跳过（新增仍走 add_tab）"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.set_tab_text("nonexistent", "x")
    assert "nonexistent" not in tb._tabs


def test_sync_replace_tab_titles_updates_open_tab(qtbot, monkeypatch):
    """注册表 title 变化 → 已挂 tab 文字与 open 集合同步更新"""
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open[GLOBAL_WINDOW_ID] = OrderedDict({"plugin-marketplace": "插件市场 (2)"})
    tm._refresh_titlebar_cards()
    assert tm.titleBar._tabs["plugin-marketplace"]._label.text() == "插件市场 (2)"

    class FakeRegistry:
        def get_floating_cards(self):
            return {"plugin-marketplace": SimpleNamespace(title="插件市场")}

    monkeypatch.setattr(UIPluginRegistry, "get_instance", staticmethod(lambda: FakeRegistry()))

    tm._sync_replace_tab_titles()

    assert tm.titleBar._tabs["plugin-marketplace"]._label.text() == "插件市场"
    assert tm._replace_open[GLOBAL_WINDOW_ID]["plugin-marketplace"] == "插件市场", (
        "open 集合的标题也要同步，供 _refresh_titlebar_cards 重建时复用"
    )


def test_update_shared_launcher_syncs_replace_tab_titles(qtbot, monkeypatch):
    """入口链路：refresh_sidebar_launcher → _update_shared_launcher 必须同步 tab 文案

    插件市场更新计数后走 UIPluginRegistry.refresh_sidebar_launcher()，
    内部落到 _update_shared_launcher；这里把它依赖的重活全部打桩，只验证
    _sync_replace_tab_titles 被调用（漏挂 = 修复失效）。
    """
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    calls = []
    monkeypatch.setattr(tm._tab_panel, "refresh_ui_plugins", lambda: None)
    monkeypatch.setattr(tm, "_sync_plugin_titlebar_tabs", lambda: None)
    monkeypatch.setattr(tm, "_sync_replace_tab_titles", lambda: calls.append(1))
    host = getattr(tm, "_workspace_page_host", None)
    if host is not None:
        monkeypatch.setattr(host, "refresh_pages", lambda: None)

    tm._update_shared_launcher()

    assert calls == [1], "_update_shared_launcher 未同步 replace tab 文案"


def test_sync_replace_tab_titles_falls_back_to_global_title(qtbot, monkeypatch):
    """注册表无该卡（内置全局卡）→ 回落 GLOBAL_REPLACE_TITLES"""
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open[GLOBAL_WINDOW_ID] = OrderedDict({"settings": "旧标题"})
    tm._refresh_titlebar_cards()
    assert tm.titleBar._tabs["settings"]._label.text() == "旧标题"

    class EmptyRegistry:
        def get_floating_cards(self):
            return {}

    monkeypatch.setattr(UIPluginRegistry, "get_instance", staticmethod(lambda: EmptyRegistry()))

    tm._sync_replace_tab_titles()

    assert tm.titleBar._tabs["settings"]._label.text() == "系统设置"
