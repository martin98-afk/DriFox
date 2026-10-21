# -*- coding: utf-8 -*-
"""验证 TabPanel 模式切换时两个滚动区的可见性 + 开发期提醒弹窗触发"""

from unittest.mock import MagicMock, patch

import pytest

from app.widgets.tab_panel import PANEL_MODE_LIST, PANEL_MODE_TREE, TabPanel


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode(PANEL_MODE_LIST, persist=False)
    qtbot.addWidget(p)
    return p


@pytest.fixture
def dialog_factory():
    """屏蔽开发期提醒弹窗（InfoDialog.exec_）以免阻塞测试。

    返回 (factory, instances)：
    - factory：patch 后的 InfoDialog 类，断言调用次数/参数
    - instances：list，记录历次返回的 dialog mock；exec_.called 表示「真正弹出过」
    """
    instances: list = []

    def _make_dialog(*a, **kw):
        m = MagicMock()
        instances.append(m)
        return m

    with patch("app.widgets.common_dialogs.InfoDialog", side_effect=_make_dialog) as factory:
        yield factory, instances


def _shown(w, parent):
    return w.isVisibleTo(parent)


def test_default_is_list_mode(panel):
    """默认是列表模式：_scroll_area 可见，_tree_scroll 隐藏"""
    assert panel.current_mode() == PANEL_MODE_LIST
    assert _shown(panel._scroll_area, panel) is True
    assert _shown(panel._tree_scroll, panel) is False


def test_switch_to_tree_mode_shows_tree(panel, dialog_factory):
    """切到工作区树模式：_tree_scroll 显示，_scroll_area 隐藏；触发提醒弹窗"""
    factory, instances = dialog_factory
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    assert panel.current_mode() == PANEL_MODE_TREE
    assert _shown(panel._tree_scroll, panel) is True
    assert _shown(panel._scroll_area, panel) is False
    assert factory.call_count == 1, "切到工作区树模式应触发一次开发期提醒弹窗"
    assert len(instances) == 1 and instances[0].exec_.called, "弹窗应被 exec_() 弹出"


def test_switch_back_to_list_mode_shows_list(panel, dialog_factory):
    """切回列表模式：_scroll_area 显示，_tree_scroll 隐藏；列表模式不弹提醒"""
    factory, instances = dialog_factory
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    assert _shown(panel._tree_scroll, panel) is True
    factory.reset_mock()
    instances.clear()
    panel.set_mode(PANEL_MODE_LIST, persist=False)
    assert panel.current_mode() == PANEL_MODE_LIST
    assert _shown(panel._scroll_area, panel) is True
    assert _shown(panel._tree_scroll, panel) is False
    assert factory.call_count == 0, "切回列表模式不应弹提醒"


def test_no_warning_on_same_mode_repeat(panel, dialog_factory):
    """同模式重复点选（用户已经在 tree 又选 tree）不重复弹提醒"""
    factory, _ = dialog_factory
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    factory.reset_mock()
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    assert factory.call_count == 0, "目标 = 当前模式时不应弹提醒"


def test_top_title_switches_with_mode(panel, dialog_factory):
    """顶部标题跟随模式切换：列表=「对话页」/树=「工作区」"""
    factory, _ = dialog_factory
    assert panel._sessions_label.text() == "对话页"
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    assert panel._sessions_label.text() == "工作区"
    panel.set_mode(PANEL_MODE_LIST, persist=False)
    assert panel._sessions_label.text() == "对话页"
