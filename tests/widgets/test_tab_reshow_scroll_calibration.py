# -*- coding: utf-8 -*-
"""回归：切回标签页时补一次滚动校准，让消息列表重新贴底。

Bug 复现（离屏 Qt 探针实测，QStackedWidget 隐藏页 + QScrollArea）：
    A. 基线（可见，贴底）:      max=1192  value=1192
    B. 切到后台（隐藏）:        max=1192  value=1192
    C. 后台内容涨高（仅靠 Qt）: max=1192  value=1192   real(sizeHint)=2692
    D. 调 _sync_scroll_maximum: max=2692  value=1192   距底=1500
    E. 后台期间持续置底:         max=3292  value=3292   距底=0

结论：隐藏页里 Qt 完全停更滚动条（``maximum()`` 冻结、``setValue()`` 被静默
钳制），且 ``setValue(9999)`` 后 ``value`` 仍为 0；容器 ``sizeHint()`` 却依旧
准确。故切走期间的高度增长不会反映到滚动条上，切回瞬间 value 停在旧位置。

负责追平的调用点此前不存在：
  - ``TabManagerWindow._on_tab_selected`` 只做浮动卡投影/主题补刷/桌宠/工作台
  - 补渲路径 ``_render_deferred → showEvent → _schedule_render`` 不置
    ``_content_just_loaded``，heightChanged 只走单卡 delta 增量补偿，追不上积压

修复：``OpenAIChatToolWindow.showEvent`` 的已初始化分支延迟一拍调
``_calibrate_scroll_on_reshow``（先校正上界再起 sticky 置底）。

测试策略：与 ``test_stream_finish_bottom_follow.py`` 同策略——不实例化完整窗口
（依赖过多），``__new__`` 后手工补齐属性，断言 calibrate 的**行为契约**：
跟底态才滚、上滚阅读态不打扰、先校正上界再起 sticky。
"""
from unittest.mock import MagicMock

# 注意：不得在此插入 "app" 到 sys.path——app/plugins（regular package）会
# 劫持顶层 `plugins.*` 命名空间解析。仓库根已由 pytest rootdir 提供。
from app.main_widget import OpenAIChatToolWindow  # noqa: E402


def _make_win(**overrides):
    """构造 ``_calibrate_scroll_on_reshow`` 可运行的最小实例"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    # ⚠️ 必须显式给值：PyQt 对象未经 __init__ 时 getattr(self, "_is_destroyed", False)
    # 会抛 RuntimeError（super-class __init__ was never called），带默认值的
    # getattr 兜不住该异常。凡走 _is_destroyed 守卫的方法都要靠它。
    win._is_destroyed = False
    win._user_intentionally_away_from_bottom = False
    win._programmatic_scroll_depth = 0
    win._bottom_anchor_deadline = 0.0
    win._scroll_max_cache = None
    # _should_follow_bottom 走 away 标志 + _is_view_at_bottom
    win._is_view_at_bottom = MagicMock(return_value=True)
    win._sync_scroll_maximum = MagicMock(return_value=2692)
    win._scroll_to_bottom = MagicMock()
    scroll_bar = MagicMock()
    scroll_bar.maximum.return_value = 2692
    scroll_bar.value.return_value = 1192  # 切走时的旧位置：距底 1500px
    area = MagicMock()
    area.verticalScrollBar.return_value = scroll_bar
    win.chat_scroll_area = area
    for key, value in overrides.items():
        setattr(win, key, value)
    return win


# ─── 1. 跟底态：必须校准（复现路径的正面用例） ─────────────────


def test_calibrate_scrolls_when_following():
    """用户未滚离底部 → 切回时先校正上界，再起 sticky 置底"""
    win = _make_win()
    win._calibrate_scroll_on_reshow()
    # 上界校正：缓存失效后才重算（防止读到隐藏期的陈旧上界）
    win._sync_scroll_maximum.assert_called_once()
    # 起一次 sticky 置底，复用 _maintain_bottom_anchor + _ensure_at_bottom 收敛链
    win._scroll_to_bottom.assert_called_once()
    _, kwargs = win._scroll_to_bottom.call_args
    assert kwargs.get("sticky_ms") == 900, "应与懒渲染批次结束/回到底部胶囊同款 sticky"


def test_calibrate_invalidates_stale_max_cache():
    """陈旧上界缓存必须先失效：隐藏期的 maximum 是失真值"""
    win = _make_win()
    win._scroll_max_cache = (0.0, 1192)  # 切走时的旧上界
    win._calibrate_scroll_on_reshow()
    assert win._scroll_max_cache is None, "缓存未失效会读到隐藏期的陈旧上界"


# ─── 2. 上滚阅读态：不得打扰（守卫回归） ────────────────────────


def test_calibrate_skips_when_user_away():
    """用户切走前正在读历史 → 保持其阅读位置，不滚底"""
    win = _make_win(
        _user_intentionally_away_from_bottom=True,
        _is_view_at_bottom=MagicMock(return_value=False),
    )
    win._calibrate_scroll_on_reshow()
    win._scroll_to_bottom.assert_not_called()


def test_calibrate_respects_away_flag_with_fallback():
    """away 标志与实际位置不一致时以实际位置为准（_should_follow_bottom 语义）"""
    win = _make_win(
        _user_intentionally_away_from_bottom=True,
        _is_view_at_bottom=MagicMock(return_value=True),  # 实际已贴底
    )
    win._calibrate_scroll_on_reshow()
    win._scroll_to_bottom.assert_called_once()


# ─── 3. 生命周期守卫 ────────────────────────────────────────────


def test_calibrate_skips_when_destroyed():
    """窗口已销毁（QTimer 回调晚于 closeEvent）→ 直接跳过，不访问已释放对象"""
    win = _make_win(_is_destroyed=True)
    win._calibrate_scroll_on_reshow()
    win._scroll_to_bottom.assert_not_called()
    win._sync_scroll_maximum.assert_not_called()


# ─── 4. showEvent 接线：已初始化分支才校准（首次显示不重复） ──────


def test_show_event_schedules_calibration_on_reshow(monkeypatch):
    """已初始化窗口的 showEvent 分支必须排入一次校准回调"""
    win = _make_win()
    win._session_initialized = True
    win._connect_opacity_signal = MagicMock()
    win._maybe_build_deferred_content = MagicMock()
    win._safe_timer_call = MagicMock()
    # super().showEvent 会访问真实 Qt 内部状态，裸 __new__ 实例不可用 → 桩掉
    monkeypatch.setattr(
        OpenAIChatToolWindow.__mro__[1],
        "showEvent",
        lambda self, e: None,
        raising=False,
    )
    scheduled = []

    def _fake_single_shot(_ms, cb):
        scheduled.append(cb)

    monkeypatch.setattr("app.main_widget.QTimer.singleShot", _fake_single_shot)

    win.showEvent(MagicMock())

    assert len(scheduled) == 1, "切回标签页必须排入恰好一次校准"
    scheduled[0]()  # 执行排入的回调 → 转交 _safe_timer_call
    win._safe_timer_call.assert_called_once()
