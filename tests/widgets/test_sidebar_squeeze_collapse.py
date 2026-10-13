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


class _StubWorkbenchPanel:
    def set_panel_visible(self, visible):
        self._panel_visible = visible


class _StubPanel:
    """最小 TabPanel 替身：只提供折叠判定所需状态

    ★ 宽度口径与真实 TabPanel 一致：_auto_collapse_width 是 **content 宽**，
    _evaluate_squeeze_collapse 内部会 + _FRAME_PADDING_X 换算到 frame 域比较。
    """

    def __init__(self):
        from app.widgets.tab_manager_window import _EXPANDED_MIN_CONTENT_WIDTH

        self._collapsed = False
        self._collapsed_by_squeeze = False
        self._animating = False
        self._auto_collapse_width = _EXPANDED_MIN_CONTENT_WIDTH
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
    """承载 _evaluate_squeeze_collapse 所需的最小宿主

    wb_width > 0 时构造三窗格（左栏 | 对话区 | 工作台），并补齐工作台折叠/
    恢复路径所需的最小面（_workbench_frame / workbench_panel /
    _wb_visible_target / _workbench_frame_w）。
    """

    def __init__(self, left, total, saved_frame_width=None, max_frame_width=414, wb_width=0):
        self._tab_panel = _StubPanel()
        sizes = [left, max(0, total - left - wb_width)]
        if wb_width > 0:
            sizes.append(wb_width)
        self._splitter = _StubSplitter(sizes)
        self._tab_frame = _StubFrame(max_frame_width)
        self._resize_blocking = False
        if saved_frame_width is not None:
            self._saved_panel_frame_width = saved_frame_width
        self.toggled = []
        # 工作台状态（默认未展开：与真实现 __init__ 中 frame.hide() 一致）
        self._workbench_frame_w = 0
        self._wb_collapsed_by_squeeze = False
        self._wb_visible_target = False
        if wb_width > 0:
            self._workbench_frame = _StubFrame(maximum_width=834, visible=True)
            self.workbench_panel = _StubWorkbenchPanel()
            self._wb_visible_target = True
        else:
            self._workbench_frame = None
            self.workbench_panel = None

    def _on_sidebar_toggled(self, collapsed):
        self.toggled.append(collapsed)

    def set_workbench_visible(self, visible, animate=True, persist=False):
        self._wb_visible_target = visible

    def _splitter_sizes_with_left(self, left_w):
        """与 TabManagerWindow 真实现同构的最小版（stub 无工作台窗格，走两窗格分支）"""
        sizes = self._splitter.sizes()
        total = sum(sizes) if sizes else 0
        if len(sizes) >= 3:
            wb = sizes[2]
            return [left_w, max(0, total - left_w - wb), wb]
        return [left_w, max(0, total - left_w)]


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


def test_workbench_squeezed_below_min_collapses(qapp):
    """★ 双向对称（本次核心）：拖右把手把工作台压穿硬 min → 工作台自动折叠

    根因：QSplitter 拖右把手时对话区顶着 minimumSizeHint 不动，Qt 递归压
    远端左栏（离屏实测 [158,500,334] → [60,500,432]）；反向拖左把手时 Qt
    只压相邻对话区、不递归到工作台（→ [158,500,334] 不变）→ 右栏永不折。
    因此工作台只能在 _evaluate_squeeze_collapse 里显式判定。
    """
    from app.widgets.tab_manager_window import _FRAME_PADDING_X
    from app.widgets.workbench_panel import PANEL_WIDTH_MIN

    wb_min_frame = PANEL_WIDTH_MIN + _FRAME_PADDING_X  # 334
    # 左栏正常展开(414)，对话区被压到 100，工作台被压到 300（< 硬 min 334）
    win = _StubWindow(left=414, total=814, wb_width=300)
    assert win._splitter.sizes()[2] < wb_min_frame
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is True
    assert win._wb_collapsed_by_squeeze is True, "工作台被压穿必须折叠"
    assert win._wb_visible_target is False
    # 记忆折叠前宽度（供空间恢复时按原宽度重开）
    assert win._wb_saved_frame_width == 300
    assert win._workbench_frame_w == 300
    # 左栏不应被连带折叠（本次 bug 就是它被误折）
    assert win._tab_panel._collapsed is False


def test_workbench_not_collapsed_when_above_min(qapp):
    """工作台宽度在硬 min 之上 → 不折叠（不得把用户正常拖出的宽度收掉）"""
    from app.widgets.workbench_panel import PANEL_WIDTH_MIN

    from app.widgets.tab_manager_window import _FRAME_PADDING_X

    wb_min_frame = PANEL_WIDTH_MIN + _FRAME_PADDING_X
    # 左栏正常 + 工作台 400（> 334）：空间够，什么都不该动
    win = _StubWindow(left=414, total=1214, wb_width=400)
    assert win._splitter.sizes()[2] > wb_min_frame
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is False
    assert win._wb_collapsed_by_squeeze is False
    assert win._tab_panel._collapsed is False


def test_workbench_collapse_skipped_when_wb_hidden(qapp):
    """工作台未展开 → 无右栏可折，直接返回 False（不得误置挤压标记）"""
    win = _StubWindow(left=414, total=814)  # 无第三窗格
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is False
    assert getattr(win, "_wb_collapsed_by_squeeze", False) is False
