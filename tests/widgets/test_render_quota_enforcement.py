# -*- coding: utf-8 -*-
"""回归测试：加载长会话时渲染配额不得被架空（2026-09-14 内存暴涨修复）。

三个缺陷（真机日志证据：``rendered=2`` 而 ``stats acquire=26 hit=0``，
单 renderer 进程 512MB，`_recycle_lru_batches` 一次都没跑）：

1. **计数漏记**：`_recycle_out_of_view_batches` 第一步的补渲染只累加日志用的
   `lazy_render_count`，从不写回 `_rendered_card_count` → 配额被永久低估 →
   淘汰链首行 `if _rendered_card_count <= quota: return` 恒成立。
2. **保留范围用了加载窗口**：`_visible_batch_start/_end` 加载完恒等于
   ``[len-12, len]``，小表（≤20 批）下保留区间覆盖整张表 → 回收范围恒为空，
   保护判定也让候选集恒为空。
3. **候选耗尽无降级**：即使前两项修好，全部批次被软保护（可视区 ±1）时
   依旧无候选可淘汰，计数无法回落。

测试策略：`__new__` 构造最小实例（与 test_main_widget_scroll_anchor.py 同法），
不实例化完整窗口、不创建 QWebEngineView。第 1 项用**源码断言**锁定归属关系，
第 2/3 项用真实几何/候选逻辑做行为断言。
"""

import sys
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QRect

# 注意：不得在此插入 "app" 到 sys.path（见 test_main_widget_scroll_anchor.py 注释）

from app.main_widget import OpenAIChatToolWindow  # noqa: E402

_SRC_PATH = "app/main_widget.py"


def _src() -> str:
    with open(_SRC_PATH, encoding="utf-8") as f:
        return f.read()


def _make_window(**overrides):
    """构造 _recycle_* 系列可运行的最小实例。"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._is_virtual_recycling = False
    win._recycle_lru_call_count = 0
    win._rendered_card_count = 0
    win._max_rendered_cards = 12
    win._visible_batch_start = 0
    win._visible_batch_end = 0
    win._batch_cards = []
    win._batch_placeholders = {}
    win._current_assistant_card = None
    win._is_widget_alive = lambda w: True
    win._log_render_quota = MagicMock()
    win._maybe_strong_recycle = MagicMock()
    win._unload_batch = MagicMock(return_value=0)
    win._effective_max_rendered_cards = lambda: 12
    win._render_floor = lambda: 3
    win._last_quota_log_at = 0.0
    win.chat_layout = MagicMock()
    win.chat_layout.count.return_value = 0
    win.chat_layout.itemAt.return_value = None
    scroll_bar = MagicMock()
    scroll_bar.maximum.return_value = 1000
    scroll_bar.value.return_value = 0
    area = MagicMock()
    area.verticalScrollBar.return_value = scroll_bar
    area.viewport.return_value.height.return_value = 600
    win.chat_scroll_area = area
    for key, value in overrides.items():
        setattr(win, key, value)
    return win


def _card(lazy_rendered: bool = True, idx: int = 0):
    card = MagicMock()
    card._lazy_rendered = lazy_rendered
    card._message_index = idx
    card._is_welcome = False
    return card


@pytest.fixture(autouse=True)
def _reset_global_pages():
    """隔离模块级 _global_rendered_pages 计数器"""
    import app.main_widget as mw

    old = getattr(mw, "_global_rendered_pages", 0)
    mw._global_rendered_pages = 0
    yield
    mw._global_rendered_pages = old


# ─── 缺陷 1：补渲染必须计入配额 ───────────────────────────────


def test_recycle_path_render_is_counted_toward_quota():
    """`_recycle_out_of_view_batches` 补渲染出的 viewer 必须写回计数。

    源码级断言：锁定「渲染后按 _lazy_rendered 归属 + 调 _sync_global_rendered_pages」
    这一对关系。漏掉任一步，配额都会被永久低估（真机表现为淘汰链一次不跑）。
    """
    text = _src()
    start = text.index("def _recycle_out_of_view_batches(")
    end = text.index("def _register_unloaded_pid(", start)
    body = text[start:end]
    assert "rendered_delta" in body, "补渲染必须累计本批新增渲染数"
    assert "_sync_global_rendered_pages(self._rendered_card_count + rendered_delta)" in body, (
        "补渲染后必须把计数写回 _rendered_card_count（否则配额失效）"
    )
    # 归属口径：以渲染后的 _lazy_rendered 为准（可见性门控会让 ensure_rendered 空转）
    assert 'getattr(card, "_lazy_rendered", False)' in body


def test_recount_helper_uses_batch_cards_and_lazy_flag():
    """`_recount_rendered_cards` 是漂移兜底：按实况重算并同步全局计数。"""
    text = _src()
    start = text.index("def _recount_rendered_cards(")
    body = text[start : text.index("def _recycle_lru_batches(", start)]
    assert 'getattr(card, "_lazy_rendered", False)' in body
    assert "self._sync_global_rendered_pages(actual)" in body

    win = _make_window()
    win._batch_cards = [[_card(True), _card(True)], None, [_card(False)]]
    win._rendered_card_count = 0
    win._recount_rendered_cards()
    assert win._rendered_card_count == 2, "只计已渲染卡片，未渲染卡不计"


# ─── 缺陷 2：保留范围必须基于真实视口 ─────────────────────────


def _patch_layout_geometry(win, entries):
    """entries: [(index, top, height)] → 让 chat_layout 返回带几何的卡片。"""
    widgets = []
    for idx, top, height in entries:
        w = MagicMock()
        w.geometry.return_value = QRect(0, top, 800, height)
        w._message_index = idx
        widgets.append(w)
    win.chat_layout.count.return_value = len(widgets)
    win.chat_layout.itemAt.side_effect = lambda i: _item(widgets[i]) if i < len(widgets) else None


def _item(widget):
    it = MagicMock()
    it.widget.return_value = widget
    return it


def test_viewport_range_uses_real_geometry_not_loaded_window():
    """真实视口几何：视口内的批次入选，视口外的（即使已加载）不入选。

    这正是修复点 —— 加载窗口 ``[len-12, len]`` 会把视口外的批次也算进保留范围。
    """
    win = _make_window()
    win._visible_batch_start = 36
    win._visible_batch_end = 48  # 加载窗口 = 最后 12 批
    scroll_bar = win.chat_scroll_area.verticalScrollBar()
    scroll_bar.value.return_value = 1000  # 视口顶部
    # 视口 = [1000, 1600)（value=1000, height=600）
    _patch_layout_geometry(
        win,
        [
            (10, 0, 500),  # [0,499] 视口上方：不入选
            (11, 500, 600),  # [500,1099] 与视口相交：入选
            (12, 1100, 400),  # [1100,1499] 与视口相交：入选
            (13, 1700, 400),  # [1700,2099] 视口下方：不入选
        ],
    )
    rng = win._viewport_batch_range()
    assert rng == (11, 12), f"应只返回真实可见批次，实际 {rng}"


def test_viewport_range_returns_none_when_layout_degenerate():
    """布局未就位（全部零高）→ 返回 None，由调用方回退加载窗口。"""
    win = _make_window()
    _patch_layout_geometry(win, [(0, 0, 0), (1, 0, 0)])
    assert win._viewport_batch_range() is None


def test_viewport_range_falls_back_to_loaded_window():
    """无几何时 `_resolve_viewport_range` 回退加载窗口（兜底不为空）。"""
    win = _make_window()
    win._visible_batch_start = 5
    win._visible_batch_end = 17
    win._viewport_batch_range = lambda: None
    assert win._resolve_viewport_range() == (5, 16)


def test_recycle_keeps_only_viewport_neighborhood():
    """回收范围以真实视口为基准：远离视口的批次必须被卸载。

    修复前：`active_start` 恒为 0、`active_end ≥ len` → 回收范围为空，
    长会话里所有批次同时持有 viewer。
    """
    win = _make_window()
    win._batch_cards = [[_card()] for _ in range(20)]
    win._visible_batch_start = 8
    win._visible_batch_end = 20
    win._viewport_batch_range = lambda: (9, 10)
    win._incremental_visible_batch_count = 8
    win._virtual_scroll_buffer = 1
    win._current_assistant_card = None

    win._recycle_out_of_view_batches()

    unloaded = [i for i, c in enumerate(win._batch_cards) if c is None]
    # buffer = 8 → active = [1, 18]；回收 0 与 19
    assert 0 in unloaded, "远在视口上方的批次必须回收"
    assert 19 in unloaded, "远在视口下方的批次必须回收"
    assert 9 not in unloaded and 10 not in unloaded, "视口内批次不得回收"


# ─── 缺陷 3：候选耗尽时降级淘汰 ─────────────────────────────


def test_streaming_and_welcome_batches_never_eligible():
    """降级候选也排除流式卡与欢迎卡（绝对不淘汰判据）。"""
    win = _make_window()
    win._batch_cards = [[_card()], [_card()], [_card()]]
    win._current_assistant_card = win._batch_cards[0][0]
    win._batch_cards[1][0]._is_welcome = True

    assert win._batch_is_streaming_or_welcome(0) is True
    assert win._batch_is_streaming_or_welcome(1) is True
    assert win._batch_is_streaming_or_welcome(2) is False


def test_lru_recycle_degrades_when_all_batches_protected():
    """全部批次落在可视区 ±1 内时，仍要按距离降级淘汰到配额内。

    修复前：candidates 为空 → 循环不执行 → 计数永久停在超配额状态。
    """
    win = _make_window()
    win._batch_cards = [[_card()] for _ in range(6)]
    win._rendered_card_count = 6
    win._effective_max_rendered_cards = lambda: 3
    win._viewport_batch_range = lambda: (2, 3)  # 全部 6 批都在 ±1 保护范围内
    unloaded = []

    def _fake_unload(idx):
        win._batch_cards[idx] = None
        unloaded.append(idx)
        win._rendered_card_count -= 1
        return 0

    win._unload_batch = _fake_unload
    win._recycle_lru_batches()

    # 视口 = (2,3) → 保护区间 [1,4]；其中 0/5 距离 1（可淘汰），1/4 距离 0（视口邻域，守住）
    assert unloaded == [0, 5], f"应按距离最远优先淘汰，实际 {unloaded}"
    assert win._rendered_card_count == 4, "视口邻域守住后计数停在该处，不会强压到配额"
    assert win._batch_cards[1] is not None and win._batch_cards[4] is not None


def test_lru_recycle_never_unloads_viewport_batches():
    """降级淘汰不得碰视口内批次（distance == 0 即刻停止）。"""
    win = _make_window()
    win._batch_cards = [[_card()] for _ in range(3)]
    win._rendered_card_count = 3
    win._effective_max_rendered_cards = lambda: 1
    win._viewport_batch_range = lambda: (1, 1)
    unloaded = []

    def _fake_unload(idx):
        win._batch_cards[idx] = None
        unloaded.append(idx)
        win._rendered_card_count -= 1
        return 0

    win._unload_batch = _fake_unload
    win._recycle_lru_batches()

    # 三批距离均为 0（含视口内）→ 降级循环首轮即 break，一个都不卸
    assert unloaded == [], "视口邻域内（distance==0）不得淘汰任何批次"
    assert win._rendered_card_count == 3, "宁可持续超配额，也不卸用户附近的内容"


def test_lru_recycle_noop_within_quota():
    """配额内不动任何批次（原有行为保留）。"""
    win = _make_window()
    win._batch_cards = [[_card()] for _ in range(3)]
    win._rendered_card_count = 3
    win._effective_max_rendered_cards = lambda: 12
    win._unload_batch = MagicMock()

    win._recycle_lru_batches()

    win._unload_batch.assert_not_called()
