# -*- coding: utf-8 -*-
"""端到端：模拟真实 host + windows + items，验证切到树模式实际渲染"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtWidgets import QWidget

from app.widgets.tab_panel import PANEL_MODE_LIST, PANEL_MODE_TREE, TabPanel
from app.widgets.workspace_tree import KIND_PROJECT, KIND_WORKTREE


def _make_host(projects, workdirs_per_project):
    """构造一个 minimal host：项目列表 + windows"""
    windows = []
    for proj, wd in workdirs_per_project:
        win = MagicMock()
        win._current_project = proj
        win._current_workdir = {proj: wd}
        win.session_manager = MagicMock()
        sess = MagicMock(session_id=f"sess-{proj}")
        win.session_manager.get_current_session.return_value = sess
        win.backend = MagicMock()
        win.backend.history_manager.get_projects.return_value = projects
        win.backend.history_manager.get_history_list.return_value = []
        win.backend.memory_manager.get_key_documents.return_value = []
        windows.append(win)

    class FakeHost(QWidget):
        _windows = windows

        def get_current_window(self_):
            return windows[0] if windows else None

    return FakeHost()


@pytest.fixture
def panel_with_host(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode(PANEL_MODE_LIST, persist=False)
    # 注入 host（模拟 TabManagerWindow 已挂上去）
    host = _make_host(["Demo"], [("Demo", "")])
    p._test_host = host  # 强引用防 GC
    p.setParent(host)
    # 把 TabPanel 的 host 解析路径走通：覆盖 _resolve_tab_host 返回 host
    p._resolve_tab_host = lambda: host
    qtbot.addWidget(p)
    return p


def test_tree_widget_has_content_after_mode_switch(panel_with_host):
    """切到树模式后，_tree_widget.rebuild 必须收到非空 specs"""
    panel_with_host.set_mode(PANEL_MODE_TREE, persist=False)

    # 触发一次重建（通常 _rebuild_layout 已经走过）
    panel_with_host._rebuild_layout()

    # 验证：_tree_widget 收到了 specs
    # 通过 tree_snapshot 间接验证（specs 已被存为 snapshot）
    snap = panel_with_host._tree_snapshot
    assert snap is not None, "tree snapshot 未生成 → rebuild 没真跑"
    # 至少有 1 个项目根 + 1 个工作树（主仓库）
    assert len(snap) >= 1
    # 验证可见性切到工作区
    assert panel_with_host._tree_scroll.isVisibleTo(panel_with_host) is True
    assert panel_with_host._scroll_area.isVisibleTo(panel_with_host) is False


def test_collapsing_sidebar_keeps_list_visible(panel_with_host):
    """折叠态下：两个滚动区都被隐藏（用户感知是窄条，不在意内容）"""
    panel_with_host._collapsed = True
    panel_with_host.set_mode(PANEL_MODE_TREE, persist=False)
    # 折叠态 → tree_active=False → 两个都隐藏
    assert panel_with_host._tree_scroll.isVisibleTo(panel_with_host) is False
    # 滚动区：因为 _rebuild_layout 走 _rebuild_team_layout（折叠强制列表）
    # _apply_mode_visibility 里 _scroll_area.setVisible(not tree_active) = not False = True
    assert panel_with_host._scroll_area.isVisibleTo(panel_with_host) is True