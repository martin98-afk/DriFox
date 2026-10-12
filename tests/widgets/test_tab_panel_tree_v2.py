# -*- coding: utf-8 -*-
"""工作区树 v2 结构回归：扁平化（去工作树层）+ 置顶跨分组 + 「工作区」标题行

覆盖：
- _collect_tree_specs 无 KIND_WORKTREE 节点（项目直挂会话）
- 会话行副行 = 预览 · 工作树名 · 相对时间
- 置顶跨分组：pinned 记录排项目下最前
- 「工作区」标题行：树模式可见 / 列表隐藏 + 「+」emit newProjectRequested
- 项目行无「分支」快捷按钮
"""

from unittest.mock import MagicMock, patch

import pytest

from app.widgets.tab_panel import PANEL_MODE_LIST, PANEL_MODE_TREE, TabPanel
from app.widgets.workspace_tree import KIND_PROJECT, KIND_SESSION, KIND_WORKTREE


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode(PANEL_MODE_LIST, persist=False)
    qtbot.addWidget(p)
    return p


def _make_history(records):
    """轻量历史管理器替身：返回给定记录列表"""
    hm = MagicMock()
    hm.get_projects.return_value = ["Demo"]
    hm.get_history_list.return_value = list(records)
    hm.get_key_documents.return_value = []
    return hm


def _make_host(history):
    win = MagicMock()
    win._current_project = "Demo"
    win._current_workdir = {}
    win.backend.history_manager = history
    win.backend.memory_manager = None
    win.session_manager.get_current_session.return_value = None
    host = MagicMock()
    host._windows = [win]
    host.get_current_window.return_value = win
    return host


def _rec(sid, title, pinned=False, time_str="2026-10-12 10:00:00", preview="", worktree=""):
    return {
        "session_id": sid,
        "title": title,
        "pinned": pinned,
        "last_time": time_str,
        "preview": preview,
        "project": "Demo",
        "worktree_path": worktree,
    }


def _collect(panel, records):
    history = _make_history(records)
    host = _make_host(history)
    with patch.object(panel, "_resolve_tab_host", return_value=host):
        return panel._collect_tree_specs()


def test_no_worktree_nodes(panel):
    """v2 扁平：项目直挂会话，无 KIND_WORKTREE 节点"""
    specs = _collect(panel, [_rec("s1", "会话一")])
    kinds = [s.kind for s in specs]
    assert KIND_WORKTREE not in kinds, "v2 不应再生成工作树节点"
    assert KIND_PROJECT in kinds
    assert KIND_SESSION in kinds


def test_session_subtitle_composed(panel):
    """会话行副行 = 预览 · 工作树名（非主仓库） · 相对时间"""
    specs = _collect(panel, [_rec("s1", "会话一", preview="帮我看看这段代码")])
    sess = [s for s in specs if s.kind == KIND_SESSION]
    assert len(sess) == 1
    sub = sess[0].subtitle
    assert "帮我看看这段代码" in sub
    assert "·" in sub


def test_pinned_sorts_first(panel):
    """置顶跨分组：pinned 记录排项目下最前（在非置顶之前）"""
    records = [
        _rec("s1", "普通会话", time_str="2026-10-12 12:00:00"),
        _rec("s2", "置顶会话", pinned=True, time_str="2026-10-12 08:00:00"),
    ]
    specs = _collect(panel, records)
    sess = [s for s in specs if s.kind == KIND_SESSION]
    assert sess[0].title == "置顶会话", "置顶应排最前（即使时间更早）"


def test_project_has_no_branch_action(panel):
    """项目行不再带「分支」快捷按钮（管理工作树入口随工作树层一起退役）"""
    specs = _collect(panel, [])
    proj = [s for s in specs if s.kind == KIND_PROJECT][0]
    assert proj.actions == (), f"项目行 actions 应为空，实际 {proj.actions}"


def test_tree_header_visibility_and_signal(panel, qtbot):
    """「工作区」标题行：树模式可见 / 列表隐藏；「+」emit newProjectRequested"""
    assert panel._tree_header.isHidden() is True, "列表模式标题行应隐藏"

    with (
        patch.object(panel, "_warn_tree_mode_in_dev"),
        patch.object(panel, "_collect_tree_specs", return_value=[]),
    ):
        panel.set_mode(PANEL_MODE_TREE, persist=False)
    assert panel._tree_header.isHidden() is False, "树模式标题行应可见"

    with qtbot.waitSignal(panel.newProjectRequested, timeout=1000):
        panel._tree_new_project_btn.click()

    panel.set_mode(PANEL_MODE_LIST, persist=False)
    assert panel._tree_header.isHidden() is True, "切回列表标题行应再隐藏"


def test_session_pin_toggle_writes_history(panel):
    """历史行图钉 → set_session_pinned 被调用 + 树刷新"""
    history = _make_history([_rec("s2", "会话")])
    host = _make_host(history)
    with (
        patch.object(panel, "_resolve_tab_host", return_value=host),
        patch.object(panel, "refresh_tree") as refresh_mock,
    ):
        panel._on_tree_pin_session({"session_id": "s2"}, True)
    history.set_session_pinned.assert_called_once_with("s2", True)
    assert refresh_mock.called, "置顶后应刷新树（跨分组重排）"
