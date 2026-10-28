# -*- coding: utf-8 -*-
"""回归：away 状态机只认真实用户滚动输入。

背景（用户报障）：多个标签页时，后台标签页在流式输出，切回来消息列表停在
旧位置，必须手动滚到底。机制链条：

1. QStackedWidget 隐藏页里 Qt 完全停更滚动条——``maximum()`` 冻结、
   ``setValue()`` 被静默钳制（``test_tab_reshow_scroll_calibration.py``
   有离屏探针实测数据），后台增长不会反映到滚动条。
2. 切回时 ``showEvent`` 会调 ``_calibrate_scroll_on_reshow`` 补一次贴底，
   但它唯一闸门是 ``_should_follow_bottom()``。
3. 旧判据把**任何**非程序 valueChanged 都当作「用户滚离底部」。Qt 自己
   也会挪 value（切回标签页重算上界、分栏 / 宽度重排、卡片收缩钳制），
   一次误判置位 away 后，只能靠「再次 valueChanged 且贴底」复位——后台
   标签页里这两种事件都可能不再发生，标志永久卡死 → 校准被静默拦下。

修复：away 置位要求「最近有真实滚动输入」（滚轮 / 触控板 / 拖滑块 /
键盘翻页），由 ``_wire_user_scroll_input`` 接线、``_mark_user_scroll_input``
记录时间窗。
"""

import time
from unittest.mock import MagicMock

# 注意：不得在此插入 "app" 到 sys.path——app/plugins（regular package）会
# 劫持顶层 `plugins.*` 命名空间解析。仓库根已由 pytest rootdir 提供。
from app.main_widget import OpenAIChatToolWindow  # noqa: E402


def _make_win(**overrides):
    """构造 `_on_scroll_changed` / 用户输入标记可运行的最小实例"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    # ⚠️ PyQt 对象未经 __init__ 时 getattr(..., 默认值) 也会抛 RuntimeError，
    # 凡走守卫的字段必须显式给值。
    win._is_destroyed = False
    win._loading_session = False
    win._user_intentionally_away_from_bottom = False
    win._programmatic_scroll_depth = 0
    win._user_scroll_input_until = 0.0
    win._user_scroll_input_wired = False
    win._bottom_anchor_deadline = 0.0
    win._bottom_anchor_timer = MagicMock()
    win._virtual_scroll_timer = MagicMock()
    win._scroll_sync_timer = MagicMock()
    win._load_more_history_batches = MagicMock()
    win._history_load_threshold = 48
    scroll_bar = MagicMock()
    scroll_bar.maximum.return_value = 1000
    scroll_bar.value.return_value = 400  # 离底 600px
    vbar = MagicMock()
    area = MagicMock()
    area.verticalScrollBar.return_value = scroll_bar
    area.vScrollBar = vbar
    # _landed_bottom_value = min(maximum, container.height - viewport.height)
    landed_container = MagicMock()
    landed_container.height.side_effect = lambda: scroll_bar.maximum()
    area.widget.return_value = landed_container
    viewport = MagicMock()
    viewport.height.return_value = 0
    area.viewport.return_value = viewport
    win.chat_scroll_area = area
    for key, value in overrides.items():
        setattr(win, key, value)
    return win


# ─── 1. 核心语义：非用户输入不得置 away ─────────────────────────


def test_geometry_change_without_input_does_not_set_away():
    """Qt 自发改 value（切回标签页 / 重排 / 钳制）→ 不得记成用户滚离"""
    win = _make_win()
    win._on_scroll_changed(400)
    assert win._user_intentionally_away_from_bottom is False


def test_user_input_then_scroll_sets_away():
    """真实滚轮 / 拖动后离底 → 仍置 away（守卫不得被废掉）"""
    win = _make_win()
    win._mark_user_scroll_input()
    win._on_scroll_changed(400)
    assert win._user_intentionally_away_from_bottom is True


def test_input_window_expires():
    """输入窗口过期后的离底不算用户意图"""
    win = _make_win(_user_scroll_input_until=time.monotonic() - 1.0)
    win._on_scroll_changed(400)
    assert win._user_intentionally_away_from_bottom is False


def test_scroll_back_to_bottom_resets_away_without_input():
    """贴底复位不要求输入证据：程序置底 / 用户回到底部都要能清 away"""
    win = _make_win(_user_intentionally_away_from_bottom=True)
    win.chat_scroll_area.verticalScrollBar().value.return_value = 1000
    win._on_scroll_changed(1000)
    assert win._user_intentionally_away_from_bottom is False


def test_programmatic_scroll_exempt_even_with_input():
    """程序滚动豁免优先级高于用户输入标记（不因刚滚过轮就误伤置底补偿）"""
    win = _make_win()
    win._mark_user_scroll_input()
    with win._programmatic_scroll():
        win._on_scroll_changed(400)
    assert win._user_intentionally_away_from_bottom is False


# ─── 2. 输入归属：只有本窗口聊天滚动条算 ────────────────────────


def test_wheel_on_chat_scroll_bar_marks_input():
    win = _make_win()
    bar = win.chat_scroll_area.verticalScrollBar()
    win._mark_user_scroll_input_if_scroll_bar(bar)
    assert win._has_recent_user_scroll_input() is True


def test_wheel_on_visible_scroll_bar_marks_input():
    """可见条（SmoothScrollBar，用户实际拖动的那个）同样算"""
    win = _make_win()
    win._mark_user_scroll_input_if_scroll_bar(win.chat_scroll_area.vScrollBar)
    assert win._has_recent_user_scroll_input() is True


def test_wheel_on_viewport_marks_input():
    """viewport 必须算：高精度滚轮（触控板 / 非 120 倍数）只在这一层可见"""
    win = _make_win()
    win._mark_user_scroll_input_if_scroll_bar(win.chat_scroll_area.viewport())
    assert win._has_recent_user_scroll_input() is True


def test_wheel_on_other_widget_ignored():
    """其它对象上的滚轮不得标记（eventFilter 挂在多个对象上）"""
    win = _make_win()
    win._mark_user_scroll_input_if_scroll_bar(object())
    assert win._has_recent_user_scroll_input() is False


# ─── 3. 各输入通道 ──────────────────────────────────────────────


def test_action_triggered_marks_input():
    """键盘翻页 / 点页槽：QAbstractSlider.actionTriggered"""
    win = _make_win()
    win._on_scroll_action_triggered(0)
    assert win._has_recent_user_scroll_input() is True


def test_slider_press_holds_long_window():
    """拖滑块期间时长不可控，按长窗口续期"""
    win = _make_win()
    win._on_scroll_slider_pressed()
    assert win._user_scroll_input_until > time.monotonic() + 5.0
    win._on_scroll_slider_released()
    assert win._user_scroll_input_until < time.monotonic() + 5.0


# ─── 4. 接线契约：两套滚动条都要挂上 ────────────────────────────


def test_wire_user_scroll_input_connects_both_bars():
    win = _make_win()
    bar = win.chat_scroll_area.verticalScrollBar()
    vbar = win.chat_scroll_area.vScrollBar
    viewport = win.chat_scroll_area.viewport()
    win._wire_user_scroll_input()
    assert bar.installEventFilter.called, "内置条要收 Wheel / MouseButtonPress"
    assert bar.actionTriggered.connect.called, "内置条要接 actionTriggered"
    assert viewport.installEventFilter.called, "viewport 要收高精度滚轮"
    assert vbar.installEventFilter.called, "可见条要收拖动事件"
    assert vbar.sliderPressed.connect.called
    assert vbar.sliderReleased.connect.called
    assert win._user_scroll_input_wired is True


def test_wire_user_scroll_input_is_idempotent():
    """_wire_scroll_range_changed 有两条装配路径，接线必须幂等"""
    win = _make_win()
    win._wire_user_scroll_input()
    bar = win.chat_scroll_area.verticalScrollBar()
    calls = bar.installEventFilter.call_count
    win._wire_user_scroll_input()
    assert bar.installEventFilter.call_count == calls


# ─── 5. 端到端：后台被 Qt 重排 → 切回必须自动置底 ───────────────


def test_reshow_calibration_runs_after_qt_geometry_change():
    """复现用户报障的完整因果链：

    后台标签页里滚动条 value 被 Qt 自己挪走（上界重算 / 重排 / 钳制）→
    旧实现把它记成「用户滚离」→ 切回时 `_calibrate_scroll_on_reshow` 被
    `_should_follow_bottom()` 拦下 → 列表停在旧位置。新语义下不得置 away，
    校准必须真正执行。
    """
    win = _make_win()
    win._scroll_max_cache = None
    win._sync_scroll_maximum = MagicMock(return_value=1000)
    win._scroll_to_bottom = MagicMock()
    # 后台：内容涨高、Qt 把 value 挪到中间（无任何滚轮 / 拖动 / 键盘输入）
    win._on_scroll_changed(400)
    assert win._user_intentionally_away_from_bottom is False
    # 切回：showEvent 延迟一拍调校准
    win._calibrate_scroll_on_reshow()
    assert win._sync_scroll_maximum.called
    win._scroll_to_bottom.assert_called_once()
    _, kwargs = win._scroll_to_bottom.call_args
    assert kwargs.get("sticky_ms") == 900


def test_reshow_calibration_still_respects_real_user_scroll():
    """守护：用户切走前真的滚上去读历史 → 切回仍保留阅读位置"""
    win = _make_win()
    win._scroll_max_cache = None
    win._sync_scroll_maximum = MagicMock(return_value=1000)
    win._scroll_to_bottom = MagicMock()
    win._mark_user_scroll_input()
    win._on_scroll_changed(400)
    assert win._user_intentionally_away_from_bottom is True
    win._calibrate_scroll_on_reshow()
    win._scroll_to_bottom.assert_not_called()


def test_click_on_viewport_is_not_scroll_input():
    """viewport 上的普通点击（点空白处）不得当作滚动输入"""
    win = _make_win()
    win._mark_user_scroll_input_if_scroll_bar(win.chat_scroll_area.viewport(), include_viewport=False)
    assert win._has_recent_user_scroll_input() is False
