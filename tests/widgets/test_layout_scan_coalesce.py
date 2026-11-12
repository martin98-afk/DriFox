# -*- coding: utf-8 -*-
"""[#viewport-scan] 滚动视口遍历收口回归测试。

改动点（app/main_widget.py）：

1. 新增 `_scan_chat_layout_once()`：单次遍历 chat_layout 同时产出卡口径
   （±200px 缓冲的 visible_ids / near_cards / preview_cards）与批口径
   （严格相交的 rng + 占位死区 in_view/near 标记），消除
   `_sync_visible_cards_on_scroll` 同拍内「卡遍历 + `_viewport_batch_range`
   批遍历」的双 O(N)。

2. `_sync_visible_cards_on_scroll` 改为消费扫描结果；快照存
   `self._last_layout_scan`，拍结束即置 None。

3. `_viewport_batch_range` 改薄包装：优先复用同拍快照，缺位（拍外 timer /
   回收链路径、`__new__` 测试桩）时自行遍历兜底，四个消费方语义不变。

测试策略：不实例化完整窗口；`__new__` 桩 + 真实 QScrollArea/QVBoxLayout
（同 test_scroll_hot_path.py 法），几何用 processEvents 后的真实布局。
"""

from unittest.mock import MagicMock

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from app.main_widget import OpenAIChatToolWindow


def _make_scan_window(scroll_value=0, viewport_h=200):
    """构造 `_scan_chat_layout_once` 可运行的最小实例。

    返回 (win, layout, container)：真实 QScrollArea + QVBoxLayout，
    show + processEvents 让 geometry 可读（布局传播是异步的）。
    """
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._last_visible_card_ids = set()
    win._last_layout_scan = None
    win._restore_queue = []
    win._log_render_quota = MagicMock()
    win._batch_placeholders = {}

    scroll_area = QScrollArea()
    container = QWidget()
    container.resize(400, 4000)  # 布局收敛的底气：真实尺寸（否则几何全零、滚动条 max=0）
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignTop)  # 固定高子项顶对齐（否则布局会把它们居中摆放）
    scroll_area.setWidget(container)
    scroll_area.resize(400, viewport_h)
    scroll_area.show()
    win.chat_scroll_area = scroll_area
    win.chat_layout = layout
    win._virtual_scroll_timer = MagicMock()
    return win, layout, container


def _add_card(layout, container, message_index=None, height=100):
    """往布局追加一张带 _message_index 的轻量卡（非 MessageCard：只进批口径）。"""
    w = QWidget(container)
    w._message_index = message_index
    w.setFixedHeight(height)
    layout.addWidget(w)
    return w


def _flush(container, scroll_area, scroll_value=0):
    """两段式收敛：先 processEvents 让布局更新滚动条 maximum，再滚动。"""
    QApplication.processEvents()
    scroll_area.verticalScrollBar().setValue(scroll_value)
    QApplication.processEvents()


# ─── 完成标准 1：同拍内 _viewport_batch_range 与扫描结果一致 ─────────────


def test_batch_range_matches_scan_result(qapp):
    """同一布局上：薄包装兜底遍历的 rng 必须与单次扫描的 rng 逐项一致。

    布局：假卡(idx=0) + 占位 spacer(idx=2) + 假卡(idx=5)，视口落在中间段，
    占位经 `_batch_placeholders` 映射进入批口径（等价真实回收场景）。
    """
    from app.widgets.message_card import MessageCard

    win, layout, container = _make_scan_window()
    card0 = _add_card(layout, container, message_index=0)
    spacer = QWidget(container)
    spacer.setFixedHeight(100)
    layout.addWidget(spacer)
    card5 = _add_card(layout, container, message_index=5)
    # 用真实 MessageCard 验证卡口径与批口径共存（其 _message_index 也能进 rng）
    real_card = MessageCard(role="assistant", parent=container)
    real_card.setFixedHeight(100)
    real_card._message_index = 9
    layout.addWidget(real_card)
    win._batch_placeholders = {2: spacer}
    _flush(container, win.chat_scroll_area)

    scan = win._scan_chat_layout_once()
    assert scan is not None
    # 兜底路径（快照缺位）：薄包装必须自行遍历且结果与扫描一致
    assert win._last_layout_scan is None
    assert win._viewport_batch_range() == scan["rng"], "兜底遍历与单次扫描的批区间必须一致"
    # 布局未变时重复扫描结果稳定（滚动拍口径的可重复性）
    scan2 = win._scan_chat_layout_once()
    assert scan2["rng"] == scan["rng"]
    assert scan2["visible_ids"] == scan["visible_ids"]


def test_snapshot_reused_within_tick_falls_back_outside(qapp):
    """同拍快照复用 + 拍外失效：薄包装两条路径的取舍必须正确。

    - 设好快照后即使布局被清空（兜底遍历必返回 None），仍返回快照 rng
      → 证明返回值来自快照而非兜底；
    - 快照置 None 后同一布局状态返回 None → 拍外不再读陈旧快照。
    """
    win, layout, container = _make_scan_window()
    card = _add_card(layout, container, message_index=3, height=100)
    _flush(container, win.chat_scroll_area)

    scan = win._scan_chat_layout_once()
    assert scan["rng"] == (3, 3), "前置校验：单卡布局批区间应为 (3, 3)"

    # 同拍：快照存在 → 布局清空后兜底必失败，返回值只能来自快照
    win._last_layout_scan = scan
    layout.removeWidget(card)
    card.setParent(None)
    _flush(container, win.chat_scroll_area)
    assert win._viewport_batch_range() == (3, 3), "同拍内必须复用快照 rng（兜底此时已不可用）"

    # 拍外：快照清空 → 回到兜底实时遍历（布局已空 → None，不读陈旧快照）
    win._last_layout_scan = None
    assert win._viewport_batch_range() is None, "拍外必须走兜底遍历，不得复用陈旧快照"


# ─── 滚动模拟：不报错 + 快照生命周期 + 可视集语义 ────────────────────────


def test_scroll_tick_runs_and_clears_snapshot(qapp):
    """滚动拍调用不报错；拍结束快照必须清空；可视集与扫描口径一致。"""
    from app.widgets.message_card import MessageCard

    win, layout, container = _make_scan_window(viewport_h=200)
    cards = []
    for idx in range(6):  # 6 张卡把内容撑到 ~900px，滚动条 max≈700，setValue 才不被钳到缓冲内
        card = MessageCard(role="assistant", parent=container)
        card.setFixedHeight(150)
        card._message_index = idx
        layout.addWidget(card)
        cards.append(card)
    win._sync_single_card_width = MagicMock()
    _flush(container, win.chat_scroll_area)

    win._sync_visible_cards_on_scroll()  # 不得抛异常

    assert win._last_layout_scan is None, "拍结束快照必须失效（拍外消费方走兜底）"
    # 首拍视口 [0,200) ±200px 缓冲 → [0,400)：含卡0/1/2（卡3 top=450 出缓冲）
    assert win._last_visible_card_ids == {id(c) for c in cards[:3]}, "可视集应整体替换为本拍扫描结果"
    # 滚到底部：首两卡出 ±200px 缓冲，必须离开可视集
    win.chat_scroll_area.verticalScrollBar().setValue(700)
    QApplication.processEvents()
    win._sync_visible_cards_on_scroll()
    after = win._last_visible_card_ids
    assert after != {id(c) for c in cards}, "滚出缓冲区的卡必须离开可视集"
    assert id(cards[0]) not in after and id(cards[1]) not in after, "首两卡必须出缓冲"
    assert after, "视口附近必须有卡（可视集不得误清空）"


def test_scroll_tick_syncs_only_new_entries(qapp):
    """[T23 语义保持] 上一拍已可见的卡不再重复 sync_width，滚出再滚入才重同步。"""
    from app.widgets.message_card import MessageCard

    win, layout, container = _make_scan_window(viewport_h=200)
    card = MessageCard(role="assistant", parent=container)
    card.setFixedHeight(150)
    card._message_index = 0
    layout.addWidget(card)
    calls = []
    win._sync_single_card_width = lambda c, force=True: calls.append(id(c))
    _flush(container, win.chat_scroll_area)

    win._sync_visible_cards_on_scroll()
    assert len(calls) == 1, "首拍：新进入视口必须同步宽度"
    win._sync_visible_cards_on_scroll()
    assert len(calls) == 1, "第二拍：仍在可视集内必须跳过"
    win._last_visible_card_ids = set()
    win._sync_visible_cards_on_scroll()
    assert len(calls) == 2, "离开视口（集合清空）后再次入拍必须重新同步"


# ─── 占位死区分派语义保持（源码断言锁定的分派关系）─────────────────────


def test_placeholder_dispatch_in_view_and_near(qapp):
    """占位分派：视口相交 → 同步重建；仅邻域 → 防抖 timer。"""
    win, layout, container = _make_scan_window(viewport_h=200)
    # 布局：占位 spacer(idx=23, 300px) 顶部 + 卡(idx=30) + filler×5（撑高让
    # spacer 能完全滚出视口；filler 无 idx 不进 rng）。
    # - 视口 [0,200)：spacer 相交 → rng=(23,23)，占位 23 ∈ rng → in_view
    # - 滚到 300：spacer（bottom=299）出视口 → rng=(30,30)，23 落 ±8 缓冲
    #   [22,38] → near
    spacer = QWidget(container)
    spacer.setFixedHeight(300)
    layout.addWidget(spacer)
    _add_card(layout, container, message_index=30, height=100)
    for _ in range(5):
        filler = QWidget(container)
        filler.setFixedHeight(300)
        layout.addWidget(filler)
    win._batch_placeholders = {23: spacer}
    win._incremental_visible_batch_count = 8
    win._virtual_scroll_buffer = 1
    restored = []
    win._restore_placeholders_in_viewport = lambda: restored.append(1) or 1
    _flush(container, win.chat_scroll_area)

    scan = win._scan_chat_layout_once()
    assert scan["rng"] == (23, 23), "初始视口只与占位相交"
    assert scan["ph_in_view"] and not scan["ph_near"]
    win._sync_visible_cards_on_scroll()
    assert len(restored) == 1, "in_view 占位必须走同步重建"
    win._virtual_scroll_timer.start.assert_not_called()

    # 邻域场景：滚到卡位（spacer 出视口），占位 23 落 ±8 缓冲 [22,38] → near
    win.chat_scroll_area.verticalScrollBar().setValue(300)
    QApplication.processEvents()
    scan2 = win._scan_chat_layout_once()
    assert scan2["rng"] == (30, 30), "spacer 已滚出视口，rng 应只含卡批次"
    assert not scan2["ph_in_view"] and scan2["ph_near"], "占位 23 应落在 [22, 38] 邻域"
    restored.clear()
    win._virtual_scroll_timer.start.reset_mock()
    win._sync_visible_cards_on_scroll()
    assert restored == [], "仅邻域时不得同步重建"
    win._virtual_scroll_timer.start.assert_called_once()


def test_degenerate_layout_dispatches_nothing(qapp):
    """全部零高（degenerate）→ rng=None → 占位分派双不动作（对齐原占位分支）。"""
    win, layout, container = _make_scan_window(viewport_h=200)
    card = _add_card(layout, container, message_index=3, height=0)
    # 显式零高（QScrollArea 内默认布局会给 spacer 非零高，必须钉死 0）→ degenerate
    spacer = QWidget(container)
    spacer.setFixedHeight(0)
    layout.addWidget(spacer)
    win._batch_placeholders = {5: spacer}
    win._incremental_visible_batch_count = 8
    win._virtual_scroll_buffer = 1
    restored = []
    win._restore_placeholders_in_viewport = lambda: restored.append(1) or 1

    scan = win._scan_chat_layout_once()
    assert scan["rng"] is None, "零高布局必须判 degenerate 返回 None"
    assert not scan["ph_in_view"] and not scan["ph_near"]
    win._sync_visible_cards_on_scroll()
    assert restored == []
    win._virtual_scroll_timer.start.assert_not_called()
