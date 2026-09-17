# -*- coding: utf-8 -*-
"""虚拟滚动「回收 → 重建 → 高度塌陷/滚动漂移」复现脚本（T42）。

    python tests/debug/vscroll_recycle_rebuild_repro.py

## 复现的现象

消息卡片滚不到底：一滚到底就自动弹回上方，随后触发视窗外清理，再被
重建/回收推进新一轮，形成「永远到不了底」的恶性循环。

## 实测输出（未修复，batch=30 / H=300 / viewport=400）

    滚到底       value/max = 8602/8602   total = 9000
    回收后       value/max = 8602/8602   total = 9000   （等高占位生效，✔）
    上滚到批次 15 value/max = 4500/8602   total = 9000
    第 1.5 步重建 value/max = 5423/8602   total = 6335
                 HEIGHT: 9000 -> 6335  (delta -2665)
                 SCROLL: 4500 -> 5423  (drift +923)

判据：修复后 `total` 不得塌、`value` 漂移必须 ≤ AT_BOTTOM_TOLERANCE(24px)。

## 环的四个节点

1. 滚到底 → `_virtual_scroll_timer`（500ms）→ `_recycle_out_of_view_batches`
2. 回收视口外批次，为每批装等高占位（`_install_batch_placeholder`）
3. 下一轮「第 1.5 步」对占位批次**原位重建**（`_take_batch_placeholder`）
4. 重建卡片从最小高度（40px）起步，占位高度 H 被丢弃 → 容器塌 Σ(H-40)
   → `_restore_anchor_after_placeholder` 的锚点补偿算出巨大正 `_delta`
   → `setValue(value + _delta)` 把视口**往下拽**（读数上表现为跳变）
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.main_widget import AT_BOTTOM_TOLERANCE, OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

BATCH = 30
CARD_H = 300
VIEWPORT_H = 400


def build_window():
    """装出能跑回收 + 重建全链的最小窗口（__new__ + 真实几何）"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    for key, value in {
        "_is_destroyed": False,
        "_programmatic_scroll_depth": 0,
        "_scroll_max_cache": None,
        "_is_virtual_recycling": False,
        "_user_intentionally_away_from_bottom": False,
        "_loading_session": False,
        "_bottom_anchor_deadline": 0.0,
        "_pending_scroll_to_bottom": False,
        "_is_loading_history_batches": False,
        "_last_visible_card_ids": set(),
        "_restore_queue": [],
        "_current_assistant_card": None,
        "_layout_epoch": 0,
        "_node_user_cards_cache": None,
        "_rendered_card_count": 0,
        "_recycle_lru_call_count": 0,
        "_incremental_visible_batch_count": 8,
        "_virtual_scroll_buffer": 1,
        "_max_rendered_cards": 12,
        "_min_rendered_cards": 6,
        "_unloaded_pids": [],
        "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False,
        "_history_load_threshold": 48,
        "_initial_scroll_to_bottom": True,
        "_suppress_scroll_sync_count": 0,
        "_visible_batch_start": 0,
        "_visible_batch_end": BATCH,
    }.items():
        setattr(win, key, value)

    # 与本案无关的外部依赖全部隔离
    win._log_render_quota = MagicMock()
    win._maybe_strong_recycle = MagicMock()
    win._recycle_lru_batches = MagicMock()
    win._sync_global_rendered_pages = MagicMock()
    win._process_next_lazy_batch = MagicMock()
    win._try_detach_card_viewer = lambda card: False
    win._is_widget_alive = lambda w: w is not None
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "s1"
    win._get_user_round_index_for_batch_index = MagicMock(return_value=0)
    win._update_card_diff_stats = MagicMock()
    win._restore_meta_from_batch = MagicMock()
    win._create_message_widget = MagicMock(return_value=None)
    win._sync_single_card_width = MagicMock()

    scroll_area = QScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    scroll_area.setWidget(container)
    scroll_area.resize(500, VIEWPORT_H)
    win.chat_scroll_area = scroll_area
    win.chat_container = container
    win.chat_layout = layout

    batch_cards, message_batch = [], []
    for idx in range(BATCH):
        card = MessageCard(role="assistant", parent=container)
        card.setFixedHeight(CARD_H)
        card._message_index = idx
        card._lazy_rendered = True
        layout.addWidget(card)
        batch_cards.append([card])
        message_batch.append([{"role": "assistant", "content": f"batch {idx}"}])
    win._batch_cards = batch_cards
    win._message_batch = message_batch
    win._batch_placeholders = {}
    win._user_prefix_cache = [0] * (BATCH + 1)

    # 重建桩：新建卡片从最小高度起步（真实 MessageCard 行为）
    def _fake_append_assistant(**kw):
        card = MessageCard(role="assistant", parent=container)
        card._message_index = kw.get("insert_index") or 0
        card._lazy_rendered = True
        win._add_chat_widget(card, insert_index=kw.get("insert_index"))
        return card

    win._append_assistant_message = _fake_append_assistant
    win._add_chat_widget = lambda w, insert_index=None: layout.insertWidget(
        layout.count() if insert_index is None else insert_index, w
    )

    scroll_area.show()
    for _ in range(3):
        app.processEvents()
    container.adjustSize()
    for _ in range(3):
        app.processEvents()
    return win


def main():
    win = build_window()
    sb = win.chat_scroll_area.verticalScrollBar()
    total = lambda: win.chat_container.sizeHint().height()  # noqa: E731

    sb.setValue(sb.maximum())
    app.processEvents()
    print(f"滚到底        value/max = {sb.value()}/{sb.maximum()}  total = {total()}")

    win._recycle_out_of_view_batches()
    app.processEvents()
    print(f"回收后        value/max = {sb.value()}/{sb.maximum()}  total = {total()}")
    print(f"              回收批次 = {[i for i, b in enumerate(win._batch_cards) if b is None]}")
    print(f"              占位批次 = {sorted(win._batch_placeholders.keys())}")

    sb.setValue(15 * CARD_H)
    app.processEvents()
    before_total, before_value = total(), sb.value()
    print(f"上滚到批次 15  value/max = {sb.value()}/{sb.maximum()}  total = {total()}")
    print(f"              视口批次 = {win._viewport_batch_range()}")

    win._recycle_out_of_view_batches()
    app.processEvents()
    app.processEvents()
    print(f"第 1.5 步重建  value/max = {sb.value()}/{sb.maximum()}  total = {total()}")

    height_delta = total() - before_total
    scroll_drift = sb.value() - before_value
    print()
    print(f"HEIGHT: {before_total} -> {total()}  (delta {height_delta})")
    print(f"SCROLL: {before_value} -> {sb.value()}  (drift {scroll_drift:+d})")
    print(f"占位批次 now = {sorted(win._batch_placeholders.keys())}")

    ok = abs(height_delta) <= 1 and abs(scroll_drift) <= AT_BOTTOM_TOLERANCE
    print()
    print("PASS" if ok else "FAIL (复现成功：高度塌陷 + 滚动漂移)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
