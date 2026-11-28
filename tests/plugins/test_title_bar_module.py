# -*- coding: utf-8 -*-
"""TitleBarModule：属性契约 + compose 集成

契约属性集（源 main_widget.setup_ui L2878-L3013 全量 self.* → host.setattr）：
- _project_branch_container _project_avatar _branch_widget
- _project_label title_edit _share_btn diff_btn
（共 7 项；其余 balance_display / coding_plan_ring / _coding_plan_hidden /
context_usage_ring 亦由 build 设置，此处聚焦会话栏契约）
注：极简化后 _pb_separator（三角连接符）已移除。
"""

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

pytest.importorskip("PyQt5.QtWidgets")

_CONTRACT_ATTRS = (
    "_project_branch_container",
    "_project_avatar",
    "_branch_widget",
    "_project_label",
    "title_edit",
    "_share_btn",
    "diff_btn",
)



def test_module_id():
    from app.widgets.modules.title_bar_module import TitleBarModule

    assert TitleBarModule.module_id == "title_bar"


def test_compose_builds_title_bar(fresh_registry, qapp):
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    from app.widgets.modules.title_bar_module import TitleBarModule
    from app.widgets.ui_composition import compose

    fresh_registry.register_ui_module("title_bar", TitleBarModule, plugin_name="system")

    class _Host(QWidget):
        pass

    host = _Host()

    def _ensure_layout(h):
        if h.layout() is None:
            QVBoxLayout(h)

    report = compose(host, ["title_bar"], root_layout_factory=_ensure_layout)

    assert report["title_bar"] == "system"
    assert not hasattr(host, "_history_questions_btn")
    for attr in _CONTRACT_ATTRS:
        assert hasattr(host, attr), f"missing host attribute: {attr}"


def test_current_chat_menu_dispatch(qapp, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from PyQt5.QtWidgets import QMenu, QVBoxLayout, QWidget

    from app.widgets.modules.title_bar_module import TitleBarModule

    host = QWidget()
    QVBoxLayout(host)
    session = SimpleNamespace(session_id="current", messages=[{"role": "user", "content": "hi"}])
    host.session_manager = Mock()
    host.session_manager.get_current_session.return_value = session
    host.history_manager = Mock()
    host.history_manager.get_pin_state.return_value = False
    host._is_streaming = False
    host._notify_history_data_changed = Mock()
    host._refresh_chat_tree = Mock()
    host._on_share_clicked = Mock()
    host._safe_duplicate_window = Mock()
    host._remove_current_chat = Mock()
    TitleBarModule().build(host)

    def click(label):
        def select(menu, pos):
            action = next(a for a in menu.actions() if a.text() == label)
            return action if action.isEnabled() else None

        monkeypatch.setattr(QMenu, "exec_", select)
        host._share_btn.click()

    click("置顶聊天")
    host.history_manager.set_session_pinned.assert_called_with("current", True)
    host.history_manager.get_pin_state.return_value = True
    click("取消置顶")
    host.history_manager.set_session_pinned.assert_called_with("current", False)
    click("分享对话")
    host._on_share_clicked.assert_called_once_with()
    click("创建聊天分支")
    host._safe_duplicate_window.assert_called_once_with(branch=True)
    click("归档")
    host._remove_current_chat.assert_called_with("current", permanent=False)
    click("删除")
    host._remove_current_chat.assert_called_with("current", permanent=True)
    host._is_streaming = True
    click("删除")
    assert host._remove_current_chat.call_count == 2
