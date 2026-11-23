# -*- coding: utf-8 -*-
"""懒渲染 drain 收口计数自愈契约。

背景：切会话 / 清空聊天区会把 `_rendered_card_count` 归零
（`_sync_global_rendered_pages(0)`）。下一轮懒渲染的增量记账可能恒 0
（卡片重建时 `_lazy_rendered` 已置位，本次 drain 不产生新的记账），
于是 `_process_next_lazy_batch` 里「`> quota` 才调度淘汰」永不成立，
`_recycle_lru_batches` 开头「计数 == 0 就校准」的分支也永远没机会执行
（死锁：离屏批次不卸载、viewer 不归还池）。

契约：drain 排空时必须按 `_batch_cards` 实况重算一次计数；重算后超配额
则调度一次淘汰。测试用 `__new__` 构造最小实例（同
test_render_quota_enforcement.py 手法），不建真窗口。
"""
import sys
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QApplication

import app.main_widget as mw_mod
from app.main_widget import OpenAIChatToolWindow


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _card(lazy_rendered=True):
    c = MagicMock()
    c._lazy_rendered = lazy_rendered
    c.ensure_rendered = MagicMock()
    return c


def _make_window(batch_cards, quota=12):
    """`_process_next_lazy_batch` 可运行的最小实例。"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._rendered_card_count = 0  # 归零后的死锁起点
    win._batch_cards = batch_cards
    win._pending_lazy_cards = []
    win._lazy_batch_timer_active = True
    win._loading_session = True
    win._is_widget_alive = lambda w: True
    win._is_virtual_recycling = False
    win._recycle_lru_call_count = 0
    win._last_visible_card_ids = set()
    win._is_loading_history_batches = False
    win._is_jump_in_flight = lambda: False
    win._batch_placeholders = {}
    win._unload_batch = MagicMock(return_value=0)
    win._maybe_strong_recycle = MagicMock()
    win._effective_max_rendered_cards = lambda: quota
    win._render_floor = lambda: 3
    win._last_quota_log_at = 0.0
    win._log_render_quota = MagicMock()
    win._initial_scroll_to_bottom = False
    win._user_intentionally_away_from_bottom = True
    win._should_follow_bottom = lambda: False
    win._extend_jump_scroll_lock = MagicMock()
    win.chat_layout = MagicMock()
    win.chat_layout.count.return_value = 0
    win.chat_layout.itemAt.return_value = None
    scroll_bar = MagicMock()
    scroll_bar.maximum.return_value = 1000
    scroll_bar.value.return_value = 0
    area = MagicMock()
    area.verticalScrollBar.return_value = scroll_bar
    area.viewport.return_value.height.return_value = 600
    win.chat_scroll_area = area
    return win


@pytest.fixture
def scheduled(monkeypatch):
    """拦截 QTimer.singleShot，记录 (delay_ms, callback)。"""
    calls = []
    monkeypatch.setattr(
        mw_mod.QTimer, "singleShot", staticmethod(lambda ms, fn=None: calls.append((ms, fn)))
    )
    return calls


def _thirty_rendered_batches():
    return [[_card(lazy_rendered=True)] for _ in range(30)]


class TestLazyDrainRecount:
    def test_drain_recounts_from_zero(self, scheduled):
        """a) 计数被归零 + drain 增量漏记 → 排空后校准为实况 30。"""
        _ensure_qapp()
        win = _make_window(_thirty_rendered_batches(), quota=12)
        # 队列里放一张未渲染卡：ensure_rendered 不置 _lazy_rendered，
        # 复现「渲染发生但增量记账为 0」的漏记路径。
        win._pending_lazy_cards = [_card(lazy_rendered=False)]

        win._process_next_lazy_batch()

        assert win._rendered_card_count == 30, (
            f"drain 排空后应按 _batch_cards 实况校准为 30，实际 {win._rendered_card_count}"
        )

    def test_drain_schedules_recycle_when_over_quota(self, scheduled):
        """b) 校准后 30 > quota 12 → 必须调度 _recycle_lru_batches。"""
        _ensure_qapp()
        win = _make_window(_thirty_rendered_batches(), quota=12)
        win._pending_lazy_cards = [_card(lazy_rendered=False)]

        win._process_next_lazy_batch()

        zero_delays = [cb for ms, cb in scheduled if ms == 0]
        assert zero_delays, f"超配额后应有 singleShot(0, _recycle_lru_batches)，实际 {scheduled}"
        assert len(zero_delays) == 1, f"drain 收口只应调度一次淘汰，实际 {len(zero_delays)}"

    def test_drain_under_quota_does_not_schedule(self, scheduled):
        """反向：30 ≤ quota 40 时不调度淘汰（避免无条件空转）。"""
        _ensure_qapp()
        win = _make_window(_thirty_rendered_batches(), quota=40)
        win._pending_lazy_cards = [_card(lazy_rendered=False)]

        win._process_next_lazy_batch()

        assert win._rendered_card_count == 30
        assert not [cb for ms, cb in scheduled if ms == 0], "未超配额不应调度淘汰"

    def test_lazy_drained_log_reports_recounted_value(self, scheduled):
        """c) lazy-drained 打点里的 rendered= 不再是 0。"""
        _ensure_qapp()
        from loguru import logger as _logger

        win = _make_window(_thirty_rendered_batches(), quota=12)
        win._pending_lazy_cards = [_card(lazy_rendered=False)]
        win._log_render_quota = OpenAIChatToolWindow._log_render_quota.__get__(win)

        logs = []
        sink_id = _logger.add(lambda m: logs.append(str(m)), level="INFO")
        try:
            win._process_next_lazy_batch()
        finally:
            _logger.remove(sink_id)

        lines = [line for line in logs if "[render-quota]" in line and "lazy-drained" in line]
        assert lines, f"未捕获 lazy-drained 打点，实际日志: {logs}"
        assert "rendered=30" in lines[-1], f"rendered= 应校准为 30，实际日志: {lines[-1]}"


class TestEarlyReturnRecount:
    """早退分支的条件自愈（切会话最常走「队列已空」入口）。"""

    def test_empty_queue_entry_heals_zero_count(self, scheduled):
        """f) `_pending_lazy_cards` 为空 + 计数 0 → 进入即校准为实况 30。"""
        _ensure_qapp()
        win = _make_window(_thirty_rendered_batches(), quota=12)
        win._pending_lazy_cards = []

        win._process_next_lazy_batch()

        assert win._rendered_card_count == 30, (
            f"空队列入口应校准为 30，实际 {win._rendered_card_count}"
        )
        assert [cb for ms, cb in scheduled if ms == 0], "校准后超配额应调度淘汰"

    def test_all_filtered_batch_heals_zero_count(self, scheduled):
        """g) 队列里的卡全被 16023 过滤（已 _lazy_rendered）→ batch 空也不漏校准。"""
        _ensure_qapp()
        win = _make_window(_thirty_rendered_batches(), quota=12)
        win._pending_lazy_cards = [_card(lazy_rendered=True) for _ in range(3)]

        win._process_next_lazy_batch()

        assert win._rendered_card_count == 30, (
            f"batch 全被过滤后应校准为 30，实际 {win._rendered_card_count}"
        )

    def test_positive_count_skips_recount(self, monkeypatch, scheduled):
        """h) 常态零成本：计数 >0 时空队列入口不触发 O(N) 校准。"""
        _ensure_qapp()
        win = _make_window(_thirty_rendered_batches(), quota=12)
        win._rendered_card_count = 30
        win._pending_lazy_cards = []
        calls = []
        original = OpenAIChatToolWindow._recount_rendered_cards

        def _counting_recount(self):
            calls.append(1)
            return original(self)

        monkeypatch.setattr(OpenAIChatToolWindow, "_recount_rendered_cards", _counting_recount)

        win._process_next_lazy_batch()

        assert not calls, "计数正常时不应触发校准（条件守卫生效）"

    def test_heal_log_line_sample(self, scheduled):
        """i) 校准日志样例：`[render-quota] recount: 0 -> 30 (lazy-empty)`。"""
        _ensure_qapp()
        from loguru import logger as _logger

        win = _make_window(_thirty_rendered_batches(), quota=12)
        win._pending_lazy_cards = []

        logs = []
        sink_id = _logger.add(lambda m: logs.append(str(m)), level="DEBUG")
        try:
            win._process_next_lazy_batch()
        finally:
            _logger.remove(sink_id)

        heal_lines = [line for line in logs if "[render-quota] recount:" in line]
        assert heal_lines, f"未捕获校准日志，实际日志: {logs}"
        assert "0 -> 30 (lazy-empty)" in heal_lines[-1], f"日志样例不符: {heal_lines[-1]}"


def _make_recycle_window(count=22, quota=12):
    """`_recycle_out_of_view_batches` 可运行的最小实例（回收循环不深跑）。"""
    win = _make_window([[_card(lazy_rendered=True)] for _ in range(count)], quota=quota)
    win._incremental_visible_batch_count = 100  # buffer 覆盖全表 → 回收循环为空
    win._virtual_scroll_buffer = 1
    win._resolve_viewport_range = lambda: (0, 0)
    win._restore_window_batches = lambda: 0
    win._batch_fully_rendered = lambda cards: True
    win._batch_is_protected = lambda idx, vp_range, restore_win: True
    win._batch_is_streaming_or_welcome = lambda idx: False
    win._batch_distance = lambda idx, vp_range: 0
    win._virtual_buffer_batches = lambda: 0
    win._message_batch = []
    win._current_assistant_card = None
    win._recycle_lru_batches = MagicMock()
    win._maybe_strong_recycle = MagicMock()
    return win


class TestRecycleOutOfViewRecount:
    def test_sync_built_cards_recovered_on_scroll(self):
        """j) 同步建卡（懒队列全被过滤）+ 计数 0 → 滚动汇合点入口即校准。"""
        _ensure_qapp()
        win = _make_recycle_window(count=22)
        # 切会话重建的卡：同步建好且 _lazy_rendered=True，懒队列里的会被过滤光
        win._pending_lazy_cards = [_card(lazy_rendered=True) for _ in range(3)]

        win._recycle_out_of_view_batches()

        assert win._rendered_card_count == 22, (
            f"同步建卡 + 懒队列被过滤后，回收入口应校准为 22，实际 {win._rendered_card_count}"
        )

    def test_positive_count_skips_recount(self, monkeypatch):
        """k) 常态零成本：计数 >0 时不触发 O(N) 校准。"""
        _ensure_qapp()
        win = _make_recycle_window(count=22)
        win._rendered_card_count = 22
        calls = []
        original = OpenAIChatToolWindow._recount_rendered_cards

        def _counting_recount(self):
            calls.append(1)
            return original(self)

        monkeypatch.setattr(OpenAIChatToolWindow, "_recount_rendered_cards", _counting_recount)

        win._recycle_out_of_view_batches()

        assert not calls, "计数正常时不应触发 O(N) 校准（入口条件守卫生效）"
