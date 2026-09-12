# -*- coding: utf-8 -*-
"""侧边栏智能折叠四规则测试

设计：docs/superpowers/specs/2026-09-12-smart-sidebar-collapse-design.md
规则 1：挤压即折叠（阈值 100→200）
规则 2：双面板协调（右先折、反向恢复）
规则 3：空间恢复即展开（增长门槛 200→80；手动折叠永不自动展开）
规则 4：启动记忆（会话栏折叠态 + 工作台显隐态，仅记用户手动终态）
"""

import pytest


class _StubSplitter:
    """最小 QSplitter 替身（与 test_sidebar_squeeze_collapse.py 同构）"""

    def __init__(self, sizes):
        self._sizes = list(sizes)

    def count(self):
        return len(self._sizes)

    def sizes(self):
        return list(self._sizes)

    def setSizes(self, sizes):
        self._sizes = list(sizes)


class _StubFrame:
    def __init__(self, maximum_width=414, minimum_width=0, visible=False):
        self._max = maximum_width
        self._min = minimum_width
        self._visible = visible

    def maximumWidth(self):
        return self._max

    def minimumWidth(self):
        return self._min

    def isVisible(self):
        return self._visible

    def isHidden(self):
        return not self._visible


class _StubPanel:
    """最小 TabPanel 替身"""

    def __init__(self):
        self._collapsed = False
        self._collapsed_by_squeeze = False
        self._animating = False
        self._auto_collapse_width = 200
        self._collapsed_min_width = 46
        self.sync_calls = 0

    def _update_toggle_button(self):
        pass

    def sync_collapsed_ui(self):
        self.sync_calls += 1


class _StubWorkbenchPanel:
    def set_panel_visible(self, visible):
        self._panel_visible = visible


@pytest.fixture
def panel_collapsible(qtbot):
    from unittest.mock import patch

    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    qtbot.addWidget(p)
    return p


# ── 规则 1：挤压即折叠 ──


def test_auto_collapse_width_raised_to_200(qapp):
    from unittest.mock import patch

    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    assert p._auto_collapse_width == 200


def test_resize_to_199_collapses(panel_collapsible, qtbot):
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent

    panel_collapsible.add_tab("会话A")
    panel_collapsible.resize(250, 600)
    panel_collapsible.resizeEvent(QResizeEvent(QSize(250, 600), QSize(250, 600)))
    assert panel_collapsible._collapsed is False
    panel_collapsible.resize(199, 600)
    panel_collapsible.resizeEvent(QResizeEvent(QSize(199, 600), QSize(250, 600)))
    qtbot.wait(50)
    assert panel_collapsible._collapsed is True


def test_still_expanded_at_210(panel_collapsible, qtbot):
    """滞回区：折叠态拉到 210 展开；210 以下保持折叠"""
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent

    panel_collapsible.add_tab("会话A")
    panel_collapsible.set_collapsed(True)
    panel_collapsible.resize(205, 600)  # 滞回区（200~209）内不动
    panel_collapsible.resizeEvent(QResizeEvent(QSize(205, 600), QSize(60, 600)))
    qtbot.wait(50)
    assert panel_collapsible._collapsed is True
    panel_collapsible.resize(210, 600)
    panel_collapsible.resizeEvent(QResizeEvent(QSize(210, 600), QSize(205, 600)))
    qtbot.wait(50)
    assert panel_collapsible._collapsed is False


# ── 规则 2：双面板协调（窗口级，stub 宿主测 _evaluate_squeeze_collapse）──


class _Win:
    """构造带可选工作台窗格的 stub 宿主（unbound 调用真实方法用）"""

    def __init__(self, left, total, wb_visible=False, wb_width=0):
        sizes = [left, max(0, total - left - wb_width)]
        if wb_visible:
            sizes.append(wb_width)
        self._tab_panel = _StubPanel()
        self._splitter = _StubSplitter(sizes)
        self._tab_frame = _StubFrame(414)
        self._saved_panel_frame_width = 250
        self.toggled = []
        self._wb_collapsed_by_squeeze = False
        if wb_visible:
            self._workbench_frame = _StubFrame(minimum_width=250, visible=True)
            self.workbench_panel = _StubWorkbenchPanel()
            self._wb_visible_target = True
        else:
            self._workbench_frame = None
            self.workbench_panel = None
            self._wb_visible_target = False

    def _on_sidebar_toggled(self, collapsed):
        self.toggled.append(collapsed)

    def _splitter_sizes_with_left(self, left_w):
        sizes = self._splitter.sizes()
        total = sum(sizes) if sizes else 0
        if len(sizes) >= 3:
            wb = sizes[2]
            return [left_w, max(0, total - left_w - wb), wb]
        return [left_w, max(0, total - left_w)]

    def _collapse_workbench_by_squeeze(self):
        # 模拟瞬切收起：工作台宽度释放给中间区（与真实 frame.hide 后的重分配同构）
        self._wb_collapsed_by_squeeze = True
        sizes = self._splitter.sizes()
        if len(sizes) >= 3:
            self._splitter.setSizes([sizes[0], sizes[1] + sizes[2], 0])
        self.set_workbench_visible(False, animate=False)

    def set_workbench_visible(self, visible, animate=True, persist=False):
        self._wb_visible_target = visible


def test_coordinate_collapse_wb_first_when_full_layout_fits_without_wb(qapp):
    """total 放得下「面板+聊天」但放不下「面板+聊天+工作台」→ 只折工作台，左栏恢复展开宽"""
    from app.widgets.tab_manager_window import (
        _EXPANDED_MIN_FRAME_WIDTH,
        _MIN_CHAT_WIDTH,
        TabManagerWindow,
    )

    total = _EXPANDED_MIN_FRAME_WIDTH + _MIN_CHAT_WIDTH + 50  # 650：<850（含工作台）但 >=600
    win = _Win(left=60, total=total, wb_visible=True, wb_width=250)
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is False
    assert win._wb_collapsed_by_squeeze is True, "应先折工作台让位"
    assert win._tab_panel._collapsed is False, "工作台让位后左栏不得折叠"
    assert win._splitter.sizes()[0] >= _EXPANDED_MIN_FRAME_WIDTH


def test_coordinate_collapse_both_when_total_too_small(qapp):
    """total 连「面板+聊天」都放不下 → 工作台与左栏都折"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _Win(left=60, total=550, wb_visible=True, wb_width=250)
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is True
    assert win._wb_collapsed_by_squeeze is True
    assert win._tab_panel._collapsed is True
    assert win._tab_panel._collapsed_by_squeeze is True


def test_no_wb_behavior_unchanged(qapp):
    """无工作台窗格：行为与旧版完全一致（防回归）"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _Win(left=60, total=550)
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is True
    assert win._tab_panel._collapsed is True


def test_enough_space_restores_left_width(qapp):
    """total 充裕（含工作台也放得下）→ 不折叠任何面板，左栏恢复展开宽"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _Win(left=60, total=1200, wb_visible=True, wb_width=250)
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is False
    assert win._wb_collapsed_by_squeeze is False
    assert win._tab_panel._collapsed is False


# ── 规则 3：空间恢复即展开（含工作台反向恢复）──


def test_auto_expand_growth_lowered(qapp):
    from app.widgets.tab_manager_window import _AUTO_EXPAND_GROWTH

    assert _AUTO_EXPAND_GROWTH == 80


def test_maybe_expand_left_first_then_workbench(qapp):
    """左栏折叠 + 工作台挤压折叠：空间恢复 → 先展左栏；再触发一次 → 展工作台"""
    from app.widgets.tab_manager_window import (
        _EXPANDED_MIN_FRAME_WIDTH,
        _MIN_CHAT_WIDTH,
        TabManagerWindow,
    )
    from app.widgets.workbench_panel import PANEL_WIDTH_MIN

    wb_min = PANEL_WIDTH_MIN + 14
    total = _EXPANDED_MIN_FRAME_WIDTH + _MIN_CHAT_WIDTH + wb_min + 300
    win = _Win(left=60, total=total, wb_visible=True, wb_width=0)
    win._tab_panel._collapsed = True
    win._tab_panel._collapsed_by_squeeze = True
    win._wb_collapsed_by_squeeze = True
    win._squeeze_total_width = total - 300  # 模拟折叠时窗口更窄，现已 +300 > 80

    set_sizes_calls = []
    orig = win._splitter.setSizes

    def _track(sizes):
        set_sizes_calls.append(list(sizes))
        orig(sizes)

    win._splitter.setSizes = _track
    TabManagerWindow._maybe_auto_expand_after_squeeze(win)
    assert win._tab_panel._collapsed is False, "第一优先：左栏先展开"
    assert win._wb_collapsed_by_squeeze is True, "同一轮不得同时展开（先左后右）"

    TabManagerWindow._maybe_auto_expand_after_squeeze(win)
    assert win._wb_collapsed_by_squeeze is False, "第二轮：左栏已展，工作台恢复"
    assert win._wb_visible_target is True


def test_manual_collapse_never_auto_expands(qapp):
    """手动折叠（squeeze 标记为 False）→ 永不自动展开"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _Win(left=60, total=1200)
    win._tab_panel._collapsed = True
    win._tab_panel._collapsed_by_squeeze = False
    win._squeeze_total_width = 1000
    TabManagerWindow._maybe_auto_expand_after_squeeze(win)
    assert win._tab_panel._collapsed is True


def test_wb_manual_close_never_auto_reopens(qapp):
    """工作台手动关闭（无挤压标记）→ 空间恢复也不重开"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _Win(left=250, total=1200, wb_visible=True, wb_width=264)
    win._wb_collapsed_by_squeeze = False
    win._squeeze_total_width = 900
    TabManagerWindow._maybe_restore_workbench_after_squeeze(win)
    assert win._wb_visible_target is True


def test_wb_squeezed_reopens_when_space_enough(qapp):
    """工作台挤压折叠 + 左栏已展开 + 空间够 → 自动重开"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _Win(left=250, total=1200, wb_visible=True, wb_width=0)
    win._wb_collapsed_by_squeeze = True
    win._squeeze_total_width = 1100
    TabManagerWindow._maybe_restore_workbench_after_squeeze(win)
    assert win._wb_visible_target is True
    assert win._wb_collapsed_by_squeeze is False


def test_wb_stays_collapsed_when_space_not_enough(qapp):
    """窗口只比折叠时宽 10（<80 增长门槛）→ 工作台保持折叠"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _Win(left=250, total=1110, wb_visible=True, wb_width=0)
    win._wb_collapsed_by_squeeze = True
    win._squeeze_total_width = 1100
    TabManagerWindow._maybe_restore_workbench_after_squeeze(win)
    assert win._wb_visible_target is False
    assert win._wb_collapsed_by_squeeze is True
