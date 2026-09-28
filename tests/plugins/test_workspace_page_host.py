# -*- coding: utf-8 -*-
"""WorkspacePageHost：懒创建/激活/refresh/teardown

纯逻辑测试：用 mock 替代 _tab_window._content_area / QStackedWidget，
避开 Windows 下 PyQt5 offscreen 平台不稳定导致的 QApplication 崩溃。
"""

from unittest.mock import MagicMock

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

pytest.importorskip("PyQt5.QtWidgets", reason="仅在 PyQt5 环境加载 mock 类")



@pytest.fixture()
def host(fresh_registry):
    from app.widgets.workspace_page_host import WorkspacePageHost

    # mock tab_window：_content_area 记录 addWidget/removeWidget/setCurrentIndex；
    # _build_ui_context 返回固定 context（与计划文档 _FakeTabWin 等效）
    tab_win = MagicMock()
    tab_win._content_area.addWidget = MagicMock(side_effect=lambda w: len(tab_win._content_area._children))
    tab_win._content_area.removeWidget = MagicMock()
    tab_win._content_area.setCurrentIndex = MagicMock()
    tab_win._content_area._children = []

    def _add_widget(w):
        idx = len(tab_win._content_area._children)
        tab_win._content_area._children.append(w)
        return idx

    tab_win._content_area.addWidget.side_effect = _add_widget
    tab_win._build_ui_context = MagicMock(return_value={"window_id": "__global__"})
    tab_win._window_id = "__global__"

    h = WorkspacePageHost()
    h.attach_to(tab_win)
    return h


class _Page:
    """轻量 page widget 替身（记录构造参数；不依赖 Qt）"""

    def __init__(self, parent=None, context=None):
        self.parent = parent
        self.context = context


class _OtherPage:
    """用于覆盖测试"""

    def __init__(self, parent=None, context=None):
        pass


class TestWorkspacePageHost:
    def test_lazy_creation_on_first_show(self, host, fresh_registry):
        created = []

        class _P:
            def __init__(self, parent=None, context=None):
                created.append((parent, context))

        fresh_registry.register_workspace_page("demo", "dash", "仪表盘", _P)
        host.refresh_pages()
        assert created == []  # 未 show 不创建
        host.show_page("dash")
        assert len(created) == 1 and created[0][1] == {"window_id": "__global__"}
        host.show_page("dash")
        assert len(created) == 1  # 二次 show 不重建

    def test_teardown_plugin_destroys_page(self, host, fresh_registry):
        fresh_registry.register_workspace_page("demo", "dash", "D", _Page)
        host.refresh_pages()
        host.show_page("dash")
        assert "dash" in host.get_loaded_page_ids()
        fresh_registry.unload_plugin("demo")
        host.teardown_plugin("demo")
        assert host.get_loaded_page_ids() == []

    def test_refresh_picks_up_new_page(self, host, fresh_registry):
        fresh_registry.register_workspace_page("demo", "p1", "P1", _Page)
        host.refresh_pages()
        assert host.get_known_page_ids() == ["p1"]
        fresh_registry.register_workspace_page("demo", "p2", "P2", _Page)
        host.refresh_pages()
        assert host.get_known_page_ids() == ["p1", "p2"]

    def test_refresh_unloads_removed_page(self, host, fresh_registry):
        """refresh_pages 自动对比销毁被卸载页面（无需显式 teardown_plugin）"""
        fresh_registry.register_workspace_page("demo", "p1", "P1", _Page)
        host.refresh_pages()
        host.show_page("p1")
        fresh_registry.unload_plugin("demo")
        host.refresh_pages()  # refresh 内部对比销毁
        assert host.get_loaded_page_ids() == []

    def test_hide_sidebar_skips_entry(self, host, fresh_registry):
        fresh_registry.register_workspace_page("demo", "hidden", "H", _Page, metadata={"hide_sidebar": True})
        host.refresh_pages()
        # hide_sidebar=True 不创建 sidebar 入口（_sidebar_item_ids 为空）
        assert host._sidebar_item_ids == []

    # ── primary_entry 派生标题栏 tab ──

    def test_primary_entry_titlebar_derived_and_click_shows_page(self, host, fresh_registry):
        """primary_entry kind=titlebar：refresh 后派生 wp-tab:* 常驻 tab，点击懒创建页面"""
        fresh_registry.register_workspace_page(
            "demo",
            "dash",
            "仪表盘",
            _Page,
            metadata={"primary_entry": {"kind": "titlebar", "label": "面板", "priority": 2}},
        )
        host.refresh_pages()
        tabs = {t.tab_id: t for t in fresh_registry.get_titlebar_tabs()}
        assert "wp-tab:dash" in tabs
        info = tabs["wp-tab:dash"]
        assert info.plugin_name == "demo"
        assert info.label == "面板"
        assert info.priority == 2
        assert info.on_click is not None
        # 点击回调 = 激活页面（懒创建）
        info.on_click()
        assert host.get_loaded_page_ids() == ["dash"]

    def test_primary_entry_str_shorthand(self, host, fresh_registry):
        """primary_entry 字符串简写：label 缺省用页面 title"""
        fresh_registry.register_workspace_page("demo", "dash", "仪表盘", _Page, metadata={"primary_entry": "titlebar"})
        host.refresh_pages()
        (info,) = fresh_registry.get_titlebar_tabs()
        assert info.tab_id == "wp-tab:dash"
        assert info.label == "仪表盘"

    def test_primary_entry_invalid_kind_ignored(self, host, fresh_registry):
        """primary_entry.kind 非 titlebar：告警跳过，不派生不抛异常"""
        fresh_registry.register_workspace_page(
            "demo", "dash", "D", _Page, metadata={"primary_entry": {"kind": "sidebar"}}
        )
        host.refresh_pages()
        assert fresh_registry.get_titlebar_tabs() == []
        # 侧栏入口仍由自动派生路径生成（与 primary_entry 无关）
        assert host._sidebar_item_ids == ["wp:dash"]

    def test_primary_entry_refresh_idempotent(self, host, fresh_registry):
        """重复 refresh：派生 tab 清旧重建，不重复堆积"""
        fresh_registry.register_workspace_page("demo", "dash", "D", _Page, metadata={"primary_entry": "titlebar"})
        host.refresh_pages()
        host.refresh_pages()
        host.refresh_pages()
        assert len(fresh_registry.get_titlebar_tabs()) == 1

    def test_primary_entry_tab_gone_after_unload_refresh(self, host, fresh_registry):
        """插件卸载 + refresh：派生 tab 同步注销"""
        fresh_registry.register_workspace_page("demo", "dash", "D", _Page, metadata={"primary_entry": "titlebar"})
        host.refresh_pages()
        assert fresh_registry.get_titlebar_tabs() != []
        fresh_registry.unload_plugin("demo")
        host.refresh_pages()
        assert fresh_registry.get_titlebar_tabs() == []
