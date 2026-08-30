# -*- coding: utf-8 -*-
"""验证切到树模式时 _tree_widget 真的被重建并展示内容"""
from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtWidgets import QWidget

from app.widgets.tab_panel import PANEL_MODE_LIST, PANEL_MODE_TREE, TabPanel, TreeNodeSpec
from app.widgets.workspace_tree import KIND_WORKTREE, KIND_PROJECT


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode(PANEL_MODE_LIST, persist=False)
    qtbot.addWidget(p)
    return p


def _make_spec():
    return [
        TreeNodeSpec(key="project:Demo", kind=KIND_PROJECT, title="Demo", count=2),
        TreeNodeSpec(key="worktree:Demo|", kind=KIND_WORKTREE, title="主仓库", count=1),
    ]


def test_switch_to_tree_calls_rebuild_with_specs(panel):
    """切到树模式：_tree_widget.rebuild 必须被调用，且参数含有效 specs"""
    with patch.object(panel._tree_widget, "rebuild") as rebuild_mock:
        # 强制返回非空 specs（避免真实 _collect_tree_specs 在无 host 时退化）
        with patch.object(panel, "_collect_tree_specs", return_value=_make_spec()):
            panel.set_mode(PANEL_MODE_TREE, persist=False)
            assert rebuild_mock.called, "_tree_widget.rebuild 没被调用"
            specs = rebuild_mock.call_args[0][0]
            assert len(specs) >= 1
            assert any(s.kind == KIND_PROJECT for s in specs)


def test_switch_back_to_list_does_not_rebuild_tree(panel):
    """切回列表模式：不应再走树模式重建路径"""
    # 先切到树模式过一次，让快照就绪
    panel.set_mode(PANEL_MODE_TREE, persist=False)
    # 再切回列表；树模式重建应不再触发
    with patch.object(panel._tree_widget, "rebuild") as rebuild_mock:
        with patch.object(panel, "_collect_tree_specs", return_value=[]):
            panel.set_mode(PANEL_MODE_LIST, persist=False)
            assert not rebuild_mock.called, "列表模式不应触发 _tree_widget.rebuild"


def test_persist_writes_settings_on_mode_change(panel):
    """切模式应写入 Settings（下次启动恢复）"""
    with patch("app.widgets.tab_panel.Settings") as settings_cls:
        instance = MagicMock()
        instance.tab_panel_mode = MagicMock()
        settings_cls.get_instance.return_value = instance
        panel.set_mode(PANEL_MODE_TREE, persist=True)
        assert instance.tab_panel_mode.value == PANEL_MODE_TREE


def test_popup_triggers_set_mode(panel):
    """⋯ 按钮点击 → PanelModePopup → 选「工作区树模式」→ set_mode 被调"""
    # 模拟 popup 选模式
    panel._on_mode_btn_clicked()
    popup = panel._mode_popup
    assert popup is not None
    # 直接 emit modeSelected 信号模拟用户点击行
    popup.modeSelected.emit(PANEL_MODE_TREE)
    assert panel.current_mode() == PANEL_MODE_TREE
    assert panel._tree_scroll.isVisibleTo(panel) is True
    assert panel._scroll_area.isVisibleTo(panel) is False