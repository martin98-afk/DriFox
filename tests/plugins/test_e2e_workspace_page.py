# -*- coding: utf-8 -*-
"""e2e：注册→attach→show 全链路（mock _content_area 避开 Windows PySide6 offscreen 崩溃）"""

from unittest.mock import MagicMock

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

pytest.importorskip("PySide6.QtWidgets", reason="仅在 PySide6 环境加载 host")


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def fake_tab_win():
    """FakeTabWin 替身（plan 文档原版的 mock 版本）"""
    tw = MagicMock()
    tw._window_id = "w-e2e"
    children = []

    def _add(widget):
        idx = len(children)
        children.append(widget)
        return idx

    tw._content_area.addWidget.side_effect = _add
    tw._content_area.removeWidget = MagicMock()
    tw._content_area.setCurrentIndex = MagicMock()
    tw._content_area._children = children
    tw._build_ui_context = MagicMock(return_value={"window_id": "w-e2e", "services": {}})
    return tw


class TestE2E:
    def test_full_chain(self, fresh_registry, fake_tab_win):
        """注册→attach→show 全链路 + context 注入 + 激活索引"""
        from app.widgets.workspace_page_host import WorkspacePageHost

        seen = {}

        class _Page:
            def __init__(self, parent=None, context=None):
                seen["ctx"] = context
                seen["parent"] = parent

        fresh_registry.register_workspace_page("demo", "kanban", "看板", _Page, order_hint=10)
        host = WorkspacePageHost()
        host.attach_to(fake_tab_win)
        host.show_page("kanban")
        # 1. context 注入正确（window_id + services）
        assert seen["ctx"] == {"window_id": "w-e2e", "services": {}}
        # 2. parent 传入 _content_area
        assert seen["parent"] is fake_tab_win._content_area
        # 3. 页面已加载
        assert host.get_loaded_page_ids() == ["kanban"]
        # 4. _content_area.setCurrentIndex 被调（激活索引）
        fake_tab_win._content_area.setCurrentIndex.assert_called()

    def test_command_naming_and_registration(self, fresh_registry, fake_tab_win):
        """命令命名约定 + 注册到 CommandManager + FunctionCommandHandlers"""
        from app.widgets.workspace_page_host import WorkspacePageHost

        class _P:
            def __init__(self, parent=None, context=None):
                pass

        fresh_registry.register_workspace_page("demo", "kanban", "看板", _P)
        host = WorkspacePageHost()
        host.attach_to(fake_tab_win)
        # 命名约定：plugin_name:page_id
        from app.core.command_manager import CommandManager
        from app.core.builtin_commands import FunctionCommandHandlers

        assert CommandManager.get_instance().has_command("demo:kanban")
        assert FunctionCommandHandlers.has("demo:kanban")

    def test_unload_cleanup_chain(self, fresh_registry, fake_tab_win):
        """卸载链路：unload_plugin → teardown_plugin → 页面销毁 + 命令注销"""
        from app.widgets.workspace_page_host import WorkspacePageHost
        from app.core.command_manager import CommandManager

        class _P:
            def __init__(self, parent=None, context=None):
                pass

        fresh_registry.register_workspace_page("demo", "kanban", "看板", _P)
        host = WorkspacePageHost()
        host.attach_to(fake_tab_win)
        host.show_page("kanban")
        fresh_registry.unload_plugin("demo")
        host.teardown_plugin("demo")
        assert host.get_loaded_page_ids() == []
        assert not CommandManager.get_instance().has_command("demo:kanban")