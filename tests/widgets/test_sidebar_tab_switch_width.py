# -*- coding: utf-8 -*-
"""切页保宽恢复 + left 卡片空间不足让位 回归测试

Bug 1（切页宽度污染）：工作台显隐是 per-tab 记忆，切页瞬切时 QSplitter
重分配空间，窗口放不下会把左栏压窄并触发 TabPanel 同步折叠；切回后释放
空间全归对话区，左栏宽度被永久污染（挤压自动展开要求窗口总宽增长 ≥80px，
布局性释放不触发）。修复：切页前记录左栏宽度/折叠态，切页后按「不变」恢复。

Bug 2（卡片挤成竖条）：窗口空间不足时打开 left 卡片，_left_card_frame_target
把 frame 钳到 TabPanel 收起宽附近，卡片区被挤成竖条不可读。修复：打开前
工作台让位（规则 2 同源），仍不足时保卡片最小可用宽。
"""

import pytest

from app.widgets.tab_manager_window import (
    _CHAT_ABSOLUTE_MIN_WIDTH,
    _COLLAPSED_PANEL_WIDTH,
    _FRAME_PADDING_X,
    _LEFT_CARD_MIN_VISIBLE_WIDTH,
    _MIN_CHAT_WIDTH,
    TabManagerWindow,
)


class _StubSplitter:
    """最小 QSplitter 替身：只提供 sizes() / setSizes()"""

    def __init__(self, sizes):
        self._sizes = list(sizes)

    def sizes(self):
        return list(self._sizes)

    def setSizes(self, sizes):
        self._sizes = list(sizes)


class _StubPanel:
    """最小 TabPanel 替身：只提供恢复逻辑所需状态"""

    def __init__(self):
        self._collapsed = False
        self._collapsed_by_squeeze = False
        self._animating = False
        self._left_card_present = False
        self._auto_collapse_suppressed = False
        self.sync_calls = 0

    def sync_collapsed_ui(self):
        self.sync_calls += 1

    def set_auto_collapse_suppressed(self, suppressed):
        self._auto_collapse_suppressed = suppressed


class _StubWindow:
    """承载 _restore_tab_frame_after_tab_switch 所需的最小宿主"""

    def __init__(self, sizes):
        self._tab_panel = _StubPanel()
        self._splitter = _StubSplitter(sizes)

    def _splitter_sizes_with_left(self, left_w):
        """与 TabManagerWindow 真实现同构（含工作台三窗格分支）"""
        sizes = self._splitter.sizes()
        total = sum(sizes)
        if len(sizes) >= 3:
            wb = sizes[2]
            return [left_w, max(0, total - left_w - wb), wb]
        return [left_w, max(0, total - left_w)]


class _StubTargetWindow:
    """_left_card_frame_target 所需最小宿主（sizes 存在时不读 width()）"""

    def __init__(self, sizes):
        self._splitter = _StubSplitter(sizes)


_BASE_FRAME_W = _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X  # 60


# ── Bug 1：切页保宽恢复 ──


def test_restore_after_squeeze_rebuilds_width(qapp):
    """切页被工作台挤压（左栏 414→160）：切回后恢复切前宽度"""
    win = _StubWindow([160, 1040, 0])
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, 414, False)
    assert win._splitter.sizes()[0] == 414
    assert win._splitter.sizes()[1] == 786  # 被挤压的宽度从对话区归还
    assert win._tab_panel._collapsed is False


def test_restore_rebuilds_collapsed_state(qapp):
    """切前折叠态、切页中被翻开（挤压链路残留）：还原折叠态与宽度，清挤压标记"""
    win = _StubWindow([160, 1040, 0])
    win._tab_panel._collapsed = True
    win._tab_panel._collapsed_by_squeeze = True
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, _BASE_FRAME_W, True)
    assert win._tab_panel._collapsed is True
    assert win._tab_panel._collapsed_by_squeeze is False  # 挤压标记清除
    assert win._splitter.sizes()[0] == _BASE_FRAME_W
    assert win._tab_panel.sync_calls == 0  # 折叠态一致，UI 形态无需刷新


def test_restore_noop_when_unchanged(qapp):
    """切页未挤压：零操作（不覆盖用户当前宽度）"""
    win = _StubWindow([414, 786, 0])
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, 414, False)
    assert win._splitter.sizes() == [414, 786, 0]
    assert win._tab_panel.sync_calls == 0


def test_restore_skipped_when_card_present(qapp):
    """卡片占位期宽度语义归卡片编排独占，不动"""
    win = _StubWindow([160, 1040, 0])
    win._tab_panel._left_card_present = True
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, 414, False)
    assert win._splitter.sizes()[0] == 160


def test_restore_clears_suppression(qapp):
    """切页期折叠抑制必须解除（否则后续用户真实拖窄不再折叠）"""
    win = _StubWindow([414, 786, 0])
    win._tab_panel._auto_collapse_suppressed = True
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, 414, False)
    assert win._tab_panel._auto_collapse_suppressed is False


def test_restore_aborts_when_saved_changed(qapp):
    """切页后 saved 被新编排改写（如用户重新拖拽）：快照过时，恢复放弃

    实测场景（探针 repro_sidebar_tab_width）：切页排队的 restore 撞上
    250ms 动画重试，执行时用户已拖出新宽度并更新 saved——过时的 prev
    会把新意图覆盖掉（saved 414 被打回 201，此后一切恢复失效）。
    """
    win = _StubWindow([414, 786, 0])
    win._tab_switch_saved_snapshot = 201  # 切页时 saved=201
    win._saved_panel_frame_width = 414  # 执行时已被新拖拽改写
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, 201, False)
    assert win._splitter.sizes()[0] == 414  # 新意图不被覆盖
    assert win._saved_panel_frame_width == 414


def test_restore_runs_when_snapshot_matches(qapp):
    """saved 未变（快照一致）：守卫放行，正常恢复"""
    win = _StubWindow([150, 1050, 0])
    win._tab_switch_saved_snapshot = 414
    win._saved_panel_frame_width = 414
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, 414, False)
    assert win._splitter.sizes()[0] == 414


def test_restore_runs_without_snapshot(qapp):
    """无快照（stub/旧路径）：守卫跳过，行为不变"""
    win = _StubWindow([150, 1050, 0])
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, 414, False)
    assert win._splitter.sizes()[0] == 414


def test_restore_ignores_invalid_prev_width(qapp):
    """prev_left_w<=0（splitter 未就绪）不动作"""
    win = _StubWindow([160, 1040, 0])
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, 0, False)
    assert win._splitter.sizes()[0] == 160


def test_restore_writes_back_saved_width_when_expanded(qapp):
    """展开态恢复后回写 _saved_panel_frame_width（供折叠→展开/挤压恢复复用）"""
    win = _StubWindow([150, 1050, 0])
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, 414, False)
    assert win._saved_panel_frame_width == 414


def test_restore_no_saved_writeback_when_collapsed(qapp):
    """折叠态恢复不写 saved（saved 语义是下次展开的宽度）"""
    win = _StubWindow([150, 1050, 0])
    win._tab_panel._collapsed = True
    TabManagerWindow._restore_tab_frame_after_tab_switch(win, _BASE_FRAME_W, True)
    assert not hasattr(win, "_saved_panel_frame_width")


# ── 切页恢复基准宽（Bug 1 二次修复：即时宽度已被污染的场景）──


def test_baseline_expanded_uses_saved_width(qapp):
    """展开态：即时宽被其他标签页工作台挤压污染（414→150）时，基准取意图宽 414"""
    win = _StubWindow([150, 1050, 0])
    win._saved_panel_frame_width = 414
    assert TabManagerWindow._tab_switch_baseline_width(win, 150, False) == 414


def test_baseline_expanded_falls_back_to_current(qapp):
    """展开态：saved 缺省（从未拖拽）→ 退化为即时宽，行为不变"""
    win = _StubWindow([201, 999, 0])
    assert TabManagerWindow._tab_switch_baseline_width(win, 201, False) == 201


def test_baseline_expanded_current_wider_than_saved(qapp):
    """展开态：即时宽大于 saved（saved 落后）→ 取即时宽"""
    win = _StubWindow([414, 786, 0])
    win._saved_panel_frame_width = 201
    assert TabManagerWindow._tab_switch_baseline_width(win, 414, False) == 414


def test_baseline_collapsed_uses_current_width(qapp):
    """折叠态：基准即即时宽（折叠宽就是意图），不受 saved 影响"""
    win = _StubWindow([60, 1140, 0])
    win._saved_panel_frame_width = 414
    assert TabManagerWindow._tab_switch_baseline_width(win, 60, True) == 60


# ── Bug 2：left 卡片 frame 目标宽 ──


def test_frame_target_full_space_no_clamp(qapp):
    """空间充足：目标 = 收起宽 + 卡片宽，不被钳"""
    win = _StubTargetWindow([60, 1340, 0])
    assert TabManagerWindow._left_card_frame_target(win, 360) == _BASE_FRAME_W + 360


def test_frame_target_with_workbench_enough(qapp):
    """工作台开着且放得下：cap = 总宽 - 工作台 - 聊天区最小宽，卡片满宽"""
    win = _StubTargetWindow([60, 846, 494])  # 总宽 1400，free=906，cap=506 > 420
    assert TabManagerWindow._left_card_frame_target(win, 360) == _BASE_FRAME_W + 360


def test_frame_target_keeps_card_usable_when_tight(qapp):
    """让位后（wb=0）仍偏窄（总宽 800）：聊天区保 400，卡片区 340 仍可用"""
    win = _StubTargetWindow([60, 740, 0])
    target = TabManagerWindow._left_card_frame_target(win, 360)
    assert target == 400
    assert target - _BASE_FRAME_W >= _LEFT_CARD_MIN_VISIBLE_WIDTH


def test_frame_target_chat_absolute_floor(qapp):
    """极端窄窗口（总宽 500）：聊天区压到绝对下限，卡片区仍 > 0（不再竖条）"""
    win = _StubTargetWindow([60, 440, 0])
    target = TabManagerWindow._left_card_frame_target(win, 360)
    # cap = max(60+240, 500-400)=300 → free-300=200 < 300 → 妥协 max(60, 200)=200
    assert target == 200
    assert target > _BASE_FRAME_W  # 卡片区仍 > 0，不再挤成竖条
    # 聊天区不低于绝对下限
    assert 500 - target >= _CHAT_ABSOLUTE_MIN_WIDTH - 1
    assert target - _BASE_FRAME_W < _MIN_CHAT_WIDTH  # 确属妥协路径
