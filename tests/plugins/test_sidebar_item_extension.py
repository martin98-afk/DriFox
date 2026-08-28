# -*- coding: utf-8 -*-
"""D2: 侧边栏项独立扩展点（registry 层 + tab_panel 消费）

不变量：
- 注册 sidebar item → get_sidebar_items() 返回（system 前 custom 后）
- tab_panel.refresh_ui_plugins 渲染独立 sidebar 行（点击回调触发 on_click）
- 存量 floating card（container="left"）兼容派生；插件已注册 sidebar item 时不重复渲染
"""

from unittest.mock import MagicMock, patch

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        from app.widgets.tab_panel import TabPanel

        p = TabPanel()
    qtbot.addWidget(p)
    return p


@pytest.fixture()
def host(qtbot):
    """侧边栏宿主窗口（含 get_current_window）——fixture 持有，规避局部变量 GC 连带删 panel"""
    from PySide6.QtWidgets import QWidget

    class _FakeHost(QWidget):
        def get_current_window(self):
            win = QWidget()
            win._window_id = "w1"
            return win

    h = _FakeHost()
    qtbot.addWidget(h)
    return h


# ---------- registry 层 ----------


def test_registry_sidebar_items_ordered(fresh_registry):
    """get_sidebar_items：system 组在前，custom 组在后"""
    fresh_registry.register_sidebar_item("demo", "c1", "自定义", group="custom", on_click=lambda ctx: None)
    fresh_registry.register_sidebar_item("demo", "s1", "系统", group="system", on_click=lambda ctx: None)
    items = fresh_registry.get_sidebar_items()
    assert [i.item_id for i in items] == ["s1", "c1"]


# ---------- tab_panel 消费 ----------


def test_refresh_ui_plugins_renders_sidebar_items(panel, qtbot, host, fresh_registry):
    """注册独立 sidebar 项 → refresh_ui_plugins 渲染出对应行，点击触发 on_click"""
    panel.setParent(host)

    clicks = []

    def _on_click(ctx):
        clicks.append(ctx)

    fresh_registry.register_sidebar_item("demo", "item-1", "插件项", group="custom", on_click=_on_click)
    # 存量 floating card（container="left"，非 sidebar 插件）→ 兼容派生
    class _FakeCard:
        pass

    fresh_registry.register_floating_card("legacy", "legacy-card", _FakeCard, container="left", title="存量卡片")

    panel.refresh_ui_plugins()

    # 自定义区渲染 2 行：插件项 + 存量卡片
    assert len(panel._custom_plugin_buttons) == 2
    # 点击插件项行（按 card_id 定位）→ on_click 触发且 context 含 item_id
    sidebar_row = next(r for r in panel._custom_plugin_buttons if r._card_id == "item-1")
    sidebar_row.clicked.emit()
    assert len(clicks) == 1
    assert clicks[0]["item_id"] == "item-1"
    assert clicks[0]["window_id"] == "w1"


def test_sidebar_item_skips_duplicate_card(panel, fresh_registry):
    """插件同时注册 sidebar item + container="left" card → 以 sidebar 为准，card 派生跳过"""
    class _FakeCard:
        pass

    fresh_registry.register_sidebar_item("demo", "item-1", "插件项", group="custom", on_click=lambda ctx: None)
    fresh_registry.register_floating_card("demo", "demo-card", _FakeCard, container="left", title="重复卡片")
    # 另一插件只有 floating card → 正常派生
    fresh_registry.register_floating_card("other", "other-card", _FakeCard, container="left", title="其他卡片")

    panel.refresh_ui_plugins()
    # 只有 2 行：item-1（sidebar）+ other-card（存量派生）；demo-card 被跳过
    assert len(panel._custom_plugin_buttons) == 2
    assert {entry[0:2] for entry in panel._plugin_infos} == {("sidebar", "item-1"), ("card", "other-card")}


def test_system_group_renders_in_system_section(panel, fresh_registry):
    """group="system" 的 sidebar 项渲染到系统插件区"""
    fresh_registry.register_sidebar_item("demo", "s1", "系统项", group="system", on_click=lambda ctx: None)
    panel.refresh_ui_plugins()
    assert len(panel._system_plugin_buttons) == 1
    assert panel._plugin_infos == [("sidebar", "s1", "系统项", "demo", 0)]


# ---------- Phase E：priority 排序失效修复 ----------


class TestSidebarSortPriority:
    def test_priority_beats_title_sort(self, fresh_registry):
        """sidebar 条目排序：priority 降序优先于标题字母序
        （regression：tab_panel 曾按 title.lower() 重排导致 priority 失效）"""
        from app.widgets.tab_panel import _sort_plugin_entries

        # (kind, key, title, plugin_name, priority)；b 注册序在前但 priority 低
        entries = [
            ("sidebar", "b", "aaa 高字母低权重", "demo", 1),
            ("sidebar", "a", "zzz 低字母高权重", "demo", 10),
            ("card", "c", "卡片", "demo", 5),
        ]
        # priority desc: a(10) > c(5) > b(1) → 期望 [a, c, b]
        assert [e[1] for e in _sort_plugin_entries(entries)] == ["a", "c", "b"]

    def test_title_sort_is_stable_within_same_priority(self, fresh_registry):
        """同 priority 内部按 title 字母序兜底（稳定）"""
        from app.widgets.tab_panel import _sort_plugin_entries

        entries = [
            ("sidebar", "x", "zzz", "demo", 5),
            ("sidebar", "y", "aaa", "demo", 5),
        ]
        assert [e[1] for e in _sort_plugin_entries(entries)] == ["y", "x"]


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
