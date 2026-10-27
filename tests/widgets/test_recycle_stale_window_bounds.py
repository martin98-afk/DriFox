# -*- coding: utf-8 -*-
"""回收器对「过期可见窗口」的健壮性（IndexError 回归）。

## 用户可见现象

2026-10-20 真机：流式输出期间回收器每 500ms 崩一次
``IndexError: list index out of range``（``_recycle_out_of_view_batches``
above 循环 ``_batch_cards[batch_idx]``）。

## 根因

截断 / 删除 round / 重新分组路径只同步 ``_message_batch`` 与
``_batch_cards``（裁到新长度），**不同步** ``_visible_batch_start/end``。
窗口残留旧大值后：

- 几何口径 ``_viewport_batch_range()`` 不可用时（截断后滚动值未钳到位、
  布局零高、流式重排中），``_resolve_viewport_range()`` 退回加载窗口
  （fallback）→ 返回过期大值；
- ``active_start = vp_start - buffer > len(_batch_cards)``；
- 第一步循环与 1.5 步重建循环都有 ``batch_idx >= len`` 保护，唯独
  above 回收循环 ``range(0, active_start)`` 直读下标 → IndexError；
- timer 周期重试、失配不自愈 → 间歇反复崩（几何可用的拍不崩）。
"""

import sys
from unittest.mock import MagicMock

import pytest

from app.main_widget import OpenAIChatToolWindow


def _make_win(n_batches: int = 5):
    """`__new__` 最小桩（同 test_recycle_rendered_guard.py 法）。

    批次全部为 None（占位态），回收器应在界内安全遍历、不做任何回收动作。
    """
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_virtual_recycling = False
    win._batch_cards = [None] * n_batches
    win._message_batch = [[{"role": "assistant", "content": "x"}] for _ in range(n_batches)]
    win._batch_placeholders = {}
    win._batch_placeholder_overhead = {}
    win._current_assistant_card = None
    win._pending_lazy_cards = []
    win._last_visible_card_ids = set()
    # 过期窗口：结构已收缩到 5 批，窗口还停在 20..27
    win._visible_batch_start = 20
    win._visible_batch_end = 27
    win._incremental_visible_batch_count = 8
    win._virtual_scroll_buffer = 1
    win._is_loading_history_batches = False
    win._is_jump_in_flight = lambda: False
    # 几何不可用 → 走 fallback（返回过期窗口）
    win._viewport_batch_range = lambda: None
    win._is_widget_alive = lambda w: True
    win._batch_slot_gap = MagicMock(return_value=0)
    win._install_batch_placeholder = MagicMock(return_value=True)
    win._try_detach_card_viewer = lambda card: False
    win._render_message_to_card = MagicMock()
    win.chat_layout = MagicMock()
    win.chat_layout.indexOf.return_value = 0
    return win


def test_recycle_survives_stale_visible_window(qapp):
    """fallback 口径下过期窗口（start=20 > 批次=5）不得越界，也不得误回收。"""
    win = _make_win(n_batches=5)

    # 修复前：IndexError（above 循环 range(0, 12) 直读 _batch_cards[5..11]）
    win._recycle_out_of_view_batches()

    assert win._batch_cards == [None] * 5, "界内 None 批次不应被回收路径改写"
    win._install_batch_placeholder.assert_not_called()
    win._render_message_to_card.assert_not_called()
    assert win._is_virtual_recycling is False, "异常退出也不得把回收标志留在 True"


def test_recycle_survives_overflow_viewport_range(qapp):
    """几何口径返回超界批次号时同样不得越界（widget _message_index 残留场景）。"""
    win = _make_win(n_batches=5)
    win._viewport_batch_range = lambda: (20, 26)

    win._recycle_out_of_view_batches()

    assert win._is_virtual_recycling is False
    win._install_batch_placeholder.assert_not_called()


def test_sync_batch_structures_clamps_stale_window(qapp):
    """根治面：结构收缩时 _sync_batch_structures 必须把窗口夹回界内。"""
    win = _make_win(n_batches=10)
    win._visible_batch_start = 92
    win._visible_batch_end = 100
    win.session_manager = MagicMock()
    session = MagicMock()
    # 重新分组后只剩 5 批（截断/合并场景）：交替角色避免被合并成单批
    session.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(5)]
    win.session_manager.get_current_session.return_value = session
    win._build_user_prefix_cache = MagicMock()

    win._sync_batch_structures()

    assert len(win._batch_cards) == 5
    assert win._visible_batch_start < 5, "窗口 start 必须夹回新批次数内"
    assert win._visible_batch_end <= 5, "窗口 end 必须夹回新批次数内"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
