# -*- coding: utf-8 -*-
"""回归：删除轮次/清幽灵插话前必须实化占位批次（防卸载批次永久丢失）。

## 用户可见现象（2026-11-08 真机）

长会话向上翻阅（视口外清理把历史批次卸载，布局留等高占位）→ 删除某轮对话 →
撤销恢复：卸载批次的用户气泡/回复永久消失、布局顺序错乱（老回复插到新消息
后面）、历史跳转与撤销定位全部打偏。

## 根因

`_rebuild_batch_cards_from_layout` 按「布局存活卡顺序」重排 `_batch_cards`，
并把占位全摘掉。虚拟滚动下卸载批次只有占位没有卡：

1. 摘占位后所有重建入口（`_restore_placeholders_in_viewport` /
   `_recycle_out_of_view_batches` 第 1.5 步 / `_restore_batch_ui`，全部以
   「占位存在」为触发条件）永久失效 → 该批次数据槽位 ``None`` 且无占位 →
   永久空白（用户气泡随之消失）；
2. 存活卡被按顺序塞进前面空出的槽位 → ``_message_index`` /
   ``_round_index`` 整体错位 → 后续回收按错位下标销毁别人的卡、定位打偏。

## 修复

`_materialize_placeholder_batches_for_rebuild`：在删除 UI 前把所有占位批次
原位实化为卡片（<1ms/张，删除是低频操作），让「布局内容 == 批次全量」的
重排前提重新成立。调用点：`_delete_user_round`、`_remove_interject_ghost_card`。

运行: uv run pytest tests/widgets/test_delete_materializes_unloaded_batches.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import app.main_widget as mw
from app.core.conversation.chat_session import ChatSession, SessionManager
from app.widgets.message_card import MessageCard

BATCH_H = 200


def _round_msgs(i: int):
    return [
        {"role": "user", "content": f"问题{i}"},
        {"role": "assistant", "content": f"回答{i}"},
    ]


class _Win:
    """最小宿主：真实 layout + 真实 MessageCard，删除链外部依赖全部打桩。"""

    def __init__(self, n_rounds: int = 4):
        self.win = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
        w = self.win
        w.session_manager = SessionManager()
        messages = []
        for i in range(n_rounds):
            messages.extend(_round_msgs(i))
        session = ChatSession(messages=messages)
        w.session_manager.sessions.append(session)
        w.session_manager.current_index = 0
        w.history_manager = MagicMock()
        w._current_session_id = session.session_id
        w._current_project = None
        w._is_streaming = False
        w._truncation_sentinel = None
        w._is_virtual_recycling = False
        w._is_loading_history_batches = False
        w._visible_batch_start = 0
        w._visible_batch_end = n_rounds * 2
        w._batch_placeholders = {}
        w._batch_placeholder_overhead = {}
        w._message_batch = []
        w._batch_cards = []
        w._user_prefix_cache = []
        w._pending_lazy_cards = []
        w._lazy_batch_timer_active = False
        w._last_visible_card_ids = set()
        w._resize_preview_active = False
        w._user_intentionally_away_from_bottom = False

        # 真实布局
        w.chat_scroll_area = QScrollArea()
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        w.chat_scroll_area.setWidget(container)
        w.chat_container = container
        w.chat_layout = layout

        # 打桩：删除链与渲染链的外围
        w.virtual_scroll_timer = MagicMock()
        w._virtual_scroll_timer = MagicMock()
        w._sync_single_card_width = MagicMock()
        w._persist_session_after_mutation = MagicMock()
        w._finalize_local_session_mutation = MagicMock()
        w._update_history_questions_badge = MagicMock()
        w._recount_rendered_cards = MagicMock()
        w._get_load_msg_extras_fn = MagicMock(return_value=None)
        w._restore_meta_from_batch = MagicMock()
        w._enqueue_card_diff_stats = MagicMock()
        w._update_node_preview = MagicMock()
        w._should_follow_bottom = MagicMock(return_value=False)
        w._refresh_context_usage_indicator = MagicMock()
        w._build_undo_entry = MagicMock(return_value=None)

        # 建卡桩：走真实 MessageCard + 真实布局插入（保留对齐语义）
        self._build_card_factories()

        # 批次结构：按 session 分组 + 建卡入布局
        from app.core.conversation.message_content import group_messages_for_display

        w._message_batch = group_messages_for_display(session.messages)
        self.cards = []
        for idx, batch in enumerate(w._message_batch):
            role = batch[0].get("role")
            card = MessageCard(role=role, parent=container)
            card.setFixedHeight(BATCH_H)
            card._message_index = idx
            card._round_index = idx // 2
            card._lazy_rendered = True
            if role == "user":
                layout.addWidget(card, 0, Qt.AlignRight)
            else:
                layout.addWidget(card, 0, Qt.AlignLeft)
            w._batch_cards.append([card])
            self.cards.append(card)
        w._build_user_prefix_cache()

    def _build_card_factories(self):
        w = self.win

        def fake_append_user(content, **kwargs):
            card = MessageCard(role="user", parent=w.chat_container)
            card._round_index = kwargs.get("user_round_index")
            idx = kwargs.get("insert_index")
            if idx is not None:
                w.chat_layout.insertWidget(idx, card, 0, Qt.AlignRight)
            else:
                w.chat_layout.addWidget(card, 0, Qt.AlignRight)
            return card

        def fake_append_assistant(**kwargs):
            card = MessageCard(role="assistant", parent=w.chat_container)
            card._round_index = kwargs.get("round_index")
            idx = kwargs.get("insert_index")
            if idx is not None:
                w.chat_layout.insertWidget(idx, card, 0, Qt.AlignLeft)
            else:
                w.chat_layout.addWidget(card, 0, Qt.AlignLeft)
            return card

        w._append_user_message = fake_append_user
        w._append_assistant_message = fake_append_assistant

    def unload_batches(self, batch_idxs):
        """把指定批次改造成卸载态：卡离场、原位留等高占位、_batch_cards=None。"""
        w = self.win
        for idx in batch_idxs:
            card = self.cards[idx]
            pos = w.chat_layout.indexOf(card)
            assert pos >= 0
            w.chat_layout.removeWidget(card)
            card.hide()
            card.setParent(None)
            spacer = QWidget(w.chat_container)
            spacer.setFixedHeight(BATCH_H)
            w.chat_layout.insertWidget(pos, spacer)
            w._batch_placeholders[idx] = spacer
            w._batch_placeholder_overhead[idx] = 60
            w._batch_cards[idx] = None

    def layout_cards(self):
        w = self.win
        return [
            w.chat_layout.itemAt(i).widget()
            for i in range(w.chat_layout.count())
            if isinstance(w.chat_layout.itemAt(i).widget(), MessageCard)
        ]


def test_materialize_restores_placeholder_batches():
    """实化 helper：占位批次必须原位建卡、摘占位、登记回 _batch_cards。"""
    env = _Win(n_rounds=4)
    w = env.win
    env.unload_batches([0, 1])
    assert w._batch_cards[0] is None and w._batch_cards[1] is None

    restored = w._materialize_placeholder_batches_for_rebuild()

    assert restored == 2, f"应实化 2 个占位批次，实际 {restored}"
    assert not w._batch_placeholders, "实化后占位表必须清空"
    for idx in (0, 1):
        cards = w._batch_cards[idx]
        assert cards, f"批次 {idx} 实化后 _batch_cards 仍为空 → 永久空洞"
        card = cards[0]
        assert w.chat_layout.indexOf(card) >= 0, f"批次 {idx} 的卡不在布局中"
        assert card._message_index == idx, f"批次 {idx} 卡的 _message_index={card._message_index}"


def test_delete_user_round_no_hole_and_alignment():
    """端到端：卸载批次存在时删除轮次，重排后不得出现空洞批次或错位登记。

    未修复时：占位被 _rebuild_batch_cards_from_layout 摘掉且批次 0/1 无卡 →
    _batch_cards[0/1] 变 None 且无占位（永久空洞）；u2/a2/u3/a3 的卡被塞进
    前移的槽位（_message_index 错位）。
    """
    env = _Win(n_rounds=4)
    w = env.win
    env.unload_batches([0, 1])

    target = env.cards[6]  # 第 4 轮 user 卡
    target._round_index = 3
    w._delete_user_round(target)

    session = w.session_manager.get_current_session()
    assert len(session.messages) == 6, f"session 应剩 3 轮 6 条，实际 {len(session.messages)}"
    assert len(w._message_batch) == 6, f"批次表应收缩到 6，实际 {len(w._message_batch)}"
    assert len(w._batch_cards) == 6

    # 无永久空洞：每个槽位必须有卡（修复后删除前已全部实化）
    for idx in range(6):
        cards = w._batch_cards[idx]
        assert cards, f"批次 {idx} 为 None 且无占位 = 永久空洞（用户气泡丢失根因）"
        card = cards[0]
        assert card._message_index == idx, (
            f"批次 {idx} 的卡登记错位（_message_index={card._message_index}）→ 回收/定位全打偏"
        )

    # 布局顺序 == 批次顺序（乱序回归）
    layout_cards = env.layout_cards()
    assert len(layout_cards) == 6, f"布局应有 6 张卡，实际 {len(layout_cards)}"
    for pos, card in enumerate(layout_cards):
        assert card._message_index == pos, f"布局第 {pos} 张卡对应批次 {card._message_index}，顺序错乱"
        expected_role = w._message_batch[pos][0].get("role")
        assert card.role == expected_role, f"布局第 {pos} 张卡 role={card.role}，应为 {expected_role}"


def test_delete_with_unloaded_batches_keeps_session_rounds():
    """卸载轮次的数据不得被删除操作误伤：删第 4 轮，前 3 轮 session 数据完整。"""
    env = _Win(n_rounds=4)
    w = env.win
    env.unload_batches([2, 3])  # 卸载中间轮次（第 2 轮）的批次

    target = env.cards[6]  # 第 4 轮 user 卡
    target._round_index = 3
    w._delete_user_round(target)

    session = w.session_manager.get_current_session()
    contents = [m.get("content") for m in session.messages]
    assert contents == ["问题0", "回答0", "问题1", "回答1", "问题2", "回答2"], (
        f"卸载轮次的消息被误删：{contents}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
