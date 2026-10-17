# -*- coding: utf-8 -*-
"""端到端验证：跳转后懒渲染排空不再把视口拽回底部。

跑法（项目根）：
    python tests/debug/verify_jump_lock_end_to_end.py

复刻用户实机链路：
  1. 24 批会话，视口停在底部（用户从底部的历史问题列表点跳转）
  2. 跳转到中间批次 → `_locate_and_scroll_to_batch` 落点 + 上锁
  3. 目标批次卡片进懒渲染队列，`_process_next_lazy_batch` 逐拍消费
  4. 队列排空（历史上这一步会 `_scroll_to_bottom(sticky_ms=900)`）

判据：
  修复前 → 跳转落点≈目标 y，排空后 value 被拽到 maximum（画面跑到底）
  修复后 → 排空后 value 仍在跳转落点附近（偏差 ≤ AT_BOTTOM_TOLERANCE）
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

BATCH = 24
CARD_H = 300
GAP = 8
VIEW_H = 600
JUMP_TARGET = 8


def pump(n=10):
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
    win._initial_scroll_to_bottom = False
    win._suppress_scroll_sync_count = 0
    win._visible_batch_start = 0
    win._visible_batch_end = BATCH
    win._batch_placeholder_overhead = {}
    win._jump_scroll_lock_until = 0.0
    win._jump_target_card = None
    win._jump_settle_left = 0
    win._jump_settle_last_value = None
    win._ensure_ab_active = False
    win._ensure_ab_active_until = 0.0
    win._bottom_snap_queued = False
    win._last_quota_log_at = 0.0
    win._scroll_bottom_timer = MagicMock()
    win._bottom_anchor_timer = MagicMock()
    win._virtual_scroll_timer = MagicMock()
    win._scroll_sync_timer = MagicMock()

    win._log_render_quota = MagicMock()
    win._maybe_strong_recycle = MagicMock()
    win._recycle_lru_batches = MagicMock()
    win._try_detach_card_viewer = lambda card: False
    win._is_widget_alive = lambda w: True
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
    lay.setSpacing(GAP)
    area.setWidget(container)
    area.resize(520, VIEW_H)
    win.chat_scroll_area = area
    win.chat_container = container
    win.chat_layout = lay

    cards = []
    batch_cards = []
    message_batch = []
    for i in range(BATCH):
        c = MessageCard(role="user", parent=container)
        c._message_index = i
        c.setFixedHeight(CARD_H)
        c._lazy_rendered = True
        lay.addWidget(c)
        cards.append(c)
        batch_cards.append([c])
        message_batch.append([{"role": "user", "content": f"q{i}"}])
    win._batch_cards = batch_cards
    win._message_batch = message_batch
    win._batch_placeholders = {}
    win._user_prefix_cache = [0] * (BATCH + 1)

    area.show()
    pump(12)
    container.adjustSize()
    pump(8)
    return win, cards


def main():
    win, cards = build()
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(sb.maximum())  # 用户停在底部
    pump(8)
    print(f"起步（视口在底部）value={sb.value()} max={sb.maximum()}", flush=True)

    # 模拟跳转目标批次被回收后重建：一张待懒渲染的 assistant 卡
    lazy = MessageCard(role="assistant", parent=win.chat_container)
    lazy.setFixedHeight(CARD_H)
    lazy._lazy_rendered = False
    win.chat_layout.insertWidget(JUMP_TARGET, lazy)

    ok_jump = win._locate_and_scroll_to_batch(JUMP_TARGET)
    pump(6)
    landing = sb.value()
    print(
        f"跳转落点 value={landing}（目标卡 y={lazy.y()}）上锁={win._is_jump_scroll_locked()} 成功={ok_jump}",
        flush=True,
    )

    # 目标批次进懒渲染队列（实机里由 _restore_batch_ui → _render_message_to_card 入队）
    win._pending_lazy_cards.append(lazy)
    win._lazy_batch_timer_active = True
    win._loading_session = True

    # 懒渲染排空（历史上这里必然置底）
    for _ in range(4):
        win._process_next_lazy_batch()
        pump(4)
    after_drain = sb.value()
    print(f"懒渲染排空后 value={after_drain} max={sb.maximum()}", flush=True)

    drift = after_drain - landing
    ok = bool(ok_jump) and abs(drift) <= AT_BOTTOM_TOLERANCE
    print(f"★ 排空造成的位移 = {drift:+d}px（容差 ±{AT_BOTTOM_TOLERANCE}）", flush=True)
    print(f"{'PASS' if ok else 'FAIL'} 跳转后不得被懒渲染排空拽回底部", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
