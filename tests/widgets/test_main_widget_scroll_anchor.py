# -*- coding: utf-8 -*-
"""回归测试：向上滚动加载历史批次时，懒渲染完成不得把视口拉回底部

Bug 复现路径：
1. 会话初始加载（_loading_session=True）懒渲染批次进行中，或加载更多历史后
2. 用户向上滚动到顶 → _on_scroll_changed → _load_more_history_batches()
3. 新批次卡片进入 _pending_lazy_cards，_process_next_lazy_batch 继续处理
4. 修复前：置底分支（_loading_session 为 True 时 setValue(maximum)）与
   sticky 滚底分支（pending 清空后 _scroll_to_bottom(sticky_ms=900)）未考虑
   _user_intentionally_away_from_bottom，把视口强制拉回底部。

修复：两个置底路径都受 _user_intentionally_away_from_bottom 保护；
_on_scroll_changed 滚回底部附近时复位该标志。

测试策略：不实例化完整窗口（依赖过多），用 __new__ + MagicMock 构造
_process_next_lazy_batch 所需的最小属性，验证置底/滚底是否被触发。
"""

import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "app")

from app.main_widget import OpenAIChatToolWindow  # noqa: E402


def _make_window(**overrides):
    """构造 _process_next_lazy_batch 可运行的最小实例"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._pending_lazy_cards = []
    win._loading_session = False
    win._lazy_batch_timer_active = False
    win._is_widget_alive = lambda card: True
    win._rendered_card_count = 0
    win._max_rendered_cards = 100
    win._recycle_lru_batches = MagicMock()
    win._initial_scroll_to_bottom = False
    win._user_intentionally_away_from_bottom = False
    win._scroll_to_bottom = MagicMock()
    # _on_scroll_changed 依赖（滚动同步/定时器/历史加载）
    win._sync_node_preview_to_scroll = MagicMock()
    win._bottom_anchor_deadline = 0.0
    win._bottom_anchor_timer = MagicMock()
    win._virtual_scroll_timer = MagicMock()
    win._scroll_sync_timer = MagicMock()
    win._load_more_history_batches = MagicMock()
    win._history_load_threshold = 48
    scroll_bar = MagicMock()
    scroll_bar.maximum.return_value = 1000
    scroll_bar.value.return_value = 0  # 顶部
    area = MagicMock()
    area.verticalScrollBar.return_value = scroll_bar
    win.chat_scroll_area = area
    for key, value in overrides.items():
        setattr(win, key, value)
    return win


def _make_card(lazy_rendered: bool = False):
    card = MagicMock()
    card._lazy_rendered = lazy_rendered
    return card


@pytest.fixture(autouse=True)
def _reset_global_pages():
    """隔离模块级 _global_rendered_pages 计数器"""
    import app.main_widget as mw

    old = getattr(mw, "_global_rendered_pages", 0)
    mw._global_rendered_pages = 0
    yield
    mw._global_rendered_pages = old


# ─── 置底分支（_loading_session 期间批次渲染完成） ───────────────


def test_lazy_batch_no_scroll_bottom_when_user_away():
    """用户已滚离底部（上滚加载历史）→ 懒渲染批次不得置底"""
    win = _make_window(
        _loading_session=True,
        _initial_scroll_to_bottom=False,
        _user_intentionally_away_from_bottom=True,
    )
    win._pending_lazy_cards = [_make_card()]
    win._process_next_lazy_batch()

    scroll_bar = win.chat_scroll_area.verticalScrollBar()
    scroll_bar.setValue.assert_not_called()
    # 队列未清空 → 继续调度下一批，不触发 sticky 滚底
    win._scroll_to_bottom.assert_not_called()


def test_lazy_batch_scroll_bottom_when_initial_loading():
    """初始加载且用户未滚动 → 首次懒渲染批次强制置底（原有行为保留）"""
    win = _make_window(
        _loading_session=True,
        _initial_scroll_to_bottom=False,
        _user_intentionally_away_from_bottom=False,
    )
    win._pending_lazy_cards = [_make_card()]
    win._process_next_lazy_batch()

    scroll_bar = win.chat_scroll_area.verticalScrollBar()
    scroll_bar.setValue.assert_called_once_with(1000)
    assert win._initial_scroll_to_bottom is True


def test_lazy_batch_scroll_bottom_when_near_bottom():
    """加载中用户在底部附近且未主动滚离 → 跟随置底（原有行为保留）"""
    win = _make_window(
        _loading_session=True,
        _initial_scroll_to_bottom=True,
        _user_intentionally_away_from_bottom=False,
    )
    win.chat_scroll_area.verticalScrollBar().value.return_value = 990  # max-10
    win._pending_lazy_cards = [_make_card()]
    win._process_next_lazy_batch()

    scroll_bar = win.chat_scroll_area.verticalScrollBar()
    scroll_bar.setValue.assert_called_once_with(1000)


# ─── sticky 滚底分支（pending 清空后） ──────────────────────────


def test_no_sticky_scroll_bottom_when_user_away():
    """用户已滚离底部 → pending 清空后不得触发 sticky 滚底"""
    win = _make_window(
        _loading_session=True,
        _initial_scroll_to_bottom=True,
        _user_intentionally_away_from_bottom=True,
    )
    win._pending_lazy_cards = [_make_card()]
    win._process_next_lazy_batch()

    scroll_bar = win.chat_scroll_area.verticalScrollBar()
    scroll_bar.setValue.assert_not_called()
    win._scroll_to_bottom.assert_not_called()


def test_sticky_scroll_bottom_when_user_at_bottom():
    """用户未滚离底部 → pending 清空后 sticky 滚底（原有行为保留）"""
    win = _make_window(
        _loading_session=True,
        _initial_scroll_to_bottom=True,
        _user_intentionally_away_from_bottom=False,
    )
    win._pending_lazy_cards = [_make_card()]
    win._process_next_lazy_batch()

    win._scroll_to_bottom.assert_called_once_with(sticky_ms=900)


# ─── 入口：_on_scroll_changed 的 away 标志状态机 ─────────────────


def test_away_flag_set_when_scroll_up_then_cleared_at_bottom():
    """滚离底部 30px 以上置 away；滚回底部附近复位"""
    win = _make_window()
    scroll_bar = win.chat_scroll_area.verticalScrollBar()

    # 用户滚到顶部（历史加载区）
    scroll_bar.value.return_value = 10
    scroll_bar.maximum.return_value = 1000
    win._on_scroll_changed(10)
    assert win._user_intentionally_away_from_bottom is True

    # 用户滚回底部附近 → 复位
    scroll_bar.value.return_value = 990
    win._on_scroll_changed(990)
    assert win._user_intentionally_away_from_bottom is False
