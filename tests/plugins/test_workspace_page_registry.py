# -*- coding: utf-8 -*-
"""WorkspacePage 槽：注册/排序/覆盖/unload 清理"""

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry, WorkspacePageInfo


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


class _Page:
    pass


class _OtherPage:
    pass


class TestWorkspacePage:
    def test_register_and_get(self, fresh_registry):
        fresh_registry.register_workspace_page("demo", "dash", "仪表盘", _Page)
        pages = fresh_registry.get_workspace_pages()
        assert len(pages) == 1
        assert pages[0].page_id == "dash" and pages[0].widget_class is _Page

    def test_order_hint_sorting(self, fresh_registry):
        fresh_registry.register_workspace_page("demo", "b", "B", _Page, order_hint=20)
        fresh_registry.register_workspace_page("demo", "a", "A", _Page, order_hint=10)
        fresh_registry.register_workspace_page("demo", "c", "C", _Page, order_hint=10)  # 同 hint 注册序
        assert [p.page_id for p in fresh_registry.get_workspace_pages()] == ["a", "c", "b"]

    def test_same_page_id_override_last_wins(self, fresh_registry):
        fresh_registry.register_workspace_page("demo", "p", "旧", _Page)
        fresh_registry.register_workspace_page("other", "p", "新", _OtherPage)
        assert fresh_registry.get_workspace_pages()[0].plugin_name == "other"
        assert fresh_registry.get_workspace_page("p").title == "新"

    def test_unload_plugin_clears(self, fresh_registry):
        fresh_registry.register_workspace_page("demo", "dash", "仪表盘", _Page)
        fresh_registry.unload_plugin("demo")
        assert fresh_registry.get_workspace_pages() == []
        assert fresh_registry.get_workspace_page("dash") is None