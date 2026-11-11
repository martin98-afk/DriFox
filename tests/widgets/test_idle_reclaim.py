# -*- coding: utf-8 -*-
"""T31 验证：renderer 空闲超时回收 reclaim_idle_renderers。

offscreen 运行，无键鼠、无可见窗口（P0 约束）。Mock host + patch 采样无关
（本回收器只看记账 ts，不读 RSS —— 与 RSS 闸门判据正交的设计验证点之一）。

覆盖：
① 空闲 599s 不回收 / 601s 回收
② 被其他窗口复用 → 跳过（全局护栏）
③ 流式卡片占用（_pid_still_in_use 语义含流式卡）→ 跳过
④ 离屏护栏内（批次距离 < 8/12）→ 跳过
⑤ kill 异常兜底不冒泡
⑥ 回收后记账正确移除、不重复 kill（记账空 / 冷却生效）
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

import pytest

from app.core.infra import memory_governor
from app.core.infra import window_registry
from app.core.infra.memory_governor import (
    RENDERER_IDLE_RECLAIM_BATCH_MAX,
    RENDERER_IDLE_RECLAIM_S,
    reclaim_idle_renderers,
)


class _FakeWindow:
    """最小窗口替身：busy_pids 覆盖常规卡片与流式卡片两种占用语义。"""

    def __init__(self, unloaded_pids=None, batch_cards=None, distances=None,
                 busy_pids=(), active=False, kill_error=False):
        self._unloaded_pids = list(unloaded_pids or [])
        self._batch_cards = list(batch_cards or [])
        self.distances = dict(distances or {})
        self.busy_pids = set(busy_pids)
        self.active = active
        self.kill_error = kill_error
        self._last_kill_at = 0.0
        self.killed = []

    def _is_active_window_for_recycle(self):
        return self.active

    def _batch_distance(self, idx):
        return self.distances.get(idx, 10 ** 9)

    def _pid_still_in_use(self, pid):
        # 真 main_widget 语义：在用 = 常规批次卡片 或 流式 _current_assistant_card
        return pid in self.busy_pids

    def _kill_renderer(self, pid):
        if self.kill_error:
            raise OSError("access denied")
        self.killed.append(pid)
        return True


def _fresh_pids(spec):
    """(pid, ts偏移秒, idx) → 记账条目（ts 相对当前时间回退）。"""
    now = time.time()
    return [(pid, now - off, idx) for pid, off, idx in spec]


def test_idle_boundary_599_vs_601():
    """① 空闲 599s 不回收 / 601s 回收"""
    now = time.time()
    host = _FakeWindow(
        unloaded_pids=_fresh_pids([(101, 599, 0)]),
        batch_cards=[["c"]],
        distances={0: 50},
    )
    assert reclaim_idle_renderers(host) == 0
    assert host.killed == []
    assert len(host._unloaded_pids) == 1

    host2 = _FakeWindow(
        unloaded_pids=_fresh_pids([(102, RENDERER_IDLE_RECLAIM_S + 1, 0)]),
        batch_cards=[["c"]],
        distances={0: 50},
    )
    assert reclaim_idle_renderers(host2) == 1
    assert host2.killed == [102]


def test_skip_pid_used_by_other_window():
    """② 被其他存活窗口复用 → 跳过（全局护栏）"""
    now = time.time()
    host = _FakeWindow(
        unloaded_pids=_fresh_pids([(201, RENDERER_IDLE_RECLAIM_S + 10, 0)]),
        batch_cards=[["c"]],
        distances={0: 50},
    )
    other = _FakeWindow(busy_pids={201})
    window_registry.window_instances.clear()
    window_registry.register_window(other)
    try:
        assert window_registry.alive_window_instances() == [other]
        assert reclaim_idle_renderers(host) == 0
        assert host.killed == []
        assert len(host._unloaded_pids) == 1  # 记账保留（不丢账）
    finally:
        window_registry.window_instances.clear()


def test_skip_streaming_card_busy():
    """③ 流式卡片占用（busy 语义含流式 _current_assistant_card）→ 跳过"""
    host = _FakeWindow(
        unloaded_pids=_fresh_pids([(301, RENDERER_IDLE_RECLAIM_S + 10, 0)]),
        batch_cards=[["c"]],
        distances={0: 50},
        busy_pids={301},  # 真实现：流式卡 _renderer_pid 命中即 busy
    )
    assert reclaim_idle_renderers(host) == 0
    assert host.killed == []


def test_skip_inside_offscreen_guard():
    """④ 离屏护栏内（距离 < 8/12）→ 跳过"""
    host = _FakeWindow(
        unloaded_pids=_fresh_pids([(401, RENDERER_IDLE_RECLAIM_S + 10, 0)]),
        batch_cards=[["c"]],
        distances={0: 3},  # < 8
        active=False,
    )
    assert reclaim_idle_renderers(host) == 0
    assert host.killed == []


def test_kill_exception_swallowed():
    """⑤ kill 异常兜底不冒泡，记账保留待下轮"""
    host = _FakeWindow(
        unloaded_pids=_fresh_pids([(501, RENDERER_IDLE_RECLAIM_S + 10, 0)]),
        batch_cards=[["c"]],
        distances={0: 50},
        kill_error=True,
    )
    assert reclaim_idle_renderers(host) == 0
    assert len(host._unloaded_pids) == 1  # 未成功 kill → 记账不删


def test_ledger_cleanup_and_no_repeat():
    """⑥ 回收后记账正确移除；冷却 + 记账空 → 不重复 kill"""
    now = time.time()
    host = _FakeWindow(
        unloaded_pids=_fresh_pids([(601, RENDERER_IDLE_RECLAIM_S + 10, 0),
                                   (602, RENDERER_IDLE_RECLAIM_S + 10, 1),
                                   (603, 5, 2)]),  # 603 空闲仅 5s（相对偏移）
        batch_cards=[["c"], ["c"], ["c"]],
        distances={0: 50, 1: 60, 2: 70},
    )
    killed = reclaim_idle_renderers(host)
    assert sorted(host.killed) == [601, 602]
    assert killed == 2
    # 603 空闲不足 → 记账保留；已杀的移除
    assert [e[0] for e in host._unloaded_pids] == [603]
    # 冷却生效（_last_kill_at 刚写）→ 下一拍直接返回 0，不重复 kill
    assert reclaim_idle_renderers(host) == 0
    assert host.killed == [601, 602]


def test_batch_max_cap():
    """每拍 ≤ RENDERER_IDLE_RECLAIM_BATCH_MAX 个"""
    n = RENDERER_IDLE_RECLAIM_BATCH_MAX + 4
    host = _FakeWindow(
        unloaded_pids=_fresh_pids([(700 + i, RENDERER_IDLE_RECLAIM_S + 10 + i, i) for i in range(n)]),
        batch_cards=[["c"]] * n,
        distances={i: 50 + i for i in range(n)},
    )
    killed = reclaim_idle_renderers(host)
    assert killed == RENDERER_IDLE_RECLAIM_BATCH_MAX
