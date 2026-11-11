# -*- coding: utf-8 -*-
"""T22 验证：动态阈值闸门（主进程 + renderer 双判据）。

offscreen 运行，无键鼠、无可见窗口（P0 约束）。Mock host（鸭子类型）+
patch rss_sampler 缓存读取，主线程零 psutil。

覆盖：
① 主进程超软阈且 _unloaded_pids 为空 → 仍触发（T21 失明修复）
② 主进程超硬阈 → main-hard（含 kill）
③ renderer 超软阈 → 温和卸载远批；超硬阈 → kill 重且远优先
④ 双判据都低 → idle 不动作
⑤ 活跃视口豁免：dist < min_offscreen(8/12) 的批不卸、pid 不杀
⑥ 全局护栏：pid 被其他存活窗口在用 → 跳过（T16 思路）
⑦ 冷却中 → cooldown 不动作
⑧ 异常兜底：采样抛异常 → 'error' 不冒泡
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

import pytest

from app.core.infra import memory_governor
from app.core.infra import rss_sampler as rss_sampler_mod
from app.core.infra.memory_governor import (
    _MAIN_RSS_GATE_HARD_MB,
    _MAIN_RSS_GATE_SOFT_MB,
    RENDERER_RSS_GATE_HARD_MB,
    RENDERER_RSS_GATE_SOFT_MB,
    enforce_renderer_rss_gate,
)


class _FakeWindow:
    """最小窗口替身：鸭子类型满足闸门所需接口。"""

    def __init__(self, unloaded_pids=None, batch_cards=None, distances=None,
                 busy_pids=(), active=False):
        self._unloaded_pids = list(unloaded_pids or [])
        self._batch_cards = list(batch_cards or [])
        self.distances = dict(distances or {})
        self.busy_pids = set(busy_pids)
        self.active = active
        self._last_kill_at = 0.0
        self.killed = []
        self.unloaded_batches = []

    def _is_active_window_for_recycle(self):
        return self.active

    def _batch_distance(self, idx):
        return self.distances.get(idx, 10 ** 9)

    def _pid_still_in_use(self, pid):
        return pid in self.busy_pids

    def _kill_renderer(self, pid):
        self.killed.append(pid)
        return True

    def _unload_batch(self, idx):
        self.unloaded_batches.append(idx)
        return 0


@pytest.fixture
def sampler():
    """patch 采样器三个读取接口（主线程缓存语义；patch 进程级单例）。"""
    singleton = rss_sampler_mod.rss_sampler
    with patch.object(singleton, "rss_mb", return_value=0.0), \
         patch.object(singleton, "web_rss_mb", return_value=0.0), \
         patch.object(singleton, "web_rss_by_pid", return_value={}):
        yield singleton


def _set_main(mb):
    rss_sampler_mod.rss_sampler.rss_mb.return_value = mb


def _set_web(mb, by_pid=None):
    rss_sampler_mod.rss_sampler.web_rss_mb.return_value = mb
    rss_sampler_mod.rss_sampler.web_rss_by_pid.return_value = dict(by_pid or {})


def test_main_over_threshold_with_empty_pids_still_triggers(sampler):
    """① 主进程超软阈 + 记账为空 → 仍触发（T21 失明修复的核心）"""
    _set_main(_MAIN_RSS_GATE_SOFT_MB + 100)
    _set_web(0.0)  # renderer 全无
    host = _FakeWindow(unloaded_pids=[], batch_cards=[])  # 记账空、批次空
    with patch.object(memory_governor, "_cleanup_global_lru_caches") as clean:
        result = enforce_renderer_rss_gate(host)
    assert "main-soft" in result
    assert clean.called  # 缓存清理被调用


def test_main_hard_threshold_kills_recorded_renderers(sampler):
    """② 主进程超硬阈 → main-hard：清缓存 + 温和卸载 + kill 记账 renderer"""
    _set_main(_MAIN_RSS_GATE_HARD_MB + 100)
    _set_web(0.0)
    host = _FakeWindow(
        unloaded_pids=[(501, 1.0, 0), (502, 2.0, 1)],
        batch_cards=[["c"], ["c"]],
        distances={0: 100, 1: 101},
    )
    with patch.object(memory_governor, "_cleanup_global_lru_caches"):
        result = enforce_renderer_rss_gate(host)
    assert "main-hard" in result
    assert sorted(host.killed) == [501, 502]
    assert host._unloaded_pids == []  # kill 后记账清理


def test_renderer_soft_unload_and_hard_kill(sampler):
    """③ renderer 软阈 → 温和卸载；硬阈 → kill 重且远优先"""
    _set_main(0.0)
    _set_web(RENDERER_RSS_GATE_SOFT_MB + 50)
    host = _FakeWindow(
        batch_cards=[["c"], ["c"]],
        distances={0: 5, 1: 50},  # 0 批在护栏内，1 批离屏远
    )
    result = enforce_renderer_rss_gate(host)
    assert "soft-unload" in result
    assert host.unloaded_batches == [1]  # 只卸远批（近批豁免）

    _set_web(RENDERER_RSS_GATE_HARD_MB + 100, {601: 200.0, 602: 90.0})
    host2 = _FakeWindow(
        unloaded_pids=[(601, 1.0, 0), (602, 2.0, 1)],
        batch_cards=[["c"], ["c"]],
        distances={0: 30, 1: 40},
    )
    result2 = enforce_renderer_rss_gate(host2)
    assert "kill" in result2
    # 重（200MB）的 601 先杀，90MB 的 602 也在 ≤12 上限内被杀
    assert sorted(host2.killed) == [601, 602]


def test_both_below_threshold_idle(sampler):
    """④ 双判据都低 → idle，零动作"""
    _set_main(500.0)
    _set_web(100.0)
    host = _FakeWindow(
        unloaded_pids=[(700, 1.0, 0)],
        batch_cards=[["c"]],
        distances={0: 100},
    )
    result = enforce_renderer_rss_gate(host)
    assert result == "idle"
    assert host.killed == []
    assert host.unloaded_batches == []


def test_viewport_exempt_and_cooldown(sampler):
    """⑤⑥ 视口豁免 + 冷却：护栏内的批/pid 绝不动；冷却中不动作"""
    _set_main(0.0)
    _set_web(RENDERER_RSS_GATE_HARD_MB + 100, {801: 200.0})
    # pid 801 距离 3（< 8 护栏）→ 不进 kill 域
    host = _FakeWindow(
        unloaded_pids=[(801, 1.0, 0)],
        batch_cards=[["c"]],
        distances={0: 3},
    )
    result = enforce_renderer_rss_gate(host)
    assert "kill(0)" in result
    assert host.killed == []

    # 冷却中：_last_kill_at 刚设置 → cooldown
    import time
    host._last_kill_at = time.time()
    result2 = enforce_renderer_rss_gate(host)
    assert result2 == "cooldown"


def test_global_guard_skips_pid_used_by_other_window(sampler):
    """⑥ 全局护栏：pid 被其他存活窗口在用 → 跳过（T16 思路）"""
    _set_main(0.0)
    _set_web(RENDERER_RSS_GATE_HARD_MB + 100, {901: 300.0})
    host = _FakeWindow(
        unloaded_pids=[(901, 1.0, 0)],
        batch_cards=[["c"]],
        distances={0: 50},
    )
    # 其他窗口的卡片正在用 901
    other = _FakeWindow(busy_pids={901})
    from app.core.infra import window_registry
    window_registry.window_instances.clear()
    window_registry.register_window(other)
    try:
        assert window_registry.alive_window_instances() == [other]
        result = enforce_renderer_rss_gate(host)
        assert host.killed == []  # 全局护栏拦下
        assert "kill(0)" in result
    finally:
        window_registry.window_instances.clear()


def test_sampler_exception_bubbles_to_error(sampler):
    """⑧ 采样异常 → 'error' 兜底，不冒泡"""
    _set_main(0.0)
    _set_web(0.0)
    rss_sampler_mod.rss_sampler.rss_mb.side_effect = RuntimeError("sampler dead")
    host = _FakeWindow()
    result = enforce_renderer_rss_gate(host)
    assert result == "error"
    rss_sampler_mod.rss_sampler.rss_mb.side_effect = None
