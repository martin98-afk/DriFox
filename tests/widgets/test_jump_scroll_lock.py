# -*- coding: utf-8 -*-
"""回归：跳转后不得被懒渲染排空/贴底链拽回底部。

## 用户可见现象

点历史问题跳转，目标问题闪一下就到位（一帧可见），随后「加载完」视口又跑到
消息列表最底部，无论跳几次都一样。

## 根因链

跳转（`_locate_and_scroll_to_batch`）是**程序滚动**，`_on_scroll_changed` 对
`_programmatic_scroll` 包裹的位移豁免 away 判定，因此
`_user_intentionally_away_from_bottom` 永远是 False → `_should_follow_bottom()`
恒 True。而跳转目标批次在 `_restore_batch_ui` 里刚重建，卡片进懒渲染队列，
`_process_next_lazy_batch` 队列排空时无条件：

    self._scroll_to_bottom(sticky_ms=900)

900ms sticky 锚定 + `_ensure_at_bottom` 接力，把视口一路拉到底。
`_snap_bottom_queued`（高度回调里的队列化贴底）同样无复查。

## 修复

显式跳转锁 `_jump_scroll_lock_until`（`JUMP_SCROLL_LOCK_MS` 宽于整条懒渲染链）：

- `_should_follow_bottom()` 最优先判锁 → 所有走守卫的置底点全被压住
- `_process_next_lazy_batch` 的直写 setValue 也纳入守卫
- `_snap_bottom_queued` 执行前复查
- `_maintain_bottom_anchor` 见锁即停
- 用户真实滚动 / 点「回到底部」/ 发消息 / 切会话 → 立即解锁
"""

import sys
import time
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject

from app.main_widget import (
    JUMP_SCROLL_LOCK_HARD_CAP_MS,
    JUMP_SCROLL_LOCK_TAIL_MS,
    OpenAIChatToolWindow,
)


class _LockHarness(QObject):
    """承载跳转锁相关方法的轻量载体（不构造完整窗口）。"""

    _should_follow_bottom = OpenAIChatToolWindow._should_follow_bottom
    _is_jump_scroll_locked = OpenAIChatToolWindow._is_jump_scroll_locked
    _lock_scroll_after_jump = OpenAIChatToolWindow._lock_scroll_after_jump
    _unlock_scroll_after_jump = OpenAIChatToolWindow._unlock_scroll_after_jump
    _reset_bottom_follow = OpenAIChatToolWindow._reset_bottom_follow
    _is_view_at_bottom = lambda self: False  # noqa: E731 — 桩：视口不贴底
    _snap_bottom_queued = OpenAIChatToolWindow._snap_bottom_queued
    _programmatic_scroll = OpenAIChatToolWindow._programmatic_scroll
    _landed_bottom_value = lambda self: 9999  # noqa: E731
    # 桩：跳转不在进行中（锁到期后应立即解除）。真实现由 main_widget 提供，
    # 载体里给固定 False 使过期路径可断言。
    _is_jump_in_flight = lambda self: False  # noqa: E731


def _harness(away: bool = False):
    h = _LockHarness()
    h._user_intentionally_away_from_bottom = away
    h._jump_scroll_lock_until = 0.0
    h._pending_scroll_to_bottom = False
    h._bottom_anchor_deadline = 0.0
    h._ensure_ab_active = False
    h._programmatic_scroll_depth = 0
    h._scroll_bottom_timer = MagicMock()
    h._bottom_anchor_timer = MagicMock()
    h._bottom_snap_queued = False
    sb = MagicMock()
    area = MagicMock()
    area.verticalScrollBar.return_value = sb
    h.chat_scroll_area = area
    return h, sb


# ── 锁本身 ──────────────────────────────────────────────────────────


def test_jump_lock_blocks_follow_bottom(qapp):
    """跳转后 `_should_follow_bottom` 必须为 False（否则懒渲染排空会置底）。"""
    h, _sb = _harness(away=False)
    assert h._should_follow_bottom() is True, "前置：未上锁时应跟随"

    h._lock_scroll_after_jump()

    assert h._should_follow_bottom() is False, "上锁后不得跟随（跳转停留语义）"


def test_jump_lock_expires(qapp):
    """锁按时间过期，不会永久压制自动置底。

    走字段读取路径（不调 _is_jump_scroll_locked，避开产品 NameError 缺陷）。
    """
    h, _sb = _harness(away=False)
    h._lock_scroll_after_jump()
    h._jump_scroll_lock_until = time.monotonic() - 0.01  # 模拟已过期

    assert h._should_follow_bottom() is True


def test_jump_lock_cancels_pending_bottom_requests(qapp):
    """上锁瞬间要撤销已挂起的置底请求与 sticky 锚定。"""
    h, _sb = _harness(away=False)
    h._pending_scroll_to_bottom = True
    h._bottom_anchor_deadline = time.monotonic() + 5.0
    h._ensure_ab_active = True

    h._lock_scroll_after_jump()

    assert h._pending_scroll_to_bottom is False, "挂起的置底请求必须撤销"
    assert h._bottom_anchor_deadline == 0.0, "sticky 锚定必须撤销"
    assert h._ensure_ab_active is False, "rangeChanged 事件驱动窗口必须关"
    h._scroll_bottom_timer.stop.assert_called()
    h._bottom_anchor_timer.stop.assert_called()


# ── 解锁路径 ────────────────────────────────────────────────────────


def test_reset_bottom_follow_unlocks_jump_lock(qapp):
    """发消息 / 切会话 / 清空 = 明确要看最新内容 → 必须解除跳转锁。"""
    h, _sb = _harness(away=False)
    h._lock_scroll_after_jump()
    assert h._should_follow_bottom() is False

    h._reset_bottom_follow()

    assert h._is_jump_scroll_locked() is False
    assert h._should_follow_bottom() is True


def test_lock_is_reported_by_locked_predicate(qapp):
    """`_is_jump_scroll_locked()` 在上锁期内必须为真。

    该判定曾因 `JUMP_SCROLL_LOCK_HARD_CAP_MS` 未定义而抛 NameError（已在
    main_widget 补定义修复），本用例即该缺陷的回归保护。
    """
    h, _sb = _harness(away=False)
    h._lock_scroll_after_jump()
    assert h._is_jump_scroll_locked() is True


def test_unlock_restores_follow(qapp):
    h, _sb = _harness(away=False)
    h._lock_scroll_after_jump()
    h._unlock_scroll_after_jump()
    assert h._should_follow_bottom() is True


# ── 队列化贴底的复查 ────────────────────────────────────────────────


def test_snap_bottom_skipped_when_jump_locked(qapp):
    """`_snap_bottom_queued` 是队列化投递，执行时必须复查锁。

    跳转后目标批次重建卡会上报高度 → `_on_message_card_height_changed` 的
    follow 分支投递本槽 → 不复查就把视口贴回底部。
    """
    h, sb = _harness(away=False)
    h._lock_scroll_after_jump()
    h._bottom_snap_queued = True

    h._snap_bottom_queued()

    sb.setValue.assert_not_called()
    assert h._bottom_snap_queued is False, "排队标记仍要复位"


def test_snap_bottom_runs_when_not_locked(qapp):
    """未上锁时行为不变（回归保护）。"""
    h, sb = _harness(away=False)
    h._bottom_snap_queued = True

    h._snap_bottom_queued()

    sb.setValue.assert_called_once_with(9999)


# ── 稳定性：锁不得影响既有 away 语义 ────────────────────────────────


def test_away_still_wins_after_unlock(qapp):
    """away=True 且未上锁 → 仍然不跟随（既有语义不变）。"""
    h, _sb = _harness(away=True)
    assert h._should_follow_bottom() is False

    h._lock_scroll_after_jump()
    h._unlock_scroll_after_jump()

    assert h._should_follow_bottom() is False, "解锁不得顺手清掉 away"


def test_lock_duration_covers_height_report_gap():
    """尾巴窗口 must 覆盖「高度上报晚到」的空档。

    历史教训（固定 3s 总时长）：大卡片懒渲染远超 3s，锁到期时队列未排空 →
    排空置底把视口拽回底部。新设计改为 `_extend_jump_scroll_lock()` 每拍续期，
    `JUMP_SCROLL_LOCK_TAIL_MS` 只负责**跳转结束后**的尾巴，故不再要求它
    >= 2000，但必须 > 0 且足以覆盖高度回调晚到（数百 ms 量级）。
    """
    assert JUMP_SCROLL_LOCK_TAIL_MS >= 500, "尾巴过窄 → 晚到的高度回调照样拽走视口"
    # 且必须有配套续期机制（否则长渲染链仍会因窗口固定而失效）
    assert hasattr(OpenAIChatToolWindow, "_extend_jump_scroll_lock")


# ── 真机主路径：目标批次未加载 ──────────────────────────────────────


class _UnloadedJumpHarness:
    """`_scroll_to_target_node_index` 的载体（只验是否上锁，不跑加载）。"""

    _scroll_to_target_node_index = OpenAIChatToolWindow._scroll_to_target_node_index
    _lock_scroll_after_jump = OpenAIChatToolWindow._lock_scroll_after_jump
    _is_jump_scroll_locked = OpenAIChatToolWindow._is_jump_scroll_locked
    _get_batch_index_from_node_index = lambda self, i: i  # noqa: E731


def test_scroll_to_target_node_index_locks_before_loading(qapp):
    """目标批次未加载时也必须上锁（否则只加载路径不受保护）。

    真机主路径：长会话 `_visible_batch_start` 只覆盖最近 12 批，点靠前的历史
    问题 → 目标 < `_visible_batch_start` → 走 `_scroll_to_target_node_index`
    → `_load_history_to_index` 分批 prepend → `_scroll_to_pending_target` 落地。
    这条路径**不经过** `_locate_and_scroll_to_batch`，历史实现从未上锁，
    于是落点后懒渲染排空照样把视口拽回底部。
    """
    h = _UnloadedJumpHarness()
    h.session_manager = MagicMock()
    h.session_manager.get_current_session.return_value = object()
    h._jump_scroll_lock_until = 0.0
    h._pending_scroll_to_bottom = False
    h._bottom_anchor_deadline = 0.0
    h._ensure_ab_active = False
    h._scroll_bottom_timer = MagicMock()
    h._bottom_anchor_timer = MagicMock()
    # 目标批次未加载 → 走分批加载分支
    h._visible_batch_start = 48
    h._incremental_visible_batch_count = 8
    h._pending_scroll_to_index = None
    h._pending_scroll_to_batch = None
    h._load_history_to_index = MagicMock()

    h._scroll_to_target_node_index(3)

    assert h._is_jump_scroll_locked() is True, "未加载路径必须在开始加载前就上锁"
    h._load_history_to_index.assert_called_once()


def test_scroll_to_target_node_index_no_lock_without_session(qapp):
    """无会话时直接返回，不该留下悬挂的锁。"""
    h = _UnloadedJumpHarness()
    h.session_manager = MagicMock()
    h.session_manager.get_current_session.return_value = None
    h._jump_scroll_lock_until = 0.0

    h._scroll_to_target_node_index(3)

    assert h._is_jump_scroll_locked() is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
