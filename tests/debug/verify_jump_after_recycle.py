# -*- coding: utf-8 -*-
"""端到端验证：目标批次在加载途中被回收后，跳转仍必须成功。

跑法（项目根）：
    python tests/debug/verify_jump_after_recycle.py

复刻真机日志：
    12:39:14.820 WARNING [NodePreview] Card not found after history load, index=0

链路：长会话跳到最前面的消息（index=0）→ 分批 prepend → 途中虚拟滚动把刚建好
的批次回收（换成等高占位）→ `_scroll_to_pending_target` 落地时两条查找路径
（`_batch_cards` / 布局遍历）全落空 → 静默失败。

判据：落点必须落在目标卡 y 附近（±AT_BOTTOM_TOLERANCE），且不报 not found。
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
os.environ["DRIFOX_QT_RENDERER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.main_widget import AT_BOTTOM_TOLERANCE, OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

BATCH = 80
LOADED_TAIL = 12
CARD_H = 300
GAP = 8
VIEW_H = 600


def pump(n=10):
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
        "_jump_settle_last_geom": None,
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
    win._bump_layout_epoch = MagicMock()

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

    cards = [None] * BATCH
    batch_cards = [None] * BATCH
    message_batch = [[{"role": "user", "content": f"q{i}"}] for i in range(BATCH)]
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
    win._build_node_to_batch_mapping = lambda: list(range(BATCH))
    win._get_batch_index_from_node_index = lambda idx: idx if 0 <= idx < BATCH else -1

    # 真实 prepend 桩：建卡并登记
    def _render(batches, insert_at_top=False, batch_offset=0, anchor_layout_index=None):
        for local in range(len(batches)):
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

    # 回收桩：模拟虚拟滚动把批次换成等高占位（真机日志里的回收链）
    def _recycle(batch_indices):
        for gi in batch_indices:
            cardlist = win._batch_cards[gi]
            if not cardlist:
                continue
            c = cardlist[0]
            idx = lay.indexOf(c)
            h = c.height() + GAP
            lay.removeWidget(c)
            c.setParent(None)
            c.hide()
            ph = QWidget(container)
            ph.setFixedHeight(h)
            lay.insertWidget(idx if idx >= 0 else 0, ph)
            win._batch_placeholders[gi] = ph
            win._batch_cards[gi] = None

    win._force_recycle = _recycle

    area.show()
    pump(14)
    container.adjustSize()
    pump(10)
    return win


def main():
    win = build()
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(sb.maximum())
    pump(10)
    print(f"起步 value={sb.value()} max={sb.maximum()} 加载窗口={win._visible_batch_start}..{win._visible_batch_end}", flush=True)

    # 跳最前面的消息
    win._scroll_to_target_node_index(0)

    import time as _t

    deadline = _t.monotonic() + 8.0
    step = 0
    while _t.monotonic() < deadline:
        pump(3)
        _t.sleep(0.01)
        step += 1
        # 模拟加载途中被回收：加载到一半时把已建好的前几批回收掉
        if step == 40 and win._visible_batch_start > 8:
            victims = list(range(win._visible_batch_start, min(win._visible_batch_start + 6, BATCH)))
            win._force_recycle(victims)
            print(f"  途中回收批次 {victims}（模拟虚拟滚动）", flush=True)
        if win._visible_batch_start <= 0 and win._jump_target_card is None and win._pending_scroll_to_index is None:
            break
    pump(10)

    target = win._batch_cards[0]
    ty = target[0].y() if target else -1
    landed = sb.value()
    print(f"最终 value={landed} 目标y={ty} 占位={sorted(win._batch_placeholders)[:6]}...", flush=True)

    if ty < 0:
        print("FAIL 目标批次仍未重建（跳转失败）", flush=True)
        return 1
    ok = abs(landed - ty) <= AT_BOTTOM_TOLERANCE
    print(f"★ 落点偏差 = {landed - ty:+d}px（容差 ±{AT_BOTTOM_TOLERANCE}）", flush=True)
    print(f"{'PASS' if ok else 'FAIL'} 目标批次被回收后跳转仍必须成功", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
