# -*- coding: utf-8 -*-
"""真机验证：`_recycle_out_of_view_batches` 不再回收「未落高」批次。

跑法（项目根）：
    python tests/debug/verify_recycle_skips_stale_height.py

场景（对齐用户实机）：24 批，每批一张 300px 卡，视口停在底部。
  - 中间 8 批（8..15）姿态为「刚懒渲染完、高度还在起步 40px」
  - 其余批次姿态为「已落定 300px」

判据：
  修复前 → 中间 8 批被回收，占位高度 = 40/张（失真 260px/张）
  修复后 → 中间 8 批整批保留，`_batch_placeholders` 里没有它们
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

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

BATCH = 24
CARD_H = 300
START_H = 40
VIEW_H = 600
STALE_RANGE = range(8, 16)


def pump(n=8):
    for _ in range(n):
        app.processEvents()


def build():
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._programmatic_scroll_depth = 0
    win._scroll_max_cache = None
    win._is_virtual_recycling = False
    win._user_intentionally_away_from_bottom = False
    win._loading_session = False
    win._bottom_anchor_deadline = 0.0
    win._pending_scroll_to_bottom = False
    win._is_loading_history_batches = False
    win._last_visible_card_ids = set()
    win._restore_queue = []
    win._current_assistant_card = None
    win._layout_epoch = 0
    win._node_user_cards_cache = None
    win._rendered_card_count = 0
    win._recycle_lru_call_count = 0
    win._incremental_visible_batch_count = 8
    win._virtual_scroll_buffer = 1
    win._max_rendered_cards = 12
    win._unloaded_pids = []
    win._pending_lazy_cards = []
    win._lazy_batch_timer_active = False
    win._history_load_threshold = 48
    win._initial_scroll_to_bottom = True
    win._suppress_scroll_sync_count = 0
    win._visible_batch_start = 0
    win._visible_batch_end = BATCH
    win._batch_placeholder_overhead = {}

    win._log_render_quota = MagicMock()
    win._maybe_strong_recycle = MagicMock()
    win._recycle_lru_batches = MagicMock()
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

    area = QScrollArea()
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    area.setWidget(container)
    area.resize(520, VIEW_H)
    win.chat_scroll_area = area
    win.chat_container = container
    win.chat_layout = lay

    batch_cards = []
    message_batch = []
    for i in range(BATCH):
        card = MessageCard(role="assistant", parent=container)
        card._message_index = i
        card.setFixedHeight(CARD_H)
        card._lazy_rendered = True
        card._viewer_async_height = True
        card.viewer = QWidget(card)
        if i in STALE_RANGE:
            card.viewer.setFixedHeight(START_H)
            card._last_applied_viewer_height = START_H
        else:
            card.viewer.setFixedHeight(CARD_H)
            card._last_applied_viewer_height = CARD_H
        lay.addWidget(card)
        batch_cards.append([card])
        message_batch.append([{"role": "assistant", "content": f"batch {i}"}])

    win._batch_cards = batch_cards
    win._message_batch = message_batch
    win._batch_placeholders = {}
    win._user_prefix_cache = [0] * (BATCH + 1)

    area.show()
    pump(10)
    container.adjustSize()
    pump(6)
    return win


def main():
    win = build()
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(sb.maximum())  # 视口在底部 → 上方批次应被回收
    pump(8)
    print(f"起步：value={sb.value()} max={sb.maximum()}", flush=True)

    win._recycle_out_of_view_batches()
    pump(8)

    ph = set(win._batch_placeholders.keys())
    kept_stale = [i for i in STALE_RANGE if win._batch_cards[i] is not None]
    recycled_stale = [i for i in STALE_RANGE if win._batch_cards[i] is None]
    recycled_ok = [i for i in range(BATCH) if i not in STALE_RANGE and win._batch_cards[i] is None]

    print(f"占位批次 = {sorted(ph)}", flush=True)
    print(f"未落高批次 保留={kept_stale}  被回收={recycled_stale}", flush=True)
    print(f"已落定批次 被回收={recycled_ok}", flush=True)

    bad_ph = [i for i in ph if i in STALE_RANGE]
    ok = not bad_ph and not recycled_stale and bool(recycled_ok)
    if bad_ph:
        # 报出失真量级，便于对照修复前行为
        heights = {i: win._batch_placeholders[i].height() for i in bad_ph}
        print(f"失真占位高度（应为 300）：{heights}", flush=True)
    print(f"{'PASS' if ok else 'FAIL'} 未落高批次不得回收", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
