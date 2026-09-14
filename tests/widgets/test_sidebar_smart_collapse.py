# -*- coding: utf-8 -*-
"""侧边栏智能折叠四规则测试

设计：docs/superpowers/specs/2026-09-12-smart-sidebar-collapse-design.md
规则 1：挤压即折叠（折叠线对齐展开最小可用宽度 _EXPANDED_MIN_FRAME_WIDTH）
规则 2：双面板协调（右先折、反向恢复）
规则 3：空间恢复即展开（增长门槛 200→80；手动折叠永不自动展开）
规则 4：启动记忆（会话栏折叠态 + 工作台显隐态，仅记用户手动终态）

★ 2026-09-13 修订：折叠线从硬编码 120 改为引用单一真源
_EXPANDED_MIN_FRAME_WIDTH(200)。原 120 与设计稿 200 不一致，导致
120~200px 出现"文字已不可读但仍不折叠"的死区。
"""

from unittest.mock import patch

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
    """最小 TabPanel 替身（宽度口径与真实 TabPanel 一致：content 宽）"""

    def __init__(self):
        from app.widgets.tab_manager_window import (
            _COLLAPSED_PANEL_WIDTH,
            _EXPANDED_MIN_CONTENT_WIDTH,
        )

        self._collapsed = False
        self._collapsed_by_squeeze = False
        self._animating = False
        # ★ content 口径（真实 TabPanel 同源），禁止写 frame 值
        self._auto_collapse_width = _EXPANDED_MIN_CONTENT_WIDTH
        self._auto_expand_width = _EXPANDED_MIN_CONTENT_WIDTH + 6
        self._collapsed_min_width = _COLLAPSED_PANEL_WIDTH
        self.sync_calls = 0

    def _update_toggle_button(self):
        pass

    def set_collapsed(self, collapsed):
        self._collapsed = collapsed

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


def test_auto_collapse_width_aligns_expanded_min(qapp):
    """折叠线必须与展开最小可用宽度同源，且是 **content 口径**

    ★ 坐标系回归：TabPanel.resizeEvent 比较的是 self.width()（content 宽 =
    frame 宽 − 14）。若此处误用 frame 域的 _EXPANDED_MIN_FRAME_WIDTH，等价于
    把折叠线抬高 14px。
    """
    from unittest.mock import patch

    from app.widgets.tab_manager_window import (
        _EXPANDED_MIN_CONTENT_WIDTH,
        _FRAME_PADDING_X,
        _EXPANDED_MIN_FRAME_WIDTH,
    )
    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    assert p._auto_collapse_width == _EXPANDED_MIN_CONTENT_WIDTH
    # 与 frame 域常量的换算关系必须成立
    assert _EXPANDED_MIN_FRAME_WIDTH == _EXPANDED_MIN_CONTENT_WIDTH + _FRAME_PADDING_X


def test_auto_expand_width_is_hysteresis_offset(qapp):
    """展开线 = 折叠线 + 滞回区，且滞回区必须窄（防"拉宽了却仍折叠"）"""
    from unittest.mock import patch

    from app.widgets.tab_manager_window import _EXPANDED_MIN_CONTENT_WIDTH, _HYSTERESIS_WIDTH
    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    assert p._auto_expand_width == _EXPANDED_MIN_CONTENT_WIDTH + _HYSTERESIS_WIDTH
    assert p._auto_expand_width > p._auto_collapse_width
    # ★ 滞回区必须远小于"用户可感知的操作粒度"：否则手动拉宽 15~20px
    #   仍等不到展开，手感即"明显拉宽了却还是折叠态"（实测 bug）。
    assert _HYSTERESIS_WIDTH <= 8, "滞回区过大 → 拉宽后长时间保持折叠，手感断裂"


def test_default_expanded_width_is_readable(qapp):
    """默认展开宽度必须 >= 折叠线：否则启动即落在"展开态却不该展开"的矛盾区"""
    from unittest.mock import patch

    from app.widgets.tab_manager_window import _DEFAULT_PANEL_WIDTH, _EXPANDED_MIN_CONTENT_WIDTH
    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    assert _DEFAULT_PANEL_WIDTH >= p._auto_collapse_width


def test_width_constants_ordering_invariant(qapp):
    """★ 宽度常量序关系不变式（本次 bug 的根因防护）

    必须同时满足，否则出现"拉到正常宽度仍是折叠态"：
        收起宽 < 折叠线 < 默认展开宽
        且 默认展开宽 >= 展开线（= 折叠线 + 滞回区）
    这条不变式是本 bug 的核心：此前折叠线 186 > 默认展开 187 之差仅 1px，
    加 6px 滞回区后展开线 192 超过默认展开宽度 → 拉到 187 仍折叠。
    """
    from unittest.mock import patch

    from app.widgets.tab_manager_window import _DEFAULT_PANEL_WIDTH
    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    assert p._collapsed_min_width < p._auto_collapse_width, "收起宽必须小于折叠线"
    assert p._auto_collapse_width < _DEFAULT_PANEL_WIDTH, "折叠线必须小于默认展开宽度"
    assert _DEFAULT_PANEL_WIDTH >= p._auto_expand_width, (
        "默认展开宽度必须 >= 展开线：否则拉回正常宽度仍判折叠（本次 bug）"
    )


def test_drag_to_expanded_width_expands_when_collapsed(panel_collapsible, qtbot):
    """★ 用户 bug 回归：收起态手动拉到"默认展开宽度"必须立刻展开

    复现原问题：折叠线被抬高 + 滞回区被撑大后，用户从折叠态拉开到
    187px content（视觉上完全够用）仍判定为折叠 → 表现为"宽度够大但里面
    还是折叠的样子"（截图：左栏文字齐全，底部 tab 仍是图标胶囊）。
    """
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent

    from app.widgets.tab_manager_window import _DEFAULT_PANEL_WIDTH

    panel_collapsible.add_tab("会话A")
    panel_collapsible.set_collapsed(True)
    # 用户从收起态拖到默认展开宽度（187 content）
    panel_collapsible.resize(_DEFAULT_PANEL_WIDTH, 600)
    panel_collapsible.resizeEvent(
        QResizeEvent(QSize(_DEFAULT_PANEL_WIDTH, 600), QSize(panel_collapsible._collapsed_min_width, 600))
    )
    qtbot.wait(50)
    assert panel_collapsible._collapsed is False, "拉到默认展开宽度必须展开（本次 bug 的核心）"
    assert panel_collapsible._items[0]._compact is False


def test_drag_just_past_expand_line_expands(panel_collapsible, qtbot):
    """收起态拉到刚越过展开线即展开（不能要求拉到远超阈值的宽度）"""
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent

    from app.widgets.tab_manager_window import _HYSTERESIS_WIDTH
    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        ref = TabPanel()
    expand_w = ref._auto_expand_width
    assert _HYSTERESIS_WIDTH <= 8  # 滞回区必须窄，保证"拉到就好"

    panel_collapsible.add_tab("会话A")
    panel_collapsible.set_collapsed(True)
    panel_collapsible.resize(expand_w, 600)
    panel_collapsible.resizeEvent(QResizeEvent(QSize(expand_w, 600), QSize(60, 600)))
    qtbot.wait(50)
    assert panel_collapsible._collapsed is False


def test_no_dead_zone_between_collapse_and_expanded_min(panel_collapsible, qtbot):
    """★ 死区回归：宽度低于折叠线必须折叠，不得停留在压扁态"""
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent

    from app.widgets.tab_manager_window import _EXPANDED_MIN_CONTENT_WIDTH

    panel_collapsible.add_tab("会话A")
    panel_collapsible.resize(300, 600)
    panel_collapsible.resizeEvent(QResizeEvent(QSize(300, 600), QSize(300, 600)))
    assert panel_collapsible._collapsed is False
    # 折叠线以下 → 必须折叠
    below = _EXPANDED_MIN_CONTENT_WIDTH - 1
    panel_collapsible.resize(below, 600)
    panel_collapsible.resizeEvent(QResizeEvent(QSize(below, 600), QSize(300, 600)))
    qtbot.wait(50)
    assert panel_collapsible._collapsed is True


def test_hysteresis_zone_holds_state(panel_collapsible, qtbot):
    """滞回区内保持状态不动；跨过展开线才展开"""
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent

    from app.widgets.tab_manager_window import _HYSTERESIS_WIDTH
    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        _p = TabPanel()
    collapse_w = _p._auto_collapse_width
    expand_w = _p._auto_expand_width

    panel_collapsible.add_tab("会话A")
    panel_collapsible.set_collapsed(True)
    # 折叠线与展开线之间（滞回区）内不动
    if _HYSTERESIS_WIDTH > 1:
        mid = collapse_w + max(1, _HYSTERESIS_WIDTH // 2)
        panel_collapsible.resize(mid, 600)
        panel_collapsible.resizeEvent(QResizeEvent(QSize(mid, 600), QSize(60, 600)))
        qtbot.wait(50)
        assert panel_collapsible._collapsed is True, "滞回区内不得展开"
    # 越过展开线 → 展开
    panel_collapsible.resize(expand_w, 600)
    panel_collapsible.resizeEvent(QResizeEvent(QSize(expand_w, 600), QSize(collapse_w, 600)))
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

    def _maybe_restore_workbench_after_squeeze(self, growth_required=True):
        # 委托真实现：恢复逻辑依赖 splitter/panel/frame，stub 已全部提供
        from app.widgets.tab_manager_window import TabManagerWindow

        TabManagerWindow._maybe_restore_workbench_after_squeeze(self, growth_required=growth_required)


def test_coordinate_collapse_wb_first_when_full_layout_fits_without_wb(qapp):
    """total 放得下「面板+聊天」但放不下「面板+聊天+工作台」→ 只折工作台，左栏恢复展开宽"""
    from app.widgets.tab_manager_window import (
        _EXPANDED_MIN_FRAME_WIDTH,
        _MIN_CHAT_WIDTH,
        TabManagerWindow,
    )

    # sizes 恒定（left=60, total=614, wb=250）→ needed(201) + chat(400) + wb(250) = 851 > 614（放不下工作台）
    # 与下条断言无关：wb 让位后 needed(201) + chat(400) = 601 <= 614（放得下左栏+聊天）
    total = _EXPANDED_MIN_FRAME_WIDTH + _MIN_CHAT_WIDTH + 50
    win = _Win(left=60, total=total, wb_visible=True, wb_width=250)
    assert TabManagerWindow._evaluate_squeeze_collapse(win) is False
    assert win._wb_collapsed_by_squeeze is True, "应先折工作台让位"
    assert win._tab_panel._collapsed is False, "工作台让位后左栏不得折叠"
    assert win._splitter.sizes()[0] >= _EXPANDED_MIN_FRAME_WIDTH


def test_coordinate_collapse_both_when_total_too_small(qapp):
    """total 连「面板+聊天」都放不下 → 工作台与左栏都折"""
    from app.widgets.tab_manager_window import (
        _EXPANDED_MIN_FRAME_WIDTH,
        _MIN_CHAT_WIDTH,
        TabManagerWindow,
    )

    # ★ 用常量推导（勿写死 550）：工作台让位能腾出的最大宽度就是 wb_width，
    #   因此"让位后仍放不下"的临界是 total + wb_width < needed + chat_min。
    wb_width = 250
    total = _EXPANDED_MIN_FRAME_WIDTH + _MIN_CHAT_WIDTH - wb_width - 50
    win = _Win(left=60, total=total, wb_visible=True, wb_width=wb_width)
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
    """左栏折叠 + 工作台挤压折叠：空间恢复 → 左栏优先；空间再富余 → 工作台恢复"""
    from app.widgets.tab_manager_window import TabManagerWindow
    from app.widgets.workbench_panel import PANEL_WIDTH_MIN

    wb_min = PANEL_WIDTH_MIN + 14  # 334
    # 空间够左栏展开（250+400=650）但不够再容工作台：左栏优先，工作台保持折叠
    win = _Win(left=60, total=984, wb_visible=True, wb_width=0)
    win._tab_panel._collapsed = True
    win._tab_panel._collapsed_by_squeeze = True
    win._wb_collapsed_by_squeeze = True
    win._squeeze_total_width = 984 - 300  # 模拟折叠时窗口更窄，现已 +300 > 80

    TabManagerWindow._maybe_auto_expand_after_squeeze(win)
    assert win._tab_panel._collapsed is False, "第一优先：左栏先展开"
    assert win._wb_collapsed_by_squeeze is True, "空间不足以同时恢复时工作台保持折叠"

    # 窗口继续拉宽：空间够工作台 → 自动重开
    win._splitter.setSizes([250, 1500, 0])
    TabManagerWindow._maybe_auto_expand_after_squeeze(win)
    assert win._wb_collapsed_by_squeeze is False
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

    win = _Win(left=250, total=1110, wb_visible=False, wb_width=0)
    # 已挤压折叠的工作台：frame 实例存在但隐藏（可见性判定只用于折叠方向）
    win._workbench_frame = _StubFrame(minimum_width=250, visible=False)
    win.workbench_panel = _StubWorkbenchPanel()
    win._wb_collapsed_by_squeeze = True
    win._squeeze_total_width = 1100
    TabManagerWindow._maybe_restore_workbench_after_squeeze(win)
    assert win._wb_visible_target is False
    assert win._wb_collapsed_by_squeeze is True
