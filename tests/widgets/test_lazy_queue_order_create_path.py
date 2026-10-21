# -*- coding: utf-8 -*-
"""回归测试：历史加载懒渲染队列「反序」必须对真实 create 路径生效

背景（2026-10-19 bug）：
    视口优先的反序入队写在 `_render_message_to_card` 尾部，但**建卡过程**
    （`_append_assistant_message` → `_add_chat_widget`）每建一张卡就按
    **正序**把它 append 进 `_pending_lazy_cards`。原实现「反序收集后按 id
    去重追加」因此一张都加不进去（id 全命中），反序成死代码 —— 用户盯着的
    最底部几张排在 20+ 张队尾，观感即「卡片从上面一张张慢慢浮现」。

    既有的 `tests/widgets/test_history_load_speedup.py::TestViewportFirstLazyOrder`
    只走「已渲染过」cache-hit 分支（批次预置 `_batch_cards`），**绕过了建卡
    路径**，所以反序 bug 存在时它依然全绿。本文件改走真实 create 分支，
    把这条路径钉死。

测试策略：不打 QTimer、不建真实 Qt 卡片。用 `_FakeCard` 过 isinstance，
把 `_append_user_message` / `_append_assistant_message` 换成「建卡即模拟
`_add_chat_widget` 正序入队」的桩，复现被架空的真实时序。
"""

from unittest.mock import MagicMock

import pytest

import app.main_widget as mw
from app.main_widget import OpenAIChatToolWindow


class _FakeCard:
    """MessageCard 轻量替身（过 isinstance + _lazy_rendered 判定）"""

    def __init__(self):
        self._lazy_rendered = False
        self._message_index = None

    def append_text(self, *a, **kw):
        pass

    def append_tool_result(self, *a, **kw):
        pass

    def finish_streaming(self, *a, **kw):
        pass


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
    """构造 _render_message_to_card 可运行的最小实例（create 分支）"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._pending_lazy_cards = []
    win._lazy_batch_timer_active = False
    win._user_intentionally_away_from_bottom = False
    win._is_widget_alive = lambda card: True
    win._flush_batch_immediate_renders = MagicMock()
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "sid-test"
    win._message_batch = []
    win._user_prefix_cache = [0]
    win._get_user_round_index_for_batch_index = lambda *a, **k: 0
    win._sanitize_user_message_for_display = lambda s: s
    win._restore_meta_from_batch = MagicMock()
    win._enqueue_card_diff_stats = MagicMock()
    win._apply_placeholder_height = MagicMock()
    win._pop_batch_overhead = MagicMock(return_value=None)
    win._batch_placeholders = {}
    win._take_batch_placeholder = lambda idx: None
    win.chat_layout = MagicMock()
    for key, value in overrides.items():
        setattr(win, key, value)
    return win


def _wire_create_path(win):
    """把建卡入口换成真实 `_add_chat_widget` 的入队行为复刻。

    关键：必须**正序** append 进 `_pending_lazy_cards` 并置
    `_lazy_batch_timer_active`——这正是把反序架空的那条路径。
    """
    built = []

    def _append_user(content, **kw):
        card = _FakeCard()
        built.append(card)
        win._pending_lazy_cards.append(card)
        win._lazy_batch_timer_active = True
        return card

    def _append_assistant(**kw):
        card = _FakeCard()
        built.append(card)
        win._pending_lazy_cards.append(card)
        win._lazy_batch_timer_active = True
        return card

    win._append_user_message = _append_user
    win._append_assistant_message = _append_assistant
    win._add_chat_widget = lambda w, insert_index=None: None
    return built


def _run(win, batch_count: int, batch_offset: int = 0, insert_at_top: bool = False):
    """跑一次真实 create 分支，返回 (队列顺序, 各批卡片列表)"""
    # 覆盖全表：建卡按 global_batch_index 写入，窗口前的槽位填 None
    win._batch_cards = [None] * (batch_offset + batch_count)
    batches = []
    for i in range(batch_count):
        # 合规结构（group_messages_for_display 口径）：user 单独成批，
        # assistant+tool 合并成批。user 批会建 1 张卡，assistant 批建 1 张卡。
        role = "user" if i % 2 == 0 else "assistant"
        batches.append([{"role": role, "content": f"c{i}"}])
    win._render_message_to_card(batches, insert_at_top=insert_at_top, batch_offset=batch_offset)
    order = list(win._pending_lazy_cards)
    return order, win._batch_cards


class TestCreatePathLazyOrder:
    def test_newest_batch_first_on_create_path(self, fake_message_card, no_qt_timer):
        """视口贴底 + 真实建卡路径 → 最新批次的卡必须排在队首"""
        win = _make_window()
        _wire_create_path(win)
        order, _ = _run(win, 5)

        assert len(order) == 5, f"应有 5 批各 1 卡，实际 {len(order)}"
        # 队首 = 最后一批（索引 4）
        assert order[0]._message_index == 4
        # 队尾 = 第一批（索引 0）
        assert order[-1]._message_index == 0

    def test_batch_indices_monotonic(self, fake_message_card, no_qt_timer):
        """批次索引沿队列单调递减（反序生效的直接证据）"""
        win = _make_window()
        _wire_create_path(win)
        order, _ = _run(win, 6)

        seq = [c._message_index for c in order]
        assert seq == sorted(seq, reverse=True), f"批次未反序：{seq}"

    def test_no_reverse_when_user_away(self, fake_message_card, no_qt_timer):
        """用户已滚离底部（上翻历史）→ 保持正序，旧批次优先"""
        win = _make_window(_user_intentionally_away_from_bottom=True)
        _wire_create_path(win)
        order, _ = _run(win, 4)

        assert order[0]._message_index == 0
        assert order[-1]._message_index == 3

    def test_no_reverse_on_insert_at_top(self, fake_message_card, no_qt_timer):
        """insert_at_top（prepend 历史批次）→ 保持正序"""
        win = _make_window()
        _wire_create_path(win)
        order, _ = _run(win, 4, insert_at_top=True)

        assert order[0]._message_index == 0
        assert order[-1]._message_index == 3

    def test_stale_cards_preserved_after_fresh(self, fake_message_card, no_qt_timer):
        """前几拍遗留在队列里的卡保持原相对顺序，排在本批新卡之后"""
        win = _make_window()
        stale_a, stale_b = _FakeCard(), _FakeCard()
        stale_a._message_index = -1
        stale_b._message_index = -2
        win._pending_lazy_cards = [stale_a, stale_b]
        _wire_create_path(win)
        order, _ = _run(win, 3)

        assert order[:2][0]._message_index == 2
        assert order[-2] is stale_a, "遗留卡相对顺序被改变"
        assert order[-1] is stale_b

    def test_no_duplicate_after_reorder(self, fake_message_card, no_qt_timer):
        """重排队列不得产生重复条目（同卡被渲染两次 = 内存翻倍）"""
        win = _make_window()
        _wire_create_path(win)
        order, _ = _run(win, 8)

        assert len(order) == len(set(id(c) for c in order)), "队列出现重复卡片"
