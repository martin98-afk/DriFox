# -*- coding: utf-8 -*-
"""撤销删除：条目栈语义 + 卡片 TTL/信号契约 + 回退条目切片（回归测试）

覆盖 2026-09-11 优化点：
1. 单步裸 dict `_undo_delete_cache` → 类型化 LIFO 栈 `UndoDeleteStore`；
2. 卡片被遮挡（`hide_card`）不再清空回退条目 —— 只有 ✕ / TTL 到期才失效；
3. TTL 固定 60s 自动消失（不再走系统设置项）；
4. `_build_undo_entry` 的切片口径（删单轮 vs 撤销到某轮）。

运行: uv run pytest tests/widgets/test_undo_delete_store.py -v
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import app.main_widget as mw
from app.core.chat_session import ChatSession, SessionManager
from app.core.message_content import group_messages_for_display
from app.widgets.cards.floating.undo_delete_store import (
    KIND_DELETE_ROUND,
    KIND_UNDO_TO_ROUND,
    UndoDeleteStore,
    UndoEntry,
)


def _entry(kind: str, count: int = 2, layout_index=None, session_id: str = "s1") -> UndoEntry:
    return UndoEntry(
        session_id=session_id,
        messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}][:count],
        insert_index=0,
        count=count,
        kind=kind,
        layout_index=layout_index,
    )


# ───────────────────────────────────────────────────────────
# 仓库：LIFO / 上限 / 失效
# ───────────────────────────────────────────────────────────


def test_store_lifo_and_depth():
    store = UndoDeleteStore()
    assert not store and store.depth == 0 and store.peek() is None

    first, second = _entry(KIND_DELETE_ROUND, 1), _entry(KIND_DELETE_ROUND, 3)
    store.push(first)
    store.push(second)

    assert store.depth == 2
    assert store.peek() is second, "peek 必须返回栈顶（最近一次操作）"
    assert store.pop() is second
    assert store.pop() is first
    assert store.pop() is None, "空栈 pop 必须返回 None（幂等，不抛异常）"


def test_store_trims_oldest_beyond_limit():
    store = UndoDeleteStore(max_entries=3)
    for i in range(5):
        store.push(_entry(KIND_DELETE_ROUND, i + 1))

    assert store.depth == 3
    # 最旧的两次被丢弃，栈内保留 3/4/5
    assert [e.count for e in store.entries()] == [3, 4, 5]


def test_store_clear_reports_dropped():
    store = UndoDeleteStore()
    store.push(_entry(KIND_DELETE_ROUND))
    store.push(_entry(KIND_UNDO_TO_ROUND))

    assert store.clear("dismiss") == 2
    assert store.clear("dismiss") == 0
    assert store.depth == 0


def test_entry_label_distinguishes_kinds():
    """两种语义必须给不同文案（旧实现共用一句「已删除 N 条消息」）"""
    assert _entry(KIND_DELETE_ROUND, 3).label == "已删除 3 条消息"
    assert _entry(KIND_UNDO_TO_ROUND, 7).label == "已撤销 7 条消息"


def test_entry_tail_restore_flag():
    """撤销到某轮移除的是尾部 → 恢复时直接追加，不需要布局锚点"""
    assert _entry(KIND_UNDO_TO_ROUND).appends_at_tail is True
    assert _entry(KIND_DELETE_ROUND, layout_index=2).appends_at_tail is False


# ───────────────────────────────────────────────────────────
# 卡片：TTL / 信号语义
# ───────────────────────────────────────────────────────────


def test_card_ttl_default_is_60s(qapp):
    from app.widgets.cards.floating.undo_delete_card import UndoDeleteCard

    card = UndoDeleteCard()
    try:
        assert UndoDeleteCard.TTL_SECONDS == 60
        card.restart_ttl()
        assert card._ttl_timer.isActive()
        assert card._ttl_timer.interval() == 60_000
    finally:
        card.deleteLater()


def test_card_hide_does_not_close_undo_window(qapp):
    """被 CardManager 遮挡只发 dismissed，不得发 dismissRequested（否则误清条目）"""
    from app.widgets.cards.floating.undo_delete_card import UndoDeleteCard

    card = UndoDeleteCard()
    dismissed, dismiss_requested = [], []
    card.dismissed.connect(lambda: dismissed.append(1))
    card.dismissRequested.connect(lambda: dismiss_requested.append(1))
    try:
        card.hide_card()
        assert dismissed == [1]
        assert dismiss_requested == [], "遮挡不应关闭撤销窗口"
    finally:
        card.deleteLater()


def test_card_ttl_timeout_requests_dismiss(qapp):
    from app.widgets.cards.floating.undo_delete_card import UndoDeleteCard

    card = UndoDeleteCard()
    fired = []
    card.dismissRequested.connect(lambda: fired.append(1))
    try:
        card.restart_ttl()
        card._on_ttl_timeout()
        assert fired == [1]
        assert card._ttl_remaining == 0
    finally:
        card.deleteLater()


def test_card_buttons_emit_expected_signals(qapp):
    from app.widgets.cards.floating.undo_delete_card import UndoDeleteCard

    card = UndoDeleteCard()
    restored, dismissed = [], []
    card.restoreRequested.connect(lambda: restored.append(1))
    card.dismissRequested.connect(lambda: dismissed.append(1))
    try:
        card._restore_btn.click()
        card._close_btn.click()
        assert restored == [1]
        assert dismissed == [1]
    finally:
        card.deleteLater()


def test_card_depth_label_only_for_multi_step(qapp):
    from app.widgets.cards.floating.undo_delete_card import UndoDeleteCard

    card = UndoDeleteCard()
    try:
        card.set_entry("已删除 3 条消息", depth=1)
        assert card._hint_label.text() == "已删除 3 条消息"
        assert card._depth_label.isHidden()

        card.set_entry("已删除 3 条消息", depth=4)
        assert not card._depth_label.isHidden()
        assert "4" in card._depth_label.text()
    finally:
        card.deleteLater()


# ───────────────────────────────────────────────────────────
# 回退条目切片口径
# ───────────────────────────────────────────────────────────


def _make_widget(messages: list) -> mw.OpenAIChatToolWindow:
    widget = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    widget.session_manager = SessionManager()
    session = ChatSession(messages=list(messages))
    widget.session_manager.sessions.append(session)
    widget.session_manager.current_index = 0
    return widget


_THREE_ROUNDS = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"},
    {"role": "assistant", "content": "a2"},
    {"role": "user", "content": "q3"},
    {"role": "assistant", "content": "a3"},
]


def test_build_undo_entry_delete_round_slices_single_round():
    widget = _make_widget(_THREE_ROUNDS)
    session = widget.session_manager.get_current_session()

    entry = widget._build_undo_entry(session, 1, KIND_DELETE_ROUND, layout_index=2)

    assert entry is not None
    assert entry.kind == KIND_DELETE_ROUND
    assert entry.insert_index == 2, "第 2 轮从 canonical 下标 2 开始"
    assert entry.count == 2
    assert [m["content"] for m in entry.messages] == ["q2", "a2"]
    assert entry.layout_index == 2
    assert entry.session_id == session.session_id


def test_build_undo_entry_undo_to_round_slices_tail():
    widget = _make_widget(_THREE_ROUNDS)
    session = widget.session_manager.get_current_session()

    entry = widget._build_undo_entry(session, 1, KIND_UNDO_TO_ROUND, layout_index=2)

    assert entry is not None
    assert entry.count == 4
    assert [m["content"] for m in entry.messages] == ["q2", "a2", "q3", "a3"]
    assert entry.layout_index is None, "撤销到某轮是尾部移除，恢复时直接追加"


def test_build_undo_entry_rejects_invalid_round():
    widget = _make_widget(_THREE_ROUNDS)
    session = widget.session_manager.get_current_session()

    assert widget._build_undo_entry(session, 99, KIND_DELETE_ROUND, layout_index=0) is None
    assert widget._build_undo_entry(session, None, KIND_DELETE_ROUND) is None


def test_build_undo_entry_summarizes_first_user_text():
    widget = _make_widget(_THREE_ROUNDS)
    session = widget.session_manager.get_current_session()

    entry = widget._build_undo_entry(session, 2, KIND_DELETE_ROUND, layout_index=4)
    assert entry.note == "q3"


def test_batch_index_matches_display_grouping_for_middle_insert():
    """增量恢复依赖：insert_at 前的展示批次数量 == 恢复区域起始批次下标

    注意一个 round 会产生 **两个** 批次：user 自成一批，其后的 assistant/tool
    消息累积成下一批（见 group_messages_for_display）。
    """
    messages = [dict(m) for m in _THREE_ROUNDS]
    # 删除第 2 轮后的会话形态：[q1, a1, q3, a3] → 4 个批次
    after_delete = messages[:2] + messages[4:]
    assert len(group_messages_for_display(after_delete)) == 4

    restored = messages[2:4]  # q2, a2
    insert_at = 2
    new_messages = after_delete[:insert_at] + restored + after_delete[insert_at:]
    all_batches = group_messages_for_display(new_messages)
    batch_index = len(group_messages_for_display(new_messages[:insert_at]))
    restored_count = len(all_batches) - len(group_messages_for_display(after_delete))

    assert batch_index == 2, "恢复区域应插在 q1/a1 两个批次之后"
    assert restored_count == 2, "q2 与 a2 各占一个批次"
    assert [b[0]["content"] for b in all_batches[batch_index : batch_index + restored_count]] == ["q2", "a2"]
