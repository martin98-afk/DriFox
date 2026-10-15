# -*- coding: utf-8 -*-
"""探针 AB：上滚到顶触发 prepend 历史加载 → value 演化全追踪（用完即删）。

对齐用户症状：「长会话滚轮上下滚动时动态加载（历史）后滚轮出现跳变，没保持原位」

观测点：
  t0 prepend 前
  t1 prepend 渲染 + restore_anchor 后
  t2 懒渲染逐批完成 + 卡片高度上报后
  t3 片刻之后（滚轮残余/回收）
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

try:
    init_shared_web_profile()
except Exception:  # noqa: BLE001
    pass

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

VIEWPORT_H = 400


def p(*a):
    print(*a)
    sys.stdout.flush()


# 内容：长度不同的 markdown（模拟真实不等高卡片）
def _msg(i):
    n = (i % 5) + 1
    return {"role": "assistant", "content": f"### 消息 {i}\n\n" + "\n".join(f"- 列表项 {j}：这是一段用于撑高度的正文。" for j in range(n))}


class Win:
    pass


def build_window(total_batches=24, visible=8):
    from qfluentwidgets import SingleDirectionScrollArea

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
        "_incremental_visible_batch_count": visible,
        "_virtual_scroll_buffer": 1,
        "_max_rendered_cards": 40,
        "_min_rendered_cards": 6,
        "_unloaded_pids": [],
        "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False,
        "_history_load_threshold": 48,
        "_initial_scroll_to_bottom": True,
        "_suppress_scroll_sync_count": 0,
        "_visible_batch_start": max(0, total_batches - visible),
        "_visible_batch_end": total_batches,
        "_message_batch": [[_msg(i)] for i in range(total_batches)],
        "_batch_placeholders": {},
        "_batch_placeholder_overhead": {},
        "_history_load_threshold": 48,
    }.items():
        setattr(win, key, value)

    win._log_render_quota = MagicMock()
    win._maybe_strong_recycle = MagicMock()
    win._recycle_lru_batches = MagicMock()
    win._sync_global_rendered_pages = MagicMock()
    win._is_widget_alive = lambda w: w is not None
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "s1"
    win._get_user_round_index_for_batch_index = MagicMock(return_value=0)
    win._update_card_diff_stats = MagicMock()
    _fake_session = MagicMock()
    _fake_session.session_id = "s1"
    _fake_session.messages = []
    _fake_sm = MagicMock()
    _fake_sm.get_current_session.return_value = _fake_session
    win.session_manager = _fake_sm
    win._restore_meta_from_batch = MagicMock()

    scroll_area = SingleDirectionScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.setSpacing(6)
    layout.setAlignment(Qt.AlignBottom)
    scroll_area.setWidget(container)
    scroll_area.setWidgetResizable(True)
    scroll_area.resize(500, VIEWPORT_H)
    win.chat_scroll_area = scroll_area
    win.chat_container = container
    win.chat_layout = layout

    scroll_area.show()
    container.show()
    for _ in range(3):
        app.processEvents()
    return win


win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
total_h = lambda: win.chat_container.sizeHint().height()  # noqa: E731

# 初始加载最后 8 批（真实路径）
win._batch_cards = [None] * len(win._message_batch)
win._build_user_prefix_cache()
visible = win._message_batch[win._visible_batch_start : win._visible_batch_end]
win._render_message_to_card(visible, batch_offset=win._visible_batch_start)
for _ in range(6):
    app.processEvents()

p(f"初始: batches {win._visible_batch_start}..{win._visible_batch_end}  hint={total_h()} value={sb.value()} max={sb.maximum()}")

# 让懒渲染跑完（等 WebEngine 高度稳定）
p("等待懒渲染（5s）...")
t = 0
while t < 5000:
    app.processEvents()
    QApplication.processEvents()
    sys.stdout.flush()
    import time

    time.sleep(0.02)
    t += 20

p(f"懒渲染后: hint={total_h()} value={sb.value()} max={sb.maximum()}")
heights = [c.height() for c in (win._batch_cards[i][0] if win._batch_cards[i] else None for i in range(len(win._batch_cards))) if c]
p(f"卡片高度样本: {heights[:10]}")

# ─── 用户上滚到顶 ───
sb.setValue(0)
for _ in range(3):
    app.processEvents()
p(f"\n滚到顶: value={sb.value()} max={sb.maximum()} away={win._user_intentionally_away_from_bottom}")

# ─── 触发 prepend 历史加载 ───
prev_h = total_h()
prev_v = sb.value()
p(f"prepend 前: hint={prev_h} value={prev_v}")

win._load_more_history_batches()
p(f"prepend 同步返回后: hint={total_h()} value={sb.value()} max={sb.maximum()} start={win._visible_batch_start}")

for _ in range(5):
    app.processEvents()
p(f"restore_anchor 后（下一拍）: hint={total_h()} value={sb.value()} max={sb.maximum()}")

# ─── 追踪懒渲染期间 value 演化 ───
import time  # noqa: E402

t = 0
samples = []
while t < 4000:
    app.processEvents()
    samples.append((t, sb.value(), sb.maximum(), total_h()))
    time.sleep(0.05)
    t += 50

p("\n=== value 演化（每 200ms 抽样）===")
for s in samples[::4]:
    p(f"  t={s[0]:>5}ms  value={s[1]:>6}  max={s[2]:>6}  hint={s[3]:>6}")

p(f"\n最终: value={sb.value()} max={sb.maximum()} hint={total_h()}")
p(f"漂移: value {prev_v} -> {sb.value()}（Δ{sb.value() - prev_v:+d}）")

sys.stdout.flush()
os._exit(0)
