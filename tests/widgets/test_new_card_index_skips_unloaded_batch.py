# -*- coding: utf-8 -*-
"""新卡批次认领不得跨过「已卸载（留占位）」的历史批次。

## 用户可见现象

长会话往上翻读历史 → 视口外清理把底部批次卸载（`_batch_cards[idx] = None`，
布局原位留等高占位）→ 此时发消息 / 排队消息续发 / 子智能体回调建新卡。
新卡被登记进**历史批次下标**，于是：

- 历史那一问的回答位置永远空白（重建侧 `_batch_cards[idx] is not None` 守卫
  永久跳过该批，占位永不摘除）
- 新卡的 `_round_index` 被改写成历史轮次 → 删除 / 撤回 / 分支 / 卡片差异
  全部打到别的轮次上

## 根因

`_fix_new_card_message_index` 只把「`_batch_cards[idx]` 非空且含存活卡」判为
已占用。B9 卸载批次在 `_batch_cards` 里就是 None，于是被当成可认领的空位。

## 两层防线（本文件各覆盖一层）

1. 占用判据补「有批次占位 = 已占用」
2. 认领搜索只在尾部连续未占用段内进行，遇占用位即停（不跨过它抢更早的空位）
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from app.main_widget import OpenAIChatToolWindow
from app.widgets.message_card import MessageCard

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = REPO_ROOT / "app" / "main_widget.py"


def _make_window(qapp):
    """最小宿主：真实 layout + 真实 MessageCard，外部依赖全部打桩。"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    for key, value in {
        "_is_destroyed": False,
            "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False,
        "_virtual_scroll_buffer": 1,
        "_incremental_visible_batch_count": 8,
        "_batch_placeholders": {},
        "_batch_placeholder_overhead": {},
        "_message_batch": [],
        "_batch_cards": [],
        "_user_prefix_cache": [],
    }.items():
        setattr(win, key, value)

    win._create_message_widget = MagicMock(return_value=None)
    win._sync_single_card_width = MagicMock()
    win._virtual_scroll_timer = MagicMock()

    scroll_area = QScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    scroll_area.setWidget(container)
    win.chat_scroll_area = scroll_area
    win.chat_container = container
    win.chat_layout = layout
    return win


def _add_unloaded_batch(win, batch_idx: int, card: MessageCard, height: int = 200) -> None:
    """把一个已渲染批次改造成 B9 卸载态：卡离场、原位留等高占位、_batch_cards=None。"""
    win.chat_layout.removeWidget(card)
    card.hide()
    card.setParent(None)
    spacer = QWidget(win.chat_container)
    spacer.setFixedHeight(height)
    win.chat_layout.addWidget(spacer)
    win._batch_placeholders[batch_idx] = spacer
    win._batch_placeholder_overhead[batch_idx] = 60
    win._batch_cards[batch_idx] = None


def test_new_card_skips_unloaded_history_batch(qapp):
    """底部两批已卸载时，新卡的批次下标必须落在尾部新增段，不得抢占历史批次。"""
    win = _make_window(qapp)

    roles = ["user", "assistant"] * 3  # 6 批：0..5
    cards = []
    for idx, role in enumerate(roles):
        card = MessageCard(role=role, parent=win.chat_container)
        card.setFixedHeight(200)
        card._message_index = idx
        card._lazy_rendered = True
        win.chat_layout.addWidget(card)
        cards.append(card)
        win._message_batch.append([{"role": role, "content": f"历史消息 {idx}"}])
        win._batch_cards.append([card])

    # 末两批（历史 user2 / assistant2）被视口外清理卸载
    _add_unloaded_batch(win, 4, cards[4])
    _add_unloaded_batch(win, 5, cards[5])

    # 新消息：session 已追加新 user 批（group_messages_for_display 结果）
    win._message_batch.append([{"role": "user", "content": "本轮新问题"}])
    win._batch_cards.append(None)
    win._user_prefix_cache = [0] * (len(win._message_batch) + 1)

    new_user = MessageCard(role="user", parent=win.chat_container)
    win.chat_layout.addWidget(new_user)
    new_assistant = MessageCard(role="assistant", parent=win.chat_container)
    win.chat_layout.addWidget(new_assistant)

    win._fix_new_card_message_index(user_text="本轮新问题")

    # 卸载批次保持卸载态，绝不能被新卡顶掉
    assert win._batch_cards[4] is None, "已卸载批次不得被新卡认领（否则历史回答位置永久空白）"
    assert win._batch_cards[5] is None, "已卸载批次不得被新卡认领（否则历史回答位置永久空白）"
    assert set(win._batch_placeholders) == {4, 5}, "历史批次的等高占位必须保留，等待原位重建"

    # 新卡的下标不得指向历史批次
    assert new_user._message_index == 6, "新 user 卡应落在尾部新增批（索引 6，即新 user 批）"
    assert new_assistant._message_index > 6, f"新 assistant 卡不得占用历史批次下标，实得 {new_assistant._message_index}"


def test_assignment_does_not_cross_occupied_batch(qapp):
    """占据中段后，不得跨过占用位去认领更早的同角色空位。

    构造：历史 4 批全渲染（占用），其后一批已卸载（占用），尾部新增一批。
    新 assistant 卡只能落到尾部 append 槽，不能回填到已卸载批。
    """
    win = _make_window(qapp)

    roles = ["user", "assistant"] * 2  # 4 批：0..3
    cards = []
    for idx, role in enumerate(roles):
        card = MessageCard(role=role, parent=win.chat_container)
        card.setFixedHeight(200)
        card._message_index = idx
        card._lazy_rendered = True
        win.chat_layout.addWidget(card)
        cards.append(card)
        win._message_batch.append([{"role": role, "content": f"历史消息 {idx}"}])
        win._batch_cards.append([card])

    # 追加一批 assistant 并卸载（占位保留，模拟「卸载后又滚回来前又发了新消息」）
    old_assistant = MessageCard(role="assistant", parent=win.chat_container)
    win.chat_layout.addWidget(old_assistant)
    win._message_batch.append([{"role": "assistant", "content": "被卸载的老回答"}])
    win._batch_cards.append([old_assistant])
    _add_unloaded_batch(win, 4, old_assistant)

    # 尾部新增 user 批
    win._message_batch.append([{"role": "user", "content": "新问题"}])
    win._batch_cards.append(None)
    win._user_prefix_cache = [0] * (len(win._message_batch) + 1)

    new_user = MessageCard(role="user", parent=win.chat_container)
    win.chat_layout.addWidget(new_user)
    new_assistant = MessageCard(role="assistant", parent=win.chat_container)
    win.chat_layout.addWidget(new_assistant)

    win._fix_new_card_message_index(user_text="新问题")

    assert win._batch_cards[4] is None, "跨过占用位认领 = 把新卡挂到别的轮次上"
    assert 4 in win._batch_placeholders
    assert new_user._message_index == 5
    assert new_assistant._message_index >= 6, f"新 assistant 卡应 append 到尾部，实得 {new_assistant._message_index}"


def test_occupied_predicate_covers_placeholders():
    """源码锁：占用判据必须包含「有批次占位 = 已占用」这一条。

    行为测只覆盖一种时序；这条守住语义本身不被后续重构删掉
    （删掉后行为测仍可能因 break 防线而通过，但卸载批的语义再次失守）。
    """
    src = SRC_PATH.read_text(encoding="utf-8")
    start = src.index("def _fix_new_card_message_index(")
    end = src.index("def _restore_meta_from_batch(", start)
    body = src[start:end]

    assert "placeholders" in body and "assigned_batch_indices.add(batch_idx)" in body, (
        "占用判据必须把「已卸载但留占位」的批次算作已占用（B9 语义：数据还在，不是空位）"
        "；只按 _batch_cards 非空判定会让新卡认领历史批次下标"
    )
    assert "break" in body, "认领搜索遇占用位必须停止，不得跨过它去抢更早的历史空位"


def test_rebuild_removes_stale_placeholder_when_card_present(qapp):
    """矛盾态自愈 A：批次已有卡片却还挂着占位 → 重建时摘掉残留占位。

    这是上一条防线失守（或历史数据已错配）后残留的形态。留着占位会让该位置
    多出一块等高空白，且 `_viewport_batch_range` 把同一 idx 映射到两个布局项。
    """
    win = _make_window(qapp)
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "s1"
    win._get_user_round_index_for_batch_index = MagicMock(return_value=0)
    win._is_widget_alive = lambda w: w is not None
    win._user_intentionally_away_from_bottom = False
    win._enqueue_card_diff_stats = MagicMock()
    win._restore_meta_from_batch = MagicMock()
    win._flush_batch_immediate_renders = MagicMock()
    win._process_next_lazy_batch = MagicMock()
    win._append_assistant_message = MagicMock(return_value=None)
    win._append_user_message = MagicMock(return_value=None)

    card = MessageCard(role="assistant", parent=win.chat_container)
    card.setFixedHeight(200)
    card._message_index = 0
    card._lazy_rendered = True
    win.chat_layout.addWidget(card)
    win._message_batch = [[{"role": "assistant", "content": "已有内容"}]]
    win._batch_cards = [[card]]

    # 矛盾态：卡片在册，占位也还在
    spacer = QWidget(win.chat_container)
    spacer.setFixedHeight(200)
    win.chat_layout.addWidget(spacer)
    win._batch_placeholders[0] = spacer
    win._batch_placeholder_overhead[0] = 60

    anchor = win.chat_layout.indexOf(spacer)
    win._render_message_to_card(win._message_batch[0:1], batch_offset=0, anchor_layout_index=anchor)

    assert 0 not in win._batch_placeholders, "已有卡片的批次不得残留占位（会多出一块等高空白）"
    assert win.chat_layout.indexOf(spacer) < 0, "残留占位必须从布局摘除"
    assert win.chat_layout.indexOf(card) >= 0, "已存在的卡片必须留在布局中"


def test_rebuild_skips_card_whose_index_conflicts(qapp):
    """矛盾态自愈 B：卡片自称下标与本批次不符 → 不重插（防串到别的问题下）。

    历史版本认领逻辑把新卡挂到历史批次上后，重建该历史批次时若把这张卡搬过来，
    等于把别的轮次的内容挪到这条问题下面。宁可跳过，等权威路径修正归属。
    """
    win = _make_window(qapp)
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "s1"
    win._get_user_round_index_for_batch_index = MagicMock(return_value=0)
    win._is_widget_alive = lambda w: w is not None
    win._user_intentionally_away_from_bottom = False
    win._enqueue_card_diff_stats = MagicMock()
    win._restore_meta_from_batch = MagicMock()
    win._flush_batch_immediate_renders = MagicMock()
    win._process_next_lazy_batch = MagicMock()
    win._append_assistant_message = MagicMock(return_value=None)
    win._append_user_message = MagicMock(return_value=None)

    # 批次 0 的历史 assistant 卡，_batch_cards[0] 却被错配成「自称下标 3」的新卡
    misplaced = MessageCard(role="assistant", parent=win.chat_container)
    misplaced.setFixedHeight(200)
    misplaced._message_index = 3
    misplaced._lazy_rendered = True
    win.chat_layout.addWidget(misplaced)
    win._message_batch = [[{"role": "assistant", "content": "历史回答"}]]
    win._batch_cards = [[misplaced]]
    win._batch_placeholders = {}
    win._batch_placeholder_overhead = {}

    win._render_message_to_card(win._message_batch[0:1], batch_offset=0, anchor_layout_index=0)

    assert win.chat_layout.indexOf(misplaced) >= 0, "错配卡不应被销毁（归属由权威路径修正）"
    idx_list = [win.chat_layout.itemAt(i).widget() for i in range(win.chat_layout.count())]
    assert idx_list.count(misplaced) == 1, "错配卡不得被重复插入布局"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
