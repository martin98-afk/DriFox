# -*- coding: utf-8 -*-
"""虚拟滚动「回收 → 重建 → 高度塌陷 / 滚动漂移」自激回路回归（T42）。

## 用户可见现象

消息卡片最新对话滚不到底：一滚到底就自动弹回上方，随后触发视窗外清理，
再被重建/回收推进新一轮，形成「永远到不了底」的恶性循环。

## 环的四个节点

1. 滚到底 → `_on_chat_scrolled` 启动 `_virtual_scroll_timer`（500ms 单发）
2. `_recycle_out_of_view_batches` 回收视口外批次，为每批装**等高占位**
   （`_install_batch_placeholder`，高度取回收瞬间 `card.height()`）
3. 下一轮「第 1.5 步」对占位批次**原位重建**（`_take_batch_placeholder`）
4. 重建的卡片从最小高度（40px）起步，占位高度 H 被丢弃 → 容器塌 Σ(H-40)
   → `_restore_anchor_after_placeholder` 的锚点补偿算出巨大 `_delta`
   → `setValue(value + _delta)` 把视口拽走

## 手工复现

    python tests/debug/vscroll_recycle_rebuild_repro.py

实测输出（未修复，batch=30 / 卡片 300px / 视口 400px）：

    滚到底        value/max = 8602/8602   total = 9000
    回收后        value/max = 8602/8602   total = 9000   ← 等高占位生效
    上滚到批次 15  value/max = 4500/8602   total = 9000
    第 1.5 步重建  value/max = 5423/8602   total = 6335
                  HEIGHT: 9000 → 6335 (delta -2665)
                  SCROLL: 4500 → 5423 (drift +923)

本文件的断言直接以这两个数字为红线。

## 修法对应

- **A 高度守恒**：`_take_batch_placeholder` 连高度一起交回，重建的卡片先
  `setFixedHeight(记录高度)` 起步 → `test_take_batch_placeholder_returns_height`
- **B 程序性滚动隔离**：`_restore_anchor_after_placeholder` 的 `setValue`
  必须包进 `_programmatic_scroll()`，否则这次位移会被 `_on_scroll_changed`
  记成「用户主动滚离底部」→ away 置脏 → 所有跟底守卫集体失效
  → `test_placeholder_compensation_is_programmatic`
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from app.main_widget import AT_BOTTOM_TOLERANCE, OpenAIChatToolWindow
from app.widgets.message_card import MessageCard

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = REPO_ROOT / "app" / "main_widget.py"

BATCH = 30
CARD_H = 300
VIEWPORT_H = 400


# ─── 组装：能跑「回收 + 第 1.5 步重建」全链的最小窗口 ───────────────


def _make_window(qapp):
    """`__new__` 构造最小实例（与 test_render_quota_enforcement.py 同法）。

    关键点：滚动区必须真正 show + 布局跑一轮，否则 `container.height()==0`、
    卡片几何全零 → `_viewport_batch_range` 判 degenerate 直接退回加载窗口 →
    回收范围恒空，用例走不到目标路径。
    """
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

    # 与本案无关的外部依赖全部隔离（避免误触 WebEngine / 日志 / 强回收）
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

    # 重建桩：新建卡片保持 MessageCard 的真实起步高度（不 setFixedHeight）
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
        qapp.processEvents()
    container.adjustSize()
    for _ in range(3):
        qapp.processEvents()
    return win


def _run_one_cycle(win, qapp):
    """滚到底 → 回收 → 上滚到中段 → 再回收（触发第 1.5 步重建）。

    Returns:
        (before_total, after_total, before_value, after_value)
    """
    sb = win.chat_scroll_area.verticalScrollBar()
    total = lambda: win.chat_container.sizeHint().height()  # noqa: E731

    sb.setValue(sb.maximum())
    qapp.processEvents()
    win._recycle_out_of_view_batches()  # 第 1 轮：装等高占位
    qapp.processEvents()

    # 上滚到中段：让占位批次落进 active 范围，触发第 1.5 步重建
    sb.setValue(15 * CARD_H)
    qapp.processEvents()
    before_total, before_value = total(), sb.value()

    win._recycle_out_of_view_batches()  # 第 2 轮：原位重建占位批次
    qapp.processEvents()
    qapp.processEvents()
    return before_total, total(), before_value, sb.value()


# ─── 断言 1：回收阶段本身是好的（等高占位生效） ─────────────────────


def test_recycle_preserves_total_height(qapp):
    """回收视口外批次必须装等高占位，容器总高不变。

    这条是**前置校验**：说明「高度守恒」的机制本身存在且在回收侧生效。
    塌陷发生在下一轮重建，不在回收。
    """
    win = _make_window(qapp)
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(sb.maximum())
    qapp.processEvents()

    before = win.chat_container.sizeHint().height()
    win._recycle_out_of_view_batches()
    qapp.processEvents()
    after = win.chat_container.sizeHint().height()

    assert after == pytest.approx(before, abs=1), f"回收必须装等高占位保持总高：{before} → {after}"
    assert win._batch_placeholders, "前置条件：应有等高占位留下（否则重建路径无输入）"


# ─── 断言 2：环的根 —— 重建后容器高度不得塌陷 ──────────────────────


def test_rebuild_preserves_total_height(qapp):
    """回收 → 重建一轮后，容器总高必须守恒。

    当前实现 `_take_batch_placeholder` 只返回布局索引、**丢掉高度**，重建的
    卡片从 MessageCard 最小高度起步 → 容器骤降 Σ(H - 40)。实测（30 批）：
    9000 → 6335，塌 2665px。这条断言直接锁死该行为。
    """
    win = _make_window(qapp)
    before_total, after_total, _, _ = _run_one_cycle(win, qapp)

    assert after_total == pytest.approx(before_total, abs=1), (
        f"重建后总高不得塌陷：{before_total} → {after_total}"
        f"（塌 {before_total - after_total}px = 用户看到的「内容往下漂」）"
    )


# ─── 断言 3：用户可见症状 —— 滚动值不得漂移 ─────────────────────────


def test_rebuild_does_not_drift_scroll(qapp):
    """回收 → 重建一轮后，滚动值漂移必须 ≤ AT_BOTTOM_TOLERANCE。

    实测（未修复）drift +923px，远超 24px 容差 —— 用户看到的就是「滚到底
    被弹走」。高度守恒后 `_restore_anchor_after_placeholder` 的 `_delta`
    归零，drift 自然收敛。
    """
    win = _make_window(qapp)
    _, _, before_value, after_value = _run_one_cycle(win, qapp)

    drift = abs(after_value - before_value)
    assert drift <= AT_BOTTOM_TOLERANCE, (
        f"滚动值漂移 {drift}px 超过容差 {AT_BOTTOM_TOLERANCE}px"
        f"（{before_value} → {after_value}）= 用户看到的「一滚到底就被弹走」"
    )


# ─── 断言 4：占位高度必须交回调用方 ─────────────────────────────────


def test_take_batch_placeholder_returns_height(qapp):
    """`_take_batch_placeholder` 必须把已记录的高度一并交回。

    占位高度是「高度守恒」的唯一凭据；只返回 index 就等于把 H 丢掉，
    重建方无从恢复起步高度。
    """
    win = _make_window(qapp)
    assert win._install_batch_placeholder(batch_idx=1, height=777, layout_index=0), "前置条件：占位应安装成功"

    result = win._take_batch_placeholder(1)
    assert result is not None, "应取回占位信息"
    assert isinstance(result, (tuple, list)) and len(result) == 2, (
        f"必须连高度一起交回，实际返回 {result!r}（只给 index = 高度丢失 = 重建塌陷）"
    )
    assert result[1] == 777, f"高度必须原值交回，实际 {result[1]}"


# ─── 断言 5：不得残留基于瞬态几何的锚点补偿（源码断言） ─────────────


def test_no_transient_geometry_anchor_compensation():
    """占位重建路径**不得**再做「读几何算 Δ」的锚点补偿。

    历史实现（已于 T42 撤除）在 `singleShot(0)` 里读视口顶 widget 的 y 与重建前
    捕获值相减，`setValue(value + Δ)` 拉回原位。实测这条路读的是**中间态几何**：

        重建后立即   itemAt(15).y = 0      Δ = -4500  → setValue(0)    滚到顶
        单发执行时   itemAt(15).y = 5412   Δ = +912   → setValue(5412) 实测漂移
        布局稳定后   itemAt(15).y = 4500   Δ = 0      ← 正确值，但已晚

    正确性由「高度守恒」三件套接管：回收侧 `_install_batch_placeholder`、
    重建侧 `_apply_placeholder_height`、原位归还 `_take_batch_placeholder`。

    用源码断言（时序依赖事件循环，行为测只能覆盖单一时序）。
    """
    src = SRC_PATH.read_text(encoding="utf-8")

    assert "def _restore_anchor_after_placeholder(" not in src, (
        "不得存在基于瞬态几何的占位锚点补偿：回调在 singleShot(0) 执行时布局尚未收敛，"
        "读到的 y 是中间态（实测 4500 → 0 → 5412 → 4500），必然把视口拽错位置"
    )
    assert "def _apply_placeholder_height(" in src, (
        "重建侧必须有占位高度还原（高度守恒的另一半，缺了它容器总高会在重建瞬间塌陷）"
    )
    assert "_take_batch_placeholder" in src and "_install_batch_placeholder" in src, "占位安装/归还两侧必须成对存在"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
