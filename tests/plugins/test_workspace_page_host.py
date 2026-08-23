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
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


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