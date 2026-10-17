# -*- coding: utf-8 -*-
"""端到端验证（真实主路径）：跳到「未加载」的历史批次后，加载完不得跑回底部。

跑法（项目根）：
    python tests/debug/verify_jump_unloaded_batch_lock.py

这条路径才是真机主路径：长会话里 `_visible_batch_start` 只覆盖最近 12 批，
点靠前的历史问题 → 目标批次 < `_visible_batch_start` → 走
`_scroll_to_target_node_index` → `_load_history_to_index` 分批 prepend →
`_scroll_to_pending_target` 落地。

历史上这条路径**从不经过** `_locate_and_scroll_to_batch`，因此从未上锁，
落点后被懒渲染排空/高度补偿拽走。

判据：prepend 完成并落地后，懒渲染排空造成的位移 ≤ AT_BOTTOM_TOLERANCE。
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
os.environ["DRIFOX_QT_RENDERER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.main_widget import AT_BOTTOM_TOLERANCE, OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

BATCH = 60           # 长会话
LOADED_TAIL = 12     # 只加载了最后 12 批
TARGET_NODE = 3      # 跳到靠前的历史问题（未加载区）
CARD_H = 300
GAP = 8
VIEW_H = 600


def pump(n=12):
    for _ in range(n):
        app.processEvents()


def build():
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    for k, v in {
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
        "_unloaded_pids": [],
        "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False,
        "_history_load_threshold": 48,
        "_initial_scroll_to_bottom": False,
        "_suppress_scroll_sync_count": 0,
        "_visible_batch_end": BATCH,
        "_visible_batch_start": BATCH - LOADED_TAIL,
        "_batch_placeholder_overhead": {},
        "_jump_scroll_lock_until": 0.0,
        "_jump_target_card": None,
        "_jump_settle_left": 0,
        "_jump_settle_last_value": None,
        "_ensure_ab_active": False,
        "_ensure_ab_active_until": 0.0,
        "_bottom_snap_queued": False,
        "_last_quota_log_at": 0.0,
        "_is_streaming": False,
        "_pending_scroll_to_index": None,
        "_pending_scroll_to_batch": None,
        "_pending_scroll_to_update": None,
        "session_manager": MagicMock(),
    }.items():
        setattr(win, k, v)

    win._scroll_bottom_timer = MagicMock()
    win._bottom_anchor_timer = MagicMock()
    win._virtual_scroll_timer = MagicMock()
    win._scroll_sync_timer = MagicMock()
    win._log_render_quota = MagicMock()
    win._maybe_strong_recycle = MagicMock()
    win._recycle_lru_batches = MagicMock()
    win._try_detach_card_viewer = lambda card: False
    win._is_widget_alive = lambda w: w is not None
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "s1"
    win._get_user_round_index_for_batch_index = MagicMock(return_value=0)
    win._update_card_diff_stats = MagicMock()
    win._restore_meta_from_batch = MagicMock()
    win._create_message_widget = MagicMock(return_value=None)
    win._sync_single_card_width = MagicMock()
    win._sync_node_preview_to_scroll = MagicMock()
    win._update_node_preview = MagicMock()
    win._sync_node_preview_to_last = MagicMock()

    area = QScrollArea()
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(GAP)
    area.setWidget(container)
    area.resize(520, VIEW_H)
    win.chat_scroll_area = area
    win.chat_container = container
    win.chat_layout = lay

    # 只渲染已加载的尾部批次（模拟长会话首屏）
    cards = [None] * BATCH
    batch_cards = [None] * BATCH
    message_batch = []
    for i in range(BATCH):
        message_batch.append([{"role": "user", "content": f"q{i}"}])
    for i in range(BATCH - LOADED_TAIL, BATCH):
        c = MessageCard(role="user", parent=container)
        c._message_index = i
        c.setFixedHeight(CARD_H)
        c._lazy_rendered = True
        lay.addWidget(c)
        cards[i] = c
        batch_cards[i] = [c]
    win._batch_cards = batch_cards
    win._message_batch = message_batch
    win._batch_placeholders = {}
    win._user_prefix_cache = [0] * (BATCH + 1)
    # 节点映射：所有 user 批次有配对（简化：下一个就是 assistant），全批都是节点
    win._build_node_to_batch_mapping = lambda: list(range(BATCH))
    win._get_batch_index_from_node_index = lambda idx: idx if 0 <= idx < BATCH else -1

    # 真实 prepend 渲染桩：同步建 user 卡并登记
    def _render(batches, insert_at_top=False, batch_offset=0, anchor_layout_index=None):
        for local, batch in enumerate(batches):
            gi = batch_offset + local
            if gi >= BATCH or win._batch_cards[gi] is not None:
                continue
            c = MessageCard(role="user", parent=container)
            c._message_index = gi
            c.setFixedHeight(CARD_H)
            c._lazy_rendered = True
            if insert_at_top:
                lay.insertWidget(0, c)
            else:
                lay.addWidget(c)
            win._batch_cards[gi] = [c]

    win._render_message_to_card = _render

    area.show()
    pump(14)
    container.adjustSize()
    pump(10)
    return win, cards


def main():
    win, cards = build()
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(sb.maximum())
    pump(10)
    print(f"起步（只加载尾部 {LOADED_TAIL} 批，视口在底部）value={sb.value()} max={sb.maximum()}", flush=True)

    # 点历史问题（目标批次未加载）→ 走 _scroll_to_target_node_index 主路径
    win._scroll_to_target_node_index(TARGET_NODE)
    # 真实等待：分批 prepend 由 100ms QTimer 驱动，落点由 16ms 收敛链驱动，
    # 探针必须让真实事件循环跑起来（processEvents 不会推进定时器到期）
    import time as _t
    deadline = _t.monotonic() + 6.0
    while _t.monotonic() < deadline:
        pump(3)
        _t.sleep(0.01)
        if win._visible_batch_start <= TARGET_NODE and win._jump_target_card is None and win._pending_scroll_to_index is None:
            break
    pump(6)
    landed = sb.value()
    target_card = win._batch_cards[TARGET_NODE]
    ty = target_card[0].y() if target_card else -1
    print(
        f"加载+落点后 value={landed} 目标y={ty} 上锁={win._is_jump_scroll_locked()} "
        f"visible_start={win._visible_batch_start}",
        flush=True,
    )

    # 懒渲染排空（历史上这里会把视口拽回底部）
    win._loading_session = True
    win._initial_scroll_to_bottom = True
    win._pending_lazy_cards = []
    win._lazy_batch_timer_active = True
    win._process_next_lazy_batch()
    pump(8)

    after = sb.value()
    drift = after - landed
    print(f"排空后 value={after} max={sb.maximum()} 位移={drift:+d}", flush=True)

    landed_ok = abs(landed - ty) <= AT_BOTTOM_TOLERANCE if ty >= 0 else False
    print(f"落点判定: landed={landed} target_y={ty} 偏差={landed - ty if ty >= 0 else 'N/A'}", flush=True)
    ok = win._visible_batch_start <= TARGET_NODE and landed_ok and abs(drift) <= AT_BOTTOM_TOLERANCE
    print(f"{'PASS' if ok else 'FAIL'} 未加载批次跳转后不得跑回底部", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
