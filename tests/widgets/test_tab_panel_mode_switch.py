# -*- coding: utf-8 -*-
"""验证 TabPanel 模式切换时两个滚动区的可见性 + 开发期提醒弹窗触发"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget

from app.widgets.tab_panel import PANEL_MODE_LIST, PANEL_MODE_TREE, TabPanel


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode(PANEL_MODE_LIST, persist=False)
    qtbot.addWidget(p)
    return p


@pytest.fixture
def silent_warning():
    """屏蔽开发期提醒弹窗（InfoDialog.exec_）以免阻塞测试。

    同时记录是否被调用，便于断言「切到树模式时弹窗触发、切回列表时不弹」。
    """
    fake = MagicMock()
    with patch("app.widgets.common_dialogs.InfoDialog", return_value=fake):
        yield fake


def _shown(w, parent):
    return w.isVisibleTo(parent)


def test_default_is_list_mode(panel):
    """默认是列表模式：_scroll_area 可见，_tree_scroll 隐藏"""
    assert panel.current_mode() == PANEL_MODE_LIST
    assert _shown(panel._scroll_area, panel) is True
    assert _shown(panel._tree_scroll, panel) is False


def test_switch_to_tree_mode_shows_tree(panel, silent_warning):
    """切到工作区树模式：_tree_scroll 显示，_scroll_area 隐藏；触发提醒弹窗"""
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    assert panel.current_mode() == PANEL_MODE_TREE
    assert _shown(panel._tree_scroll, panel) is True
    assert _shown(panel._scroll_area, panel) is False
    assert silent_warning.called, "切到工作区树模式应触发开发期提醒弹窗"


def test_switch_back_to_list_mode_shows_list(panel, silent_warning):
    """切回列表模式：_scroll_area 显示，_tree_scroll 隐藏；列表模式不弹提醒"""
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    assert _shown(panel._tree_scroll, panel) is True
    silent_warning.reset_mock()
    panel.set_mode(PANEL_MODE_LIST, persist=False)
    assert panel.current_mode() == PANEL_MODE_LIST
    assert _shown(panel._scroll_area, panel) is True
    assert _shown(panel._tree_scroll, panel) is False
    assert not silent_warning.called, "切回列表模式不应弹提醒"


def test_no_warning_on_same_mode_repeat(panel, silent_warning):
    """同模式重复点选（用户已经在 tree 又选 tree）不重复弹提醒"""
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    silent_warning.reset_mock()
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    assert not silent_warning.called, "目标 = 当前模式时不应弹提醒"


def test_top_title_switches_with_mode(panel, silent_warning):
    """顶部标题跟随模式切换：列表=「对话页」/树=「工作区」"""
    assert panel._sessions_label.text() == "对话页"
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    assert panel._sessions_label.text() == "工作区"
    panel.set_mode(PANEL_MODE_LIST, persist=False)
    assert panel._sessions_label.text() == "对话页"
