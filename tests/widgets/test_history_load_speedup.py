# -*- coding: utf-8 -*-
"""回归测试：历史会话加载的两项提速改动（2026-10-14）

改动 1：懒渲染队列「视口优先」反序
  问题：`_render_message_to_card` 按批号**升序**收集待懒渲染卡片入队，队列
  FIFO 消费（每节拍约 2 张，80ms 节拍）。历史加载后视口 sticky 在**底部**
  （用户看的是最新几条），于是用户盯着的那几张排在 30 张队尾，约 2.4s 后
  才渲染 —— 观感即「内容出来了但卡片一张张慢慢浮现」。
  修复：批次索引反序收集（最新批次先进队），批内卡片顺序保持原样；向上翻
  历史（insert_at_top / 用户已滚离底部）时不反序。

改动 3：diff stats 批量消费（替代每卡一个 QTimer(0)）
  问题：历史加载时每张 assistant 卡各排一个 `QTimer.singleShot(0)` 去读
  备份文件 + 当前文件 + difflib，N 卡 = N 个回调在懒渲染节拍之间抢主线程。
  修复：去重入队 + 懒渲染排空后按时间片批量消费 + 消费前会话/存活校验。

测试策略：
- 改动 1 走**真实** `_render_message_to_card`（batch 走「已渲染过」cache-hit
  分支，不触卡片构造），monkeypatch MessageCard 为轻量桩以过 isinstance。
- 改动 3 直接调用真实入队/消费方法，QTimer.singleShot 隔离。
"""

from unittest.mock import MagicMock

import pytest

import app.main_widget as mw
from app.main_widget import OpenAIChatToolWindow


class _FakeCard:
    """MessageCard 轻量替身（过 isinstance + _lazy_rendered 判定）"""

    def __init__(self, lazy_rendered: bool = False):
        self._lazy_rendered = lazy_rendered


@pytest.fixture
def fake_message_card(monkeypatch):
    monkeypatch.setattr(mw, "MessageCard", _FakeCard)
    return _FakeCard


@pytest.fixture
def no_qt_timer(monkeypatch):
    """隔离 QTimer.singleShot，收集 (delay, fn) 调用供断言"""
    calls = []
    monkeypatch.setattr(mw.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn))))
    return calls


def _make_window(**overrides):
    """构造 _render_message_to_card 收集段 / diff stats 队列可运行的最小实例"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._pending_lazy_cards = []
    win._lazy_batch_timer_active = False
    win._user_intentionally_away_from_bottom = False
    # _render_message_to_card 前置依赖
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "sid-test"
    win._is_widget_alive = lambda card: True
    win._flush_batch_immediate_renders = MagicMock()
    # cache-hit 分支仍会先算 round_index（前缀和缓存 → 需 _message_batch）
    win._message_batch = []
    win._user_prefix_cache = [0]
    win._get_user_round_index_for_batch_index = lambda *a, **k: 0
    # insert_at_top 分支会 insertWidget 到布局（桩掉避免真 Qt 布局依赖）
    win.chat_layout = MagicMock()
    # diff stats 队列
    win._pending_diff_stats_queue = []
    win._pending_diff_stats_ids = set()
    win._diff_stats_timer_active = False
    win._update_card_diff_stats = MagicMock()
    for key, value in overrides.items():
        setattr(win, key, value)
    return win


def _collect(win, batch_count: int, batch_offset: int = 0, insert_at_top: bool = False):
    """走真实 _render_message_to_card 收集段：batch 全走「已渲染过」cache-hit 分支。

    batch_offset 非零时 _batch_cards 需覆盖全量索引（真实场景即为全表长度），
    窗口前的槽位填 None（未渲染）。
    返回 (pending_lazy_cards, 各批卡片引用列表)。
    """
    cards_by_batch = []
    win._batch_cards = [None] * batch_offset
    batches = []
    for i in range(batch_count):
        card = _FakeCard(lazy_rendered=False)
        cards_by_batch.append(card)
        win._batch_cards.append([card])
        batches.append([{"role": "user", "content": f"b{i}"}])
    win._render_message_to_card(batches, insert_at_top=insert_at_top, batch_offset=batch_offset)
    return win._pending_lazy_cards, cards_by_batch


# ── 改动 1：批次级反序（真实方法） ─────────────────────────


class TestViewportFirstLazyOrder:
    def test_reversed_when_viewport_at_bottom(self, fake_message_card, no_qt_timer):
        """视口贴底（默认）→ 最新批次最先入队（可见区第一拍渲染）"""
        win = _make_window()
        pending, cards = _collect(win, 5)
        assert len(pending) == 5
        assert pending[0] is cards[4], "最新批次（索引 4）应最先入队"
        assert pending[-1] is cards[0], "最旧批次最后入队"

    def test_no_reverse_when_user_away_from_bottom(self, fake_message_card, no_qt_timer):
        """用户已滚离底部（上翻历史）→ 顺序保持原样，旧批次优先"""
        win = _make_window(_user_intentionally_away_from_bottom=True)
        pending, cards = _collect(win, 5)
        assert pending[0] is cards[0]
        assert pending[-1] is cards[4]

    def test_no_reverse_on_insert_at_top(self, fake_message_card, no_qt_timer):
        """insert_at_top（向上 prepend 历史批次）→ 顺序保持原样"""
        win = _make_window()
        pending, cards = _collect(win, 5, insert_at_top=True)
        assert pending[0] is cards[0]
        assert pending[-1] is cards[4]

    def test_batch_offset_respected(self, fake_message_card, no_qt_timer):
        """batch_offset 非零（分批加载）→ 只收集窗口内批次，仍反序"""
        win = _make_window()
        pending, cards = _collect(win, 4, batch_offset=2)
        assert len(pending) == 4
        assert pending[0] is cards[3], "窗口内最新批次最先入队"

    def test_already_rendered_cards_excluded(self, fake_message_card, no_qt_timer):
        """已懒渲染的卡片不入队（不影响顺序语义）"""
        win = _make_window()
        win._batch_cards = []
        batches = []
        for i in range(3):
            card = _FakeCard(lazy_rendered=(i == 1))  # 中间批已渲染
            win._batch_cards.append([card])
            batches.append([{"role": "user", "content": f"b{i}"}])
        win._render_message_to_card(batches)
        pending = win._pending_lazy_cards
        assert len(pending) == 2
        assert pending[0] is win._batch_cards[2][0]
        assert pending[1] is win._batch_cards[0][0]


# ── 改动 3：diff stats 批量消费 ───────────────────────────


class TestDiffStatsBatchQueue:
    def test_enqueue_dedupes_same_card(self, no_qt_timer):
        win = _make_window()
        card = MagicMock()
        win._enqueue_card_diff_stats(card)
        win._enqueue_card_diff_stats(card)
        assert len(win._pending_diff_stats_queue) == 1

    def test_enqueue_skips_without_session_id(self, no_qt_timer):
        win = _make_window(_current_session_id_for_extras=lambda: "")
        win._enqueue_card_diff_stats(MagicMock())
        assert win._pending_diff_stats_queue == []

    def test_enqueue_skips_none_card(self, no_qt_timer):
        win = _make_window()
        win._enqueue_card_diff_stats(None)
        assert win._pending_diff_stats_queue == []

    def test_enqueue_schedules_drain_once(self, no_qt_timer):
        """首次入队调度一次 drain；已在途不重复调度"""
        win = _make_window()
        win._enqueue_card_diff_stats(MagicMock())
        first = len(no_qt_timer)
        win._enqueue_card_diff_stats(MagicMock())
        assert len(no_qt_timer) == first, "drain 在途不应重复调度"

    def test_drain_defers_while_lazy_queue_busy(self, no_qt_timer):
        """懒渲染队列未排空 → 整体推迟，不做统计"""
        win = _make_window()
        card = MagicMock()
        win._enqueue_card_diff_stats(card)
        no_qt_timer.clear()
        win._pending_lazy_cards.append(MagicMock())  # 懒渲染忙碌
        win._drain_pending_diff_stats()
        win._update_card_diff_stats.assert_not_called()
        assert len(win._pending_diff_stats_queue) == 1
        assert no_qt_timer, "应重新调度下一拍"

    def test_drain_runs_when_lazy_queue_empty(self, no_qt_timer):
        win = _make_window()
        card = MagicMock()
        win._enqueue_card_diff_stats(card)
        win._drain_pending_diff_stats()
        win._update_card_diff_stats.assert_called_once_with(card)
        assert win._pending_diff_stats_queue == []
        assert win._diff_stats_timer_active is False

    def test_drain_discards_on_session_mismatch(self, no_qt_timer):
        """切会话后旧卡片队列项丢弃（不写错数字）"""
        win = _make_window()
        card = MagicMock()
        win._enqueue_card_diff_stats(card)
        win._current_session_id_for_extras = lambda: "sid-other"
        win._drain_pending_diff_stats()
        win._update_card_diff_stats.assert_not_called()
        assert win._pending_diff_stats_queue == []

    def test_drain_skips_dead_card_but_continues(self, no_qt_timer):
        """已销毁卡片跳过，后续项照常处理"""
        win = _make_window()
        dead, alive = MagicMock(), MagicMock()
        win._enqueue_card_diff_stats(dead)
        win._enqueue_card_diff_stats(alive)
        win._is_widget_alive = lambda c: c is alive
        win._drain_pending_diff_stats()
        win._update_card_diff_stats.assert_called_once_with(alive)

    def test_drain_destroyed_window_clears(self, no_qt_timer):
        """窗口已销毁 → drain 自清退场"""
        win = _make_window(_is_destroyed=True)
        win._enqueue_card_diff_stats(MagicMock())
        win._drain_pending_diff_stats()
        win._update_card_diff_stats.assert_not_called()
        assert win._pending_diff_stats_queue == []

    def test_clear_resets_all_state(self, no_qt_timer):
        win = _make_window()
        win._enqueue_card_diff_stats(MagicMock())
        assert win._pending_diff_stats_queue and win._pending_diff_stats_ids
        win._clear_pending_diff_stats()
        assert win._pending_diff_stats_queue == []
        assert win._pending_diff_stats_ids == set()
        assert win._diff_stats_timer_active is False

    def test_drain_single_card_failure_does_not_break_rest(self, no_qt_timer):
        """单卡统计抛异常 → 跳过该卡，后续卡照常"""
        win = _make_window()
        bad, good = MagicMock(), MagicMock()

        def _side_effect(card):
            if card is bad:
                raise RuntimeError("boom")

        win._update_card_diff_stats.side_effect = _side_effect
        win._enqueue_card_diff_stats(bad)
        win._enqueue_card_diff_stats(good)
        win._drain_pending_diff_stats()
        assert win._update_card_diff_stats.call_count == 2
        assert win._pending_diff_stats_queue == []
