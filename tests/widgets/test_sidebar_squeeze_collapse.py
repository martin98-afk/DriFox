# -*- coding: utf-8 -*-
"""侧边栏"只在被挤压时才自动折叠"回归测试

Bug：最大化/还原（以及 resize 末尾 _force_relayout 的全量重算）会把左面板
瞬时压到折叠阈值（100px）以下，TabPanel.resizeEvent 只看宽度，把它误判为
"用户把面板拖窄"而自动折叠；折叠后窗口总宽往往不增反减，自动展开的相对
增长条件（≥ 折叠时总宽 +200）永不满足 → 折叠态永久残留。

修复：
1. resize 周期（含 relayout 瞬变）内 TabPanel 抑制自动折叠/展开；
2. 几何收拢后由 TabManagerWindow._evaluate_squeeze_collapse 按最终宽度判定，
   只有窗口确实放不下「常规展开宽度 + 聊天区最小宽度」才折叠。

本文件覆盖这两条，防止回归。
"""

import pytest

from app.widgets.tab_manager_window import _EXPANDED_MIN_FRAME_WIDTH, _MIN_CHAT_WIDTH, TabManagerWindow


class _StubSplitter:
    """最小 QSplitter 替身：只提供 count() / sizes() / setSizes()"""

    def __init__(self, sizes):
        self._sizes = list(sizes)

    def count(self):
        return len(self._sizes)

    def sizes(self):
        return list(self._sizes)

    def setSizes(self, sizes):
        self._sizes = list(sizes)


class _StubFrame:
    def __init__(self, maximum_width=414):
        self._max = maximum_width

    def maximumWidth(self):
        return self._max


class _StubPanel:
    """最小 TabPanel 替身：只提供折叠判定所需状态"""

    def __init__(self):
        self._collapsed = False
        self._collapsed_by_squeeze = False
        self._animating = False
        self._auto_collapse_width = 100
        self._auto_collapse_suppressed = False
        self.sync_calls = 0
        self.toggle_btn_calls = 0

    def _update_toggle_button(self):
        self.toggle_btn_calls += 1

    def sync_collapsed_ui(self):
        self.sync_calls += 1

    def set_auto_collapse_suppressed(self, suppressed):
        self._auto_collapse_suppressed = suppressed


class _StubWindow:
    """承载 _evaluate_squeeze_collapse 所需的最小宿主"""

    def __init__(self, left, total, saved_frame_width=None, max_frame_width=414):
        self._tab_panel = _StubPanel()
        self._splitter = _StubSplitter([left, max(0, total - left)])
        self._tab_frame = _StubFrame(max_frame_width)
        self._resize_blocking = False
        if saved_frame_width is not None:
            self._saved_panel_frame_width = saved_frame_width
        self.toggled = []

    def _on_sidebar_toggled(self, collapsed):
        self.toggled.append(collapsed)


@pytest.fixture
def panel(qtbot):
    from unittest.mock import patch

    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    qtbot.addWidget(p)
    return p


def test_transient_narrow_during_resize_transition_does_not_collapse(panel, qtbot):
    """resize/relayout 过渡期（瞬时压窄）不得自动折叠"""
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent

    panel.add_tab("会话A")
    panel.resize(250, 600)
    panel.resizeEvent(QResizeEvent(QSize(250, 600), QSize(250, 600)))
    assert panel._collapsed is False

    # 模拟 _force_relayout 瞬变：面板被压到 60px
    panel.set_auto_collapse_suppressed(True)
    panel.resize(60, 600)
    panel.resizeEvent(QResizeEvent(QSize(60, 600), QSize(250, 600)))
    qtbot.wait(50)
    assert panel._collapsed is False, "过渡期瞬时压窄不得被误判为'用户拖窄'"

    # 解除抑制后，用户真实拖窄才折叠
    panel.set_auto_collapse_suppressed(False)
    panel.resize(80, 600)
    panel.resizeEvent(QResizeEvent(QSize(80, 600), QSize(60, 600)))
    qtbot.wait(50)
    assert panel._collapsed is True


def test_transient_widen_during_resize_transition_does_not_expand(panel, qtbot):
    """过渡期瞬时变宽也不得自动展开（抑制是双向的）"""
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent

    panel.add_tab("会话A")
    panel.set_collapsed(True)
    panel.resize(60, 600)
    assert panel._collapsed is True

    panel.set_auto_collapse_suppressed(True)
    panel.resize(250, 600)
    panel.resizeEvent(QResizeEvent(QSize(250, 600), QSize(60, 600)))
    qtbot.wait(50)
    assert panel._collapsed is True, "过渡期瞬时变宽不得自动展开"


def test_evaluate_squeeze_collapse_skipped_when_space_is_enough(qapp):
    """空间够（只是 relayout 瞬时压窄）→ 不折叠，并把面板恢复到常规展开宽度"""
    win = _StubWindow(left=60, total=1200)
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is False
    assert win._tab_panel._collapsed is False
    assert win._tab_panel._collapsed_by_squeeze is False
    # 瞬时压窄被修复回常规展开宽度
    assert win._splitter.sizes()[0] >= _EXPANDED_MIN_FRAME_WIDTH


def test_evaluate_squeeze_collapse_noop_when_width_is_normal(qapp):
    """面板宽度正常 → 完全不动（不得覆盖用户手动拖出的宽度）"""
    win = _StubWindow(left=201, total=1200)
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is False
    assert win._splitter.sizes()[0] == 201
    assert win._tab_panel.sync_calls == 0


def test_evaluate_squeeze_collapse_triggers_when_truly_squeezed(qapp):
    """窗口确实放不下「展开宽度 + 聊天区最小宽度」→ 折叠"""
    total = _EXPANDED_MIN_FRAME_WIDTH + _MIN_CHAT_WIDTH - 50
    win = _StubWindow(left=60, total=total)
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is True
    assert win._tab_panel._collapsed is True
    assert win._tab_panel._collapsed_by_squeeze is True


def test_evaluate_squeeze_collapse_respects_collapsed_and_animating(qapp):
    """已折叠 / 动画进行中 → 跳过判定（避免打断动画与重复折叠）"""
    win = _StubWindow(left=60, total=500)
    win._tab_panel._collapsed = True
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is False

    win2 = _StubWindow(left=60, total=500)
    win2._tab_panel._animating = True
    assert TabManagerWindow._evaluate_squeeze_collapse(win2) is False
    assert win2._tab_panel._collapsed is False
