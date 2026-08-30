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

from app.main_widget import AT_BOTTOM_TOLERANCE, OpenAIChatToolWindow  # noqa: E402


def _make_window(**overrides):
    """构造 _process_next_lazy_batch 可运行的最小实例"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    # ⚠️ 必须显式给值：PyQt 对象未经 __init__ 时 getattr(self, "_is_destroyed", False)
    # 会抛 RuntimeError（super-class __init__ was never called），而带默认值的
    # getattr 兜不住这个异常。凡走 _is_destroyed 守卫的方法都要靠它。
    win._is_destroyed = False
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


# ─── 统一守卫：AT_BOTTOM_TOLERANCE / _should_follow_bottom ───────
# 背景：同一语义曾散落 5 套阈值（20/30/50/80），且 4 处滚底入口绕过 away 守卫，
# 导致「流式输出时无法自由阅读」。以下用例锁定收敛后的单点裁决语义。


def test_is_view_at_bottom_uses_single_tolerance():
    """贴底判定只认 AT_BOTTOM_TOLERANCE，边界值不得有两套解释"""
    win = _make_window()
    bar = win.chat_scroll_area.verticalScrollBar()
    bar.maximum.return_value = 1000

    bar.value.return_value = 1000 - AT_BOTTOM_TOLERANCE  # 恰好在阈值内
    assert win._is_view_at_bottom() is True
    bar.value.return_value = 1000 - AT_BOTTOM_TOLERANCE - 1  # 越过阈值 1px
    assert win._is_view_at_bottom() is False


def test_is_view_at_bottom_true_when_content_shorter_than_viewport():
    """内容不足一屏（maximum==0，无处可滚）必须判为贴底"""
    win = _make_window()
    bar = win.chat_scroll_area.verticalScrollBar()
    bar.maximum.return_value = 0
    bar.value.return_value = 0
    assert win._is_view_at_bottom() is True


def test_should_follow_bottom_falls_back_to_actual_position():
    """away 标志与实际位置冲突时以实际位置为准，避免标志卡死后永不跟随"""
    win = _make_window(_user_intentionally_away_from_bottom=True)
    bar = win.chat_scroll_area.verticalScrollBar()
    bar.maximum.return_value = 1000

    bar.value.return_value = 1000
    assert win._should_follow_bottom() is True
    bar.value.return_value = 0
    assert win._should_follow_bottom() is False


def test_reset_bottom_follow_clears_away():
    """用户点击发送 → 恢复跟随（否则「发了消息没反应」）"""
    win = _make_window(_user_intentionally_away_from_bottom=True)
    win._reset_bottom_follow()
    assert win._user_intentionally_away_from_bottom is False


def test_away_cleared_when_content_shorter_than_viewport():
    """回归：旧实现 `elif maximum > 0` 在 maximum==0 时不复位 → away 永久卡 True"""
    win = _make_window(_user_intentionally_away_from_bottom=True)
    bar = win.chat_scroll_area.verticalScrollBar()
    bar.maximum.return_value = 0
    bar.value.return_value = 0
    win._on_scroll_changed(0)
    assert win._user_intentionally_away_from_bottom is False


def test_ensure_at_bottom_skipped_when_user_away():
    """回归：流式结束兜底重试链曾靠「2s 宽限期」显式忽略 away，强行拽回视口"""
    win = _make_window(_user_intentionally_away_from_bottom=True)
    bar = win.chat_scroll_area.verticalScrollBar()
    bar.maximum.return_value = 1000
    bar.value.return_value = 0
    win._ensure_at_bottom(retries=0)
    bar.setValue.assert_not_called()


def test_scroll_to_bottom_if_following_guarded():
    """延迟兜底滚底（500ms/1000ms）同样要过守卫"""
    win = _make_window(_user_intentionally_away_from_bottom=True)
    bar = win.chat_scroll_area.verticalScrollBar()
    bar.maximum.return_value = 1000
    bar.value.return_value = 0
    win._scroll_to_bottom_if_following()
    win._scroll_to_bottom.assert_not_called()


# ─── 批次卸载等高占位 ───────────────────────────────────────────
# 背景：B4 回收把卡片移出布局后容器高度瞬间塌陷，随后卡片高度再异步上报，
# 与 _ensure_at_bottom 的 8×300ms 重试链重叠 → 长对话滚动期抖动。
# 占位让总高度在回收瞬间保持不变。


def _make_placeholder_window():
    """造一个带真实 chat_layout / chat_container 的最小实例"""
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    win = _make_window()
    container = QWidget()
    layout = QVBoxLayout(container)
    win.chat_container = container
    win.chat_layout = layout
    win._batch_placeholders = {}
    return win, container, layout


def test_install_batch_placeholder_keeps_height(qapp):
    """占位等高且落在原位；摘掉卡片后布局里仍留着它 → 高度不塌陷"""
    from PyQt5.QtWidgets import QWidget

    win, container, layout = _make_placeholder_window()
    card = QWidget(container)
    card.setFixedHeight(120)
    layout.addWidget(card)
    idx = layout.indexOf(card)

    assert win._install_batch_placeholder(3, 120, idx) is True
    ph = win._batch_placeholders[3]
    assert ph.minimumHeight() == 120  # 等高
    assert layout.indexOf(ph) == idx  # 原位

    layout.removeWidget(card)
    card.setParent(None)
    assert layout.count() == 1  # 占位还在 → 容器总高度没塌


def test_take_batch_placeholder_returns_index_and_removes(qapp):
    """取回占位要还回原索引 —— 否则重建的卡片会被追加到末尾，顺序错乱"""
    from PyQt5.QtWidgets import QWidget

    win, container, layout = _make_placeholder_window()
    card = QWidget(container)
    layout.addWidget(card)
    idx = layout.indexOf(card)

    win._install_batch_placeholder(7, 80, idx)
    assert win._take_batch_placeholder(7) == idx
    assert win._take_batch_placeholder(7) is None  # 幂等：取过就没了


def test_install_batch_placeholder_rejects_bad_args(qapp):
    """高度 0 / 索引非法时不安装，调用方必须回退到旧的滚动值补偿"""
    win, _container, _layout = _make_placeholder_window()
    assert win._install_batch_placeholder(0, 0, 0) is False
    assert win._install_batch_placeholder(0, 100, -1) is False
    assert win._batch_placeholders == {}


def test_clear_batch_placeholders(qapp):
    """清空会话 / 重建布局时必须能一次性摘干净，防止索引错位"""
    from PyQt5.QtWidgets import QWidget

    win, container, layout = _make_placeholder_window()
    card = QWidget(container)
    layout.addWidget(card)
    win._install_batch_placeholder(1, 60, layout.indexOf(card))
    win._install_batch_placeholder(2, 60, layout.indexOf(card))
    assert len(win._batch_placeholders) == 2
    win._clear_batch_placeholders(remove_from_layout=True)
    assert win._batch_placeholders == {}
    assert layout.count() == 1  # 只剩原来那张 card，占位已摘掉


# ─── 主题切换刷新 ───────────────────────────────────────────────


def test_theme_changed_event_refreshes_button(qapp):
    """回归：按钮必须靠 EV_THEME_CHANGED 刷新，光注册 refresh_target 收不到

    🐛 主程序主题切换走 `main_widget._execute_batched_theme_refresh`，它只做
    Colors.refresh() + theme_manager.on_theme_changed() + per-window
    `_apply_runtime_ui_settings()`，**从不调用 theme_manager.dispatch_refresh()**
    → 注册进 `_refresh_targets` 的 widget 一个都收不到 refresh_theme()。
    （main_widget.py 的批量刷新注释里也点明了这一点。）
    """
    from app.core.ui_event_bus import EV_THEME_CHANGED, UIEventBus
    from app.utils.design_tokens import Colors
    from app.utils.theme_manager import theme_manager
    from app.widgets.scroll_to_bottom_button import ScrollToBottomButton

    _win, container, _layout = _make_placeholder_window()
    btn = ScrollToBottomButton(container, anchor=None, on_click=None)
    before = btn.styleSheet()

    # 订阅必须真实存在（模块级只订阅一次）
    assert UIEventBus.get_instance().subscriptions().get(EV_THEME_CHANGED, 0) >= 1

    saved = (Colors.CARD_BG, Colors.BORDER, Colors.HOVER_BG)
    try:
        Colors.CARD_BG = "rgba(250, 250, 250, {alpha})"
        Colors.BORDER = "#dddddd"
        Colors.HOVER_BG = "rgba(0, 0, 0, 0.12)"
        # 模拟主程序主题切换：Colors 刷新 + on_theme_changed（内部 publish 事件）
        theme_manager.on_theme_changed()
        assert btn.styleSheet() != before
        assert "#dddddd" in btn.styleSheet()
    finally:
        Colors.CARD_BG, Colors.BORDER, Colors.HOVER_BG = saved
        theme_manager.on_theme_changed()


def test_apply_style_skips_when_signature_unchanged(qapp):
    """签名未变时不重复渲染 SVG（按钮每次浮出都会调 _apply_style）"""
    from app.widgets.scroll_to_bottom_button import ScrollToBottomButton

    _win, container, _layout = _make_placeholder_window()
    btn = ScrollToBottomButton(container, anchor=None, on_click=None)
    sig = btn._style_sig
    btn._apply_style()
    assert btn._style_sig == sig  # 没变 → 没重刷
    btn._apply_style(force=True)
    assert btn._style_sig == sig  # 强制重刷后签名依然一致
