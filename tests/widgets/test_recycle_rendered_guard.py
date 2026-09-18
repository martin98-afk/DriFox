# -*- coding: utf-8 -*-
"""回收守卫：未渲染批次不得卸载（滚轮跳动修复回归）。

## 用户可见现象

超长消息列表滚动中滚轮位置异常跳动。根因链：快速拖动滚动条跳过的批次从未
懒渲染，卡片高度停在起步值（40px 级）；旧回收逻辑照常卸载并按此失真高度装
「等高占位」→ 容器总高骤缩 → 滚动条 maximum 崩、value 被钳 → 视口瞬跳。
随后批次滚回视口重建，卡片从 40px 长到真实高度，连环滚动补偿再次拽动视口。

## 守卫语义

`_batch_fully_rendered`：批次内所有存活卡片 `_lazy_rendered=True` 才可回收。
- user 卡气泡模式下恒 True（PlainTextViewer 非懒渲染），不受影响；
- 未渲染卡没有 QWebEngineView、不占渲染配额，回收零收益纯风险；
- `_unload_batch`（LRU/强回收入口）与 `_recycle_out_of_view_batches`
  （视口回收器）两侧都必须守。
"""

import sys
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from app.main_widget import OpenAIChatToolWindow
from app.widgets.message_card import MessageCard


def _make_win():
    """`__new__` 最小桩（同 test_render_quota_enforcement.py 法）。"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_widget_alive = lambda w: w is not None
    win._batch_cards = []
    win._batch_placeholders = {}
    win._last_visible_card_ids = set()
    win._rendered_card_count = 0
    win._unloaded_pids = []
    win._current_assistant_card = None
    win._bump_layout_epoch = MagicMock()
    win._decr_rendered_count = MagicMock()
    win._register_unloaded_pid = MagicMock()
    win._try_detach_card_viewer = lambda card: False
    win._install_batch_placeholder = MagicMock(return_value=True)
    win.chat_layout = MagicMock()
    win.chat_layout.indexOf.return_value = 0
    return win


def _card(lazy: bool) -> MessageCard:
    card = MessageCard(role="assistant")
    card.setFixedHeight(300)
    card._lazy_rendered = lazy
    return card


def test_batch_fully_rendered_semantics(qapp):
    """渲染态判定：未渲染 assistant → False；已渲染 → True；非卡/死引用忽略。"""
    win = _make_win()
    win._is_widget_alive = lambda w: True
    rendered = _card(lazy=True)
    unrendered = _card(lazy=False)

    assert win._batch_fully_rendered([rendered]) is True
    assert win._batch_fully_rendered([rendered, unrendered]) is False
    assert win._batch_fully_rendered([unrendered]) is False
    # 非 MessageCard 引用（mock/其它 widget）不参与判定
    assert win._batch_fully_rendered([MagicMock(), rendered]) is True
    # 空批次：无可卸内容，按「可回收」处理（调用方自身有空批次短路）
    assert win._batch_fully_rendered([]) is True
    assert win._batch_fully_rendered(None) is True


def test_unload_batch_skips_unrendered_batch(qapp):
    """未渲染批次整批跳过：返回 0、_batch_cards 不置 None、不装占位。"""
    win = _make_win()
    win._is_widget_alive = lambda w: True
    card = _card(lazy=False)
    win._batch_cards = [[card]]

    removed = win._unload_batch(0)

    assert removed == 0
    assert win._batch_cards[0] == [card], "未渲染批次必须保留 UI 引用"
    win._install_batch_placeholder.assert_not_called()
    win._bump_layout_epoch.assert_not_called()


def test_unload_batch_still_unloads_rendered_batch(qapp):
    """已渲染批次行为不变：正常卸载、置 None、装等高占位。"""
    win = _make_win()
    win._is_widget_alive = lambda w: True
    card = _card(lazy=True)
    win._batch_cards = [[card]]
    # delete_widgets_from_layout 走真实函数，需要真实布局才能摘干净
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.addWidget(card)
    win.chat_layout = layout

    removed = win._unload_batch(0)

    assert removed == 0, "等高占位安装成功时净高度为 0"
    assert win._batch_cards[0] is None
    win._install_batch_placeholder.assert_called_once()


def test_unload_batch_mixed_batch_is_atomic(qapp):
    """混合批次（含未渲染卡）整批保留，不允许半批次卸载。"""
    win = _make_win()
    win._is_widget_alive = lambda w: True
    ok = _card(lazy=True)
    bad = _card(lazy=False)
    win._batch_cards = [[ok, bad]]

    removed = win._unload_batch(0)

    assert removed == 0
    assert win._batch_cards[0] == [ok, bad], "部分渲染批次必须整批保留"


# ─── 第二步：视口内占位同步重建（不等 500ms 防抖） ───────────────────


def _make_restore_win(vp_range):
    win = _make_win()
    win._is_virtual_recycling = False
    win._message_batch = [[{"role": "assistant", "content": "x"}] for _ in range(10)]
    win._batch_cards = [None] * 10
    win.chat_layout = MagicMock()
    win.chat_layout.indexOf.return_value = 7
    win._viewport_batch_range = lambda: vp_range
    return win


def test_restore_placeholders_only_in_viewport(qapp):
    """只重建与视口直接相交的占位批次；邻域/视口外不碰。"""
    win = _make_restore_win((5, 5))
    win._batch_placeholders = {5: MagicMock(), 2: MagicMock(), 8: MagicMock()}
    calls = []
    win._render_message_to_card = lambda batches, batch_offset=0, anchor_layout_index=None: calls.append(
        (batch_offset, anchor_layout_index)
    )

    n = win._restore_placeholders_in_viewport()

    assert n == 1
    assert calls == [(5, 7)], "视口内批次按原布局位重建；视口外占位留给防抖路径"


def test_restore_placeholders_skips_rendered_or_empty_batches(qapp):
    """批次已渲染（非 None）或数据为空时跳过，不重复建卡。"""
    win = _make_restore_win((0, 9))
    card = _card(lazy=True)
    win._batch_cards = [[card], None, None, None, None, None, None, None, None, None]
    win._message_batch = [
        [{"role": "assistant", "content": "x"}],
        [],
        [],
        *([{"role": "assistant", "content": "x"}] for _ in range(7)),
    ]
    win._batch_placeholders = {0: MagicMock(), 1: MagicMock(), 2: MagicMock()}
    calls = []
    win._render_message_to_card = lambda batches, batch_offset=0, anchor_layout_index=None: calls.append(batch_offset)

    n = win._restore_placeholders_in_viewport()

    assert calls == [], "已渲染批次 / 空数据批次都不得重建"
    assert n == 0


def test_restore_placeholders_noop_guards(qapp):
    """回收进行中 / 几何不可用时直接返回 0。"""
    win = _make_restore_win((5, 5))
    win._batch_placeholders = {5: MagicMock()}
    win._render_message_to_card = MagicMock()

    win._is_virtual_recycling = True
    assert win._restore_placeholders_in_viewport() == 0

    win._is_virtual_recycling = False
    win._viewport_batch_range = lambda: None
    assert win._restore_placeholders_in_viewport() == 0
    win._render_message_to_card.assert_not_called()


def test_scroll_sync_uses_sync_restore_for_in_view_placeholders():
    """源码断言：滚动同步回调对「视口内占位」走同步重建，邻域保留防抖唤醒。

    行为测覆盖不了 100ms 防抖时序，用源码锁定关键分派关系：
    相交 → `_restore_placeholders_in_viewport()`；仅邻域 → timer 提前启动。
    """
    with open("app/main_widget.py", encoding="utf-8") as f:
        src = f.read()
    start = src.index("def _sync_visible_cards_on_scroll(")
    body = src[start : src.index("def _restore_card_now(", start)]
    assert "self._restore_placeholders_in_viewport()" in body, "视口相交占位必须同步重建"
    assert "self._virtual_scroll_timer.start()" in body, "邻域占位保留防抖提前唤醒路径"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
