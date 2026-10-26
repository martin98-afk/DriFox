# -*- coding: utf-8 -*-
"""侧边栏收起动画：紧凑形态必须等动画结束才切（回归测试）

Bug：收起动画途中宽度掉到「收起宽 + 滞回」时就把整行切紧凑。切形态会让
icon 中心从展开位（约 28）跳到紧凑位（23），而宽度仍在继续变小 → 视觉上
icon 先往右跑、再跟着宽度往左回（折返感）。

修复：收起方向的跨阈值切换整体撤销（_sidebar_anim_ui_switched 起始置 True），
整段动画维持展开形态，形态切换只在 _on_sidebar_anim_finished 执行一次。
展开方向保留跨阈值提前切（文字早于宽度就位，避免窄条挤字）。
"""
from unittest.mock import patch

import pytest
from PyQt5.QtCore import QObject

from app.widgets.tab_manager_window import (
    _COLLAPSED_PANEL_WIDTH,
    _DEFAULT_PANEL_WIDTH,
    _FRAME_PADDING_X,
    TabManagerWindow,
)


class _StubPanel:
    """最小 TabPanel 替身：只记录紧凑形态同步次数"""

    def __init__(self):
        from app.widgets.tab_manager_window import _EXPANDED_MIN_CONTENT_WIDTH

        self._collapsed = False
        self._collapsed_by_squeeze = False
        self._animating = False
        self._dragging_splitter = False
        self._left_card_present = False
        self._auto_collapse_width = _EXPANDED_MIN_CONTENT_WIDTH
        self._auto_expand_width = _EXPANDED_MIN_CONTENT_WIDTH + 6
        self._collapsed_min_width = _COLLAPSED_PANEL_WIDTH
        self.sync_calls = 0

    def set_animating(self, animating: bool):
        self._animating = animating

    def sync_collapsed_ui(self):
        self.sync_calls += 1


class _StubWindow(QObject):
    """承载侧边栏动画回调的最小宿主（需是 QObject：动画以其为 parent）"""

    def __init__(self):
        super().__init__()
        self._tab_panel = _StubPanel()
        self._sidebar_anim = None
        self._saved_panel_frame_width = _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X
        self._maybe_expand_calls = 0
        self.applied_left_widths = []

    def setSizes(self, sizes):
        self.applied_left_widths.append(sizes[0])

    # ── 真实宿主被动画路径消费的最小接口 ──
    # 动画 valueChanged/finished 信号连接的正是这两个名字（见 _start_sidebar_anim），
    # stub 需以同样名字把调用转发到真实的 TabManagerWindow 实现。
    def _on_sidebar_anim_value(self, value):
        return TabManagerWindow._on_sidebar_anim_value(self, value)

    def _on_sidebar_anim_finished(self):
        return TabManagerWindow._on_sidebar_anim_finished(self)

    def _splitter_sizes_with_left(self, left_w):
        return [left_w, max(0, 800 - left_w)]

    @property
    def _splitter(self):
        """记录每次 setSizes 的落位宽度（供断言最终精确落位）"""
        return self

    def _set_windows_resize_preview_suppressed(self, suppressed: bool):
        self.suppressed = suppressed

    def _maybe_auto_expand_after_squeeze(self, growth_required: bool = True):
        self._maybe_expand_calls += 1
        return False

    def _post_expand_squeeze_check(self):
        return None

    def _evaluate_squeeze_collapse(self):
        return False


@pytest.fixture
def win(qapp):
    return _StubWindow()


def test_collapsing_animation_does_not_switch_ui_midway(win):
    """收起动画途中不得切紧凑形态（icon 不得先右后左）"""
    start = _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X  # 201
    end = _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X  # 60

    with patch("app.widgets.tab_manager_window.Animations.motion_enabled", return_value=True):
        TabManagerWindow._start_sidebar_anim(win, start, end, collapsing=True)
    anim = win._sidebar_anim
    assert anim is not None
    # 收起方向：起始即标记"已切换" → 跨阈值分支永不执行
    assert win._sidebar_anim_ui_switched is True

    # 逐帧喂值（模拟动画从 201 收到 60），中途不得触发形态同步
    for w in range(start, end - 1, -10):
        TabManagerWindow._on_sidebar_anim_value(win, float(w))
    assert win._tab_panel.sync_calls == 0, "收起动画途中不得切换紧凑形态"

    # 动画结束：形态切换此时才发生，且只发生一次
    TabManagerWindow._on_sidebar_anim_finished(win)
    assert win._tab_panel.sync_calls == 1, "收起形态须在动画结束后切换一次"
    anim.stop()


def test_expanding_animation_still_switches_ui_at_expand_line(win):
    """展开方向保留跨阈值提前切（文字早于宽度就位，避免窄条挤字）"""
    start = _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X  # 60
    end = _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X  # 201

    with patch("app.widgets.tab_manager_window.Animations.motion_enabled", return_value=True):
        TabManagerWindow._start_sidebar_anim(win, start, end, collapsing=False)
    assert win._sidebar_anim_ui_switched is False
    anim = win._sidebar_anim

    # 未到展开线：不切
    TabManagerWindow._on_sidebar_anim_value(win, float(start))
    assert win._tab_panel.sync_calls == 0
    # 越过展开线：切一次（后续帧不再重复切）
    TabManagerWindow._on_sidebar_anim_value(win, float(end))
    TabManagerWindow._on_sidebar_anim_value(win, float(end))
    assert win._tab_panel.sync_calls == 1
    anim.stop()


def test_collapse_without_motion_still_lands_compact_ui(win):
    """系统「减少动态效果」：瞬时落位也必须走同一条收尾，形态落地一次"""
    start = _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X
    end = _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X

    with patch("app.widgets.tab_manager_window.Animations.motion_enabled", return_value=False):
        TabManagerWindow._start_sidebar_anim(win, start, end, collapsing=True)
    assert win._tab_panel.sync_calls >= 1, "无动效路径仍须落地紧凑形态"
