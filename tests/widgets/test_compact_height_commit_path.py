# -*- coding: utf-8 -*-
"""简洁模式流式期「WebView → 外层 Qt」高度提交路径回归测试。

覆盖本轮重构的两条语义（不依赖 QWebEngine / 主窗口）：

1. **外层不预抬上界**（`MessageCard.wants_outer_max_prelift`）：
   简洁模式流式卡的高度是内容驱动的连续流，逐拍 `maximum + delta` 的人工
   预抬与 Qt 下一拍才落地的真实上界之间总有几 px 的缝 —— 滚动条 thumb /
   折叠栏抖动即由此而来。非流式 / 非简洁模式仍预抬（离散高度事件）。

2. **自写高度回环抑制**（`_SELF_WRITE_GUARD_S`）：
   `setFixedHeight` → Chromium 视口变化 → ResizeObserver → reportHeight 的
   自激回传，在窗口内、且与已应用值差 ≤ 落定阈值时丢弃；真实增长照常进入
   流式追踪。

并附一个量化 harness：同一组流式高度序列下，统计「外层上界写入次数」与
「预抬上界与已落地内容底之间的偏差（slack）峰值」，作为抖动基线。
"""

import time

from app.widgets.message_card import MessageCard, _SELF_WRITE_GUARD_S


class _SigStub:
    def __init__(self):
        self.args = []

    def emit(self, *a):
        self.args.append(a[0] if a else None)


class _TimerStub:
    def __init__(self, interval=0):
        self._interval = interval
        self.starts = 0
        self._active = False

    def setInterval(self, ms):
        self._interval = ms

    def interval(self):
        return self._interval

    def setSingleShot(self, _flag):
        pass

    def isActive(self):
        return self._active

    def start(self, *a):
        self.starts += 1
        self._active = True

    def stop(self):
        self._active = False

    def state(self):
        return 0  # QVariantAnimation.Stopped


class _ViewerStub:
    def __init__(self, height=40, compact=True):
        self._h = height
        self._tool_compact_mode = compact
        self.fixed = []

    def height(self):
        return self._h

    def minimumHeight(self):
        return 0

    def body_max_height_cap(self):
        return 10000

    def setFixedHeight(self, h):
        self._h = h
        self.fixed.append(h)


def _make_card(streaming=True, height=200, applied=200, compact=True):
    card = MessageCard.__new__(MessageCard)
    card._streaming = streaming
    card.viewer = _ViewerStub(height=height, compact=compact)
    card._last_applied_viewer_height = applied
    card._viewer_write_guard_until = 0.0
    card._stream_height_anim_active = False
    card._is_height_animating = False
    card._resize_preview_mode = False
    # __new__ 未走 QWidget.__init__：所有会被 getattr 读到的字段必须先落 __dict__，
    # 否则 PyQt 的 __getattr__ 直接抛 RuntimeError（super-class __init__ 未调用）。
    card._layout_height_pinned = False
    card._pinned_overhead = None
    card._pinned_height = 0
    card._last_height_delta = 0
    card._target_viewer_height = applied
    card._height_batch = None
    card._finish_height_anim_active = False
    card._finish_height_anim_until = 0.0
    card._finish_height_anim_left = 0
    card._dock_collapse_until = 0.0
    card.heightChanged = _SigStub()
    card._unpin_layout_height = lambda: None
    card._resolve_height_batch = lambda: None
    card._remember_height_for_width = lambda h: None
    card._cancel_pending_shrink = lambda: None
    card._schedule_pending_shrink = lambda h: None
    card._debounced_target_height = 0
    card._height_anim = _TimerStub()
    card._stream_height_tick = _TimerStub()
    card._stream_height_timer = _TimerStub(interval=16)
    card.setProperty = lambda *a: None
    card._release_no_container_animation = lambda *a: None
    card.layout = lambda: None
    card._height_track_factor = 0.84
    return card


class TestOuterMaxPrelift:
    """外层是否预抬滚动上界的判据。"""

    def test_compact_streaming_skips_prelift(self):
        card = _make_card(streaming=True, compact=True)
        assert card.wants_outer_max_prelift() is False

    def test_non_compact_streaming_keeps_prelift(self):
        card = _make_card(streaming=True, compact=False)
        assert card.wants_outer_max_prelift() is True

    def test_non_streaming_keeps_prelift(self):
        card = _make_card(streaming=False, compact=True)
        assert card.wants_outer_max_prelift() is True

    def test_dead_viewer_defaults_to_prelift(self):
        """viewer 已被池化摘走（RuntimeError / None）→ 退回旧行为，不因异常丢补偿。"""
        card = _make_card(streaming=True, compact=True)
        card.viewer = None
        assert card.wants_outer_max_prelift() is True

        class _Dead:
            @property
            def _tool_compact_mode(self):
                raise RuntimeError("wrapped C/C++ object has been deleted")

        card.viewer = _Dead()
        assert card.wants_outer_max_prelift() is True


class TestSelfWriteGuard:
    """自写高度回环抑制。"""

    def _arm(self, card, h=200):
        card._apply_viewer_height(h)

    def test_same_value_echo_inside_window_is_dropped(self):
        card = _make_card()
        card._apply_viewer_height(240)  # 写入 + 开窗
        assert card._viewer_write_guard_until > time.monotonic()
        card._stream_height_timer._active = False
        card._update_height(card.viewer.height())  # ResizeObserver 等值回传
        assert card._debounced_target_height == 0, "自写回传不应进入流式防抖"
        assert card._stream_height_timer.starts == 0

    def test_near_value_echo_inside_window_is_dropped(self):
        card = _make_card()
        card._apply_viewer_height(240)
        card._update_height(242)  # ±2px 亚像素噪声
        assert card._debounced_target_height == 0

    def test_real_growth_inside_window_still_tracked(self):
        card = _make_card()
        card._apply_viewer_height(240)
        card._update_height(280)  # 真实增长 40px > 落定阈值
        assert card.viewer.fixed == [240, 280]
        assert card._stream_height_timer.starts == 0

    def test_echo_outside_window_is_tracked(self):
        """窗口过期后同值回传按常规路径处理（可能是真实内容变化）。"""
        card = _make_card(compact=False)
        card._viewer_write_guard_until = time.monotonic() - 0.001
        card._update_height(240)
        assert card._debounced_target_height == 240

    def test_non_streaming_ignores_guard(self):
        card = _make_card(streaming=False)
        card._viewer_write_guard_until = time.monotonic() + 1
        card._update_height(300)
        # 非流式：无流式防抖，直接走 ±2px 死区 → 大变化立即应用
        assert card.viewer.fixed == [300]


class TestSyncFirstOnLargeGap:
    """小步增长走插值追踪，落后超过一行余隙直接对齐（消除裁剪窗口）。"""

    def test_large_gap_snaps_immediately(self):
        card = _make_card(height=200, applied=200)
        card._debounced_target_height = 260  # 落后 60px > 32px 余隙
        card._apply_debounced_height()
        assert card.viewer.fixed == [260], "大台阶必须当拍对齐，不能留给追踪插值"
        assert card._stream_height_anim_active is False

    def test_small_gap_starts_track(self):
        card = _make_card(height=200, applied=200)
        card._debounced_target_height = 214  # 落后 14px < 余隙
        card._apply_debounced_height()
        assert card.viewer.fixed == [], "小步增长不应直接写高度（保持连续生长观感）"
        assert card._stream_height_anim_active is True
        assert card._stream_height_tick.starts == 1

    def test_large_gap_stops_running_track_before_snap(self):
        card = _make_card(height=200, applied=200)
        card._stream_height_anim_active = True
        card._stream_height_tick._active = True
        card._debounced_target_height = 300
        card._apply_debounced_height()
        assert card.viewer.fixed == [300]
        assert card._stream_height_tick.isActive() is False


class _OuterHostStub:
    """镜像 main_widget._on_message_card_height_changed 的上界判据（预抬 + 落地）。

    只复刻与本轮重构相关的两行语义：

    * 预抬路径：高度回调**先**把 `maximum` 加 delta，而控件真实高度要下一轮
      LayoutRequest 才传播 —— 因此这一帧 `maximum > 已落地内容底`，差值就是
      「上界比内容多出来的一帧」。逐拍发生 → 滚动条 thumb / 折叠栏可见抖动。
    * 不预抬路径：上界完全由 Qt 在布局落地时更新，与落地内容同帧一致。
    """

    def __init__(self):
        self.maximum = 0
        self.landed = 0
        self.prelift_calls = 0
        self.max_slack = 0

    def apply(self, delta, prelift):
        if prelift and delta > 0:
            self.maximum = max(0, self.maximum + delta)
            self.prelift_calls += 1
            # 本帧布局尚未传播：maximum 已抬、landed 还在上一拍
            self.max_slack = max(self.max_slack, self.maximum - self.landed)
        # LayoutRequest 落地：控件真实高度跟上
        self.landed += delta
        if self.landed > self.maximum:
            self.maximum = self.landed


class TestOuterJitterBaseline:
    """同一组流式高度序列下，预抬与否的外层上界稳定性对比。"""

    def _run(self, prelift: bool):
        host = _OuterHostStub()
        seq = [40, 60, 92, 130, 168, 210, 268, 300, 356, 420, 470, 520, 590, 640, 700, 760, 820, 880, 940, 1000]
        prev = seq[0]
        for h in seq[1:]:
            host.apply(h - prev, prelift)
            prev = h
        return host

    def test_prelift_leaves_slack_every_frame(self):
        """基线：逐拍预抬时，上界与已落地内容底逐帧存在间隙（抖动的来源）。"""
        host = self._run(prelift=True)
        assert host.prelift_calls == 19
        assert host.max_slack >= 50, "基线应复现「上界领先内容一帧」的可见间隙"

    def test_prelift_disabled_has_zero_manual_writes(self):
        """重构后：简洁流式卡不写 maximum，全部交给 Qt 布局。"""
        host = self._run(prelift=False)
        assert host.prelift_calls == 0
        assert host.max_slack == 0


class TestProductionWiring:
    """生产入口接线（源码级断言，防止判据被绕过）。"""

    def test_main_widget_uses_prelift_predicate(self):
        import inspect

        import app.main_widget as mw

        src = inspect.getsource(mw.OpenAIChatToolWindow._on_message_card_height_changed)
        assert "sender.wants_outer_max_prelift()" in src, (
            "外层高度回调必须经卡片判据决定是否预抬上界，否则简洁模式流式期仍会逐拍改写滚动条 maximum"
        )
        assert "if delta > 0:" not in src, "无条件预抬分支应已被判据替换"
