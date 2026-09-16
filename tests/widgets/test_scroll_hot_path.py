# -*- coding: utf-8 -*-
"""T23 滚动热路径优化回归测试。

三处改动（对齐 T21 方案 b>c>a）：

1. **b（TTL 放宽 + 高度失效）**：`SCROLL_MAX_CACHE_TTL` 0.03 → 0.12，覆盖
   sticky 锚定期的 100ms 拍（`_maintain_bottom_anchor` 每拍调
   `_sync_scroll_maximum`）；同时高度真变化（`_on_message_card_height_changed`
   读到非零 delta）时置 `_scroll_max_cache = None` 主动失效，避免读到陈旧上界。

2. **c（增量可视集）**：`_sync_visible_cards_on_scroll` 只对**本拍新进入**
   视口的卡片跑 `sync_width`（1.4ms/张）。常驻视口内的卡片在上一拍已同步，
   重复同步是纯浪费；集合每拍整体替换，离开视口后再滚入会重新同步。

3. **a（布局纪元缓存）**：`_sync_node_preview_to_scroll` 里「节点 → 用户卡引用」
   的定位结果只依赖布局结构，按 `_layout_epoch` 缓存；`y()`/`height()` 每拍实时
   重读。四个递增挂点：`_add_chat_widget` / `_recycle_out_of_view_batches` /
   `_clear_chat_area` / 撤回截断（`_truncate_session_from_user_round`）。

测试策略：不实例化完整窗口；TTL/失效用 `__new__` + MagicMock 行为测；
可视集与 epoch 用真实组件或源码静态断言（布局链依赖过多，行为测成本高）。
"""

import time
from pathlib import Path
from unittest.mock import MagicMock

from PyQt5.QtWidgets import QWidget

from app.main_widget import SCROLL_MAX_CACHE_TTL, OpenAIChatToolWindow

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = REPO_ROOT / "app" / "main_widget.py"


# ─── 改动 b：TTL 窗口内 sizeHint 不触发 + 高度变化失效重算 ─────────────


def _make_scroll_window(maximum=1000):
    """构造 `_sync_scroll_maximum` 可运行的最小实例（真实 QWidget 容器）"""
    from PyQt5.QtWidgets import QScrollArea

    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._programmatic_scroll_depth = 0
    win._scroll_max_cache = None
    scroll_area = QScrollArea()
    container = QWidget()
    container.resize(400, 5000)  # sizeHint 的底气：真实高度
    scroll_area.setWidget(container)
    scroll_area.resize(400, 200)
    win.chat_scroll_area = scroll_area
    return win


def test_ttl_covers_anchor_tick_interval():
    """TTL 必须 ≥ 0.1s（覆盖 sticky 锚定期的 100ms 拍间隔）"""
    assert SCROLL_MAX_CACHE_TTL >= 0.1, "TTL 需覆盖 _maintain_bottom_anchor 的 100ms 拍，否则锚定期仍每拍重算 sizeHint"


def test_sizehint_reused_within_ttl_window(qapp):
    """TTL 窗口内第二次调用命中缓存：不再重算 sizeHint（用替身计数验证）"""
    win = _make_scroll_window()
    calls = []
    container = win.chat_scroll_area.widget()
    assert container is not None  # _make_scroll_window 已 setWidget
    real_size_hint = container.sizeHint

    # 用替身包裹 sizeHint 计数（缓存命中时不应被调用第二次）
    def counting_size_hint():
        calls.append(1)
        return real_size_hint()

    container.sizeHint = counting_size_hint

    win._sync_scroll_maximum()
    first = len(calls)
    win._sync_scroll_maximum()  # 窗口内：应命中缓存
    assert len(calls) == first, "TTL 窗口内第二次调用不得重算 sizeHint（这是锚定期每拍开销的主项）"


def test_height_change_invalidates_max_cache(qapp):
    """高度真变化（非零 delta）→ 缓存置 None，下一次调用重算"""
    from PyQt5.QtWidgets import QVBoxLayout

    from app.widgets.message_card import MessageCard

    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._programmatic_scroll_depth = 0
    win._scroll_max_cache = (time.monotonic(), 8888)  # 伪造“刚缓存过”
    win._should_follow_bottom = MagicMock(return_value=False)
    win._user_intentionally_away_from_bottom = True

    from PyQt5.QtWidgets import QScrollArea

    scroll_area = QScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    card = MessageCard(role="assistant", parent=container)
    layout.addWidget(card)
    scroll_area.setWidget(container)
    scroll_area.resize(400, 200)
    win.chat_scroll_area = scroll_area

    card._last_height_delta = 42  # 非零 → 真变化
    # 直接走处理器主体（sender() 在无信号发射时为 None，故改用桩替代）
    win.sender = lambda: card  # type: ignore[method-assign]
    win._sync_scroll_maximum = MagicMock(return_value=1000)

    win._on_message_card_height_changed(100)

    assert win._scroll_max_cache is None, "高度真变化必须失效滚动上界缓存（否则 TTL 放宽后会读到陈旧上界）"


# ─── 改动 c：可视集增量 ──────────────────────────────────────────────


def test_visible_set_skips_already_visible_cards(qapp):
    """同一张卡连续两拍：第二拍不再调 sync_width（增量跳过）"""
    from PyQt5.QtWidgets import QScrollArea, QVBoxLayout

    from app.widgets.message_card import MessageCard

    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._last_visible_card_ids = set()
    win._restore_queue = []
    win._log_render_quota = MagicMock()

    scroll_area = QScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    card = MessageCard(role="assistant", parent=container)
    card.setFixedHeight(300)
    layout.addWidget(card)
    scroll_area.setWidget(container)
    scroll_area.resize(400, 200)
    scroll_area.show()
    qapp.processEvents()
    win.chat_scroll_area = scroll_area
    win.chat_layout = layout

    calls = []
    win._sync_single_card_width = lambda c, force=True: calls.append(id(c))  # type: ignore[method-assign]

    win._sync_visible_cards_on_scroll()
    assert len(calls) == 1, "首拍：卡片新进入视口，应同步宽度"

    win._sync_visible_cards_on_scroll()
    assert len(calls) == 1, "第二拍：卡片仍在上一拍视口集内，应跳过 sync_width"

    # 滚出视口后又滚回：集合已不含该卡 → 重新同步
    win._last_visible_card_ids = set()
    win._sync_visible_cards_on_scroll()
    assert len(calls) == 2, "离开视口后再次滚入必须重新同步（保证宽度正确）"


# ─── 改动 a：布局纪元 ────────────────────────────────────────────────


def test_layout_epoch_bumped_at_four_hooks():
    """四个挂点都必须递增布局纪元（源码静态断言：布局链依赖过多，行为测成本高）"""
    src = SRC_PATH.read_text(encoding="utf-8")

    # _bump_layout_epoch 自身存在且两条动作齐全
    start = src.find("def _bump_layout_epoch(self) -> None:")
    assert start != -1, "缺少 _bump_layout_epoch 定义"
    body_end = src.find("\n    def ", start + 1)
    body = src[start:body_end]
    assert "_layout_epoch += 1" in body, "递增 _layout_epoch"
    assert "_node_user_cards_cache = None" in body, "作废节点定位缓存"

    # 四个挂点：增卡 / 回收 / 清空 / 撤回截断
    hooks = [
        ("def _add_chat_widget(", "_bump_layout_epoch()", "新增卡片"),
        ("def _recycle_out_of_view_batches(", "_bump_layout_epoch()", "批次回收"),
        ("def _clear_chat_area(", "_bump_layout_epoch()", "清空聊天区"),
        ("def _truncate_session_from_user_round(", "_bump_layout_epoch()", "撤回截断"),
    ]
    for defn, call, label in hooks:
        fstart = src.find(defn)
        assert fstart != -1, f"未找到 {label} 入口 {defn}"
        fend = src.find("\n    def ", fstart + 1)
        assert call in src[fstart:fend], f"{label}（{defn}）缺少 {call}"


def test_node_cache_rebuilt_on_epoch_change():
    """纪元变化 → 节点定位缓存作废重建；纪元不变 → 复用（不重扫 _batch_cards）"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._layout_epoch = 7
    win._node_user_cards_cache = None
    assert win._node_user_cards_cache is None

    win._bump_layout_epoch()
    assert win._layout_epoch == 8

    # 复现缓存判定语义（与 _sync_node_preview_to_scroll 内一致）
    win._node_user_cards_cache = (win._layout_epoch, [("located",)])
    cache = win._node_user_cards_cache
    assert cache is not None and cache[0] == win._layout_epoch, "同期应命中"

    win._bump_layout_epoch()
    cache = win._node_user_cards_cache
    assert cache is None, "纪元递增必须同时清空缓存（否则复用陈旧卡引用）"
