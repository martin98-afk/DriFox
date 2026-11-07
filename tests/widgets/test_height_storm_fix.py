# -*- coding: utf-8 -*-
"""[风暴主修 #32] 落高合并路由 + 空文档连报拉黑回归测试。

覆盖：
1. _commit_viewer_height 批路由放宽：batch.active 时流式卡也走 submit（不再直写）
2. _on_height_reported 空文档同值连报拉黑：≥3 次同值 <40px 拉黑 5s、期内吞报、
   高度变化立即解禁
无 WebEngine 依赖（__new__ 桩 + 实例属性遮蔽）。
"""
import pytest

from app.widgets.card_viewers import CodeWebViewer


class TestEmptyReportBlackout:
    """空文档同值连报拉黑（修复 2）。"""

    def _make_stub(self):
        w = CodeWebViewer.__new__(CodeWebViewer)
        w._streaming = False
        w._height_report_pending = False
        w._finish_t0 = 0.0
        w._document_height = 0
        w._empty_report_streak = (None, 0)
        w._empty_report_blocked_until = 0.0
        w._blank_diag_last_ts = 0.0
        w._rdiag = None
        w._rdiag_last = 0.0
        w.height = lambda: 40  # 遮蔽 QWidget.height（未初始化桩）
        w._content_sentinel_check = lambda: None

        class _Sig:
            def emit(self, *a):
                self.last = a[0] if a else None

        w.contentHeightChanged = _Sig()  # 遮蔽类信号（未初始化 QObject 不可 emit）
        return w

    def test_three_same_small_reports_trigger_blackout(self):
        """连续 3 次同值 <40px：第 3 次触发拉黑并吞掉本次上报。"""
        w = self._make_stub()
        w._on_height_reported(11)
        w._on_height_reported(11)
        assert w._document_height == 11  # 未拉黑，正常记账
        w._on_height_reported(11)  # 第 3 次 → 拉黑
        assert w._empty_report_blocked_until > 0
        assert w._empty_report_blocked_value == 11
        assert w._document_height == 11  # 拉黑吞报，不再更新

    def test_blocked_period_swallows_same_value(self):
        """拉黑期内同值上报直接吞掉（不更新 _document_height）。"""
        w = self._make_stub()
        for _ in range(3):
            w._on_height_reported(11)
        w._document_height = -1  # 哨兵：拉黑期内不应被覆盖
        w._on_height_reported(11)
        assert w._document_height == -1

    def test_height_change_releases_blackout(self):
        """拉黑期内高度变化 = 真实内容到达，立即解禁并放行本次上报。"""
        w = self._make_stub()
        for _ in range(3):
            w._on_height_reported(11)
        w._on_height_reported(500)
        assert w._empty_report_blocked_until == 0.0
        assert w._document_height == 500

    def test_large_height_never_blackouts(self):
        """正常高度（≥40）无论连报多少次都不触发拉黑。"""
        w = self._make_stub()
        for _ in range(10):
            w._on_height_reported(400)
        assert w._empty_report_blocked_until == 0.0
        assert w._document_height == 400

    def test_alternating_small_values_never_blackout(self):
        """同值计数按相邻重复计：交替值（11/12/11/12）不触发拉黑。"""
        w = self._make_stub()
        for v in (11, 12, 11, 12, 11, 12):
            w._on_height_reported(v)
        assert w._empty_report_blocked_until == 0.0


class TestBatchRoutingStreamingCard:
    """批路由放宽（修复 1b）：batch.active 时流式卡也走 submit。"""

    def test_streaming_card_goes_through_batch(self, qapp):
        from app.widgets.message_card import MessageCard

        card = MessageCard.__new__(MessageCard)
        card._streaming = True
        card.viewer = None  # submit 路径不触 viewer；直写路径才会触（若误走即炸）

        class FakeBatch:
            def __init__(self):
                self.active = True
                self.submitted = []

            def submit(self, card_, height):
                self.submitted.append((card_, height))

        batch = FakeBatch()
        # 桩掉获取链：实例上直接持有 batch
        card._height_batch = batch
        card._unpin_layout_height = lambda: None
        card._resolve_height_batch = lambda: batch

        card._commit_viewer_height(123)
        assert batch.submitted == [(card, 123)]

    def test_inactive_batch_falls_back_to_direct(self, qapp):
        """batch 未激活时保持直接路径（流式跟底零延迟语义）。"""
        from app.widgets.message_card import MessageCard

        card = MessageCard.__new__(MessageCard)
        card._streaming = True

        class FakeViewer:
            def __init__(self):
                self.fixed = None

            def setFixedHeight(self, h):
                self.fixed = h

        viewer = FakeViewer()
        card.viewer = viewer

        class HeightSig:
            def emit(self, h):
                self.last = h

        card.heightChanged = HeightSig()
        card._height_batch = None
        card._unpin_layout_height = lambda: None
        card._resolve_height_batch = lambda: None

        card._commit_viewer_height(200)
        assert viewer.fixed == 200
