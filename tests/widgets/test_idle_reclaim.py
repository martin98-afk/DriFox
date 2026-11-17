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

    def _pid_used_by_cards(self, pid):
        """不含池护栏的卡片共享判据（T31 池候选专用）。"""
        return pid in self.busy_pids


def _fresh_pids(spec):
    """(pid, ts偏移秒, idx) → 记账条目（ts 相对当前时间回退）。"""
    now = time.time()
    return [(pid, now - off, idx) for pid, off, idx in spec]


def test_idle_boundary_419_vs_421():
    """① 空闲 419s 不回收 / 421s 回收（#37 条 2：阈值 420s）"""
    host = _FakeWindow(
        unloaded_pids=_fresh_pids([(101, RENDERER_IDLE_RECLAIM_S - 1, 0)]),
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


# ── #37 条 1：renderer 池内 pid 纳入空闲回收候选域 ──────────────

def test_idle_threshold_is_90s():
    """条 2 常量：420→180s（#26）→90s（[#R3] 压到达下限时间非下限本身；代价=30-60s 重开场景 Chromium 重建 100-500ms）"""
    assert RENDERER_IDLE_RECLAIM_S == 90.0


def test_pool_idle_pid_enters_candidate_domain(monkeypatch):
    """条 1：池内空闲 pid 进入候选 → 被回收（池护栏不拦空闲回收路径）"""
    now = time.time()
    stale = [(now - RENDERER_IDLE_RECLAIM_S - 10, 901)]
    monkeypatch.setattr(memory_governor, "_idle_pool_pids", lambda: stale)
    host = _FakeWindow(
        unloaded_pids=[],
        batch_cards=[["c"]],
        distances={0: 50},
    )
    killed = reclaim_idle_renderers(host)
    assert killed == 1, "池内空闲 pid 必须进入回收候选域（原实现恒 0）"
    assert host.killed == [901]


def test_pool_keep_tab_pid_not_reclaimed(monkeypatch):
    """条 1 保底：keep-tab 在用 pid（切回复用语义）不被回收"""
    now = time.time()
    stale = [(now - RENDERER_IDLE_RECLAIM_S - 10, 902)]
    monkeypatch.setattr(memory_governor, "_idle_pool_pids", lambda: stale)
    card = type("Card", (), {"_renderer_pid": 902})()
    host = _FakeWindow(unloaded_pids=[], batch_cards=[["c"]], distances={0: 50})
    host._kept_session_tab = {"widgets": [card]}
    assert reclaim_idle_renderers(host) == 0, "keep-tab 保留期 pid 绝不回收"
    assert host.killed == []


def test_pool_candidate_shared_with_visible_card_not_reclaimed(monkeypatch):
    """池内 pid 与在用卡共享时绝不 kill（T31 误杀白卡根因）。

    renderer-process-limit 下多张卡共享 pid；池路径刻意绕过 _pid_still_in_use，
    但可见卡/流式卡的共享检查必须保留，否则 kill(n) 同秒崩 n 张卡。
    """
    now = time.time()
    monkeypatch.setattr(
        memory_governor, "_idle_pool_pids",
        lambda: [(now - RENDERER_IDLE_RECLAIM_S - 10, 904)],
    )
    host = _FakeWindow(unloaded_pids=[], batch_cards=[["c"]], distances={0: 50},
                       busy_pids={904})
    assert reclaim_idle_renderers(host) == 0
    assert host.killed == []


def test_pool_lru_keep_fresh_pid_not_reclaimed(monkeypatch):
    """条 1 保底：LRU keep 新鲜期（_idle_pool_pids 不返回刚归还项）"""
    monkeypatch.setattr(memory_governor, "_idle_pool_pids", lambda: [])
    host = _FakeWindow(unloaded_pids=[], batch_cards=[["c"]], distances={0: 50})
    assert reclaim_idle_renderers(host) == 0


def test_pool_candidate_without_timestamp_is_skipped():
    """无 _pool_idle_since 打点的池内 viewer 不得被当成「已空闲 421s」。"""
    from types import SimpleNamespace

    from app.widgets.webview_pool import WebViewPool

    pool = WebViewPool.get_instance()
    pool.clear()
    pool._idle[False] = [SimpleNamespace(_renderer_pid=905)]
    try:
        assert memory_governor._idle_pool_pids() == []
        assert reclaim_idle_renderers(_FakeWindow()) == 0
    finally:
        pool.clear()

# ── I2：死 pid 记账清理 + 池 clear 移交记账 ──────────────────────

def test_dead_pid_entry_purged_when_viewer_gone(monkeypatch):
    """I2①：kill 失败且进程已不存在（viewer 已随池 clear 出池）→ 死条目移出记账。

    旧实现：viewer=None 时 kill 恒 False 且 suppressed=False，两个分支都不进，
    条目滞留 → 每 45s 拍重复无效探测。修复后必须移出，且因未发生真 kill
    不写 60s 冷却（不推迟下拍其他窗口的真回收）。
    """
    now = time.time()
    host = _FakeWindow(
        unloaded_pids=[(911, now - RENDERER_IDLE_RECLAIM_S - 10, -1)],
        batch_cards=[["c"]],
        distances={0: 50},
    )
    monkeypatch.setattr(host, "_kill_renderer", lambda pid: False)  # NoSuchProcess 语义
    monkeypatch.setattr(memory_governor, "_pool_viewer_for_pid", lambda pid: None)
    monkeypatch.setattr(memory_governor, "_pid_alive", lambda pid: False)
    killed = reclaim_idle_renderers(host)
    assert killed == 0  # 死进程不算 kill 成功
    assert host._unloaded_pids == [], "死 pid 条目必须移出记账（不再每拍重复探测）"
    assert host._last_kill_at == 0.0, "死条目清理非 kill 动作，不得写 60s 冷却"

def test_alive_pid_kill_failure_keeps_entry(monkeypatch):
    """I2① 对照：进程仍活但 kill 失败（AccessDenied/校验拒）→ 记账保留，下拍重试"""
    now = time.time()
    host = _FakeWindow(
        unloaded_pids=[(912, now - RENDERER_IDLE_RECLAIM_S - 10, -1)],
        batch_cards=[["c"]],
        distances={0: 50},
    )
    monkeypatch.setattr(host, "_kill_renderer", lambda pid: False)
    monkeypatch.setattr(memory_governor, "_pool_viewer_for_pid", lambda pid: None)
    monkeypatch.setattr(memory_governor, "_pid_alive", lambda pid: True)
    assert reclaim_idle_renderers(host) == 0
    assert len(host._unloaded_pids) == 1, "活进程 kill 失败保留记账（与旧行为一致）"

def test_pool_clear_hands_over_pids_to_ledger():
    """I2②：池 clear 后池内 renderer pid 移交活窗口记账（进入 T31 候选域）。

    旧实现 clear 只 deleteLater：pid 随 viewer 销毁离开所有候选域，进程若未
    被 Chromium 自行回收则 ~60MB 无兜底滞留。修复后 clear 把 _renderer_pid
    经 _register_unloaded_pid 移交活窗口（复用其去重/背压），T31 按记账 ts
    在下拍兜底回收。无 pid 的 viewer（_renderer_pid=0）不得产生移交条目。
    """
    from types import SimpleNamespace

    from app.widgets.webview_pool import WebViewPool

    pool = WebViewPool.get_instance()
    pool.clear()
    registered = []
    win = _FakeWindow()
    win._register_unloaded_pid = lambda pid, ts, idx: registered.append((pid, ts, idx))
    window_registry.window_instances.clear()
    window_registry.register_window(win)
    pool._idle[False] = [
        SimpleNamespace(_renderer_pid=921),
        SimpleNamespace(_renderer_pid=922),
        SimpleNamespace(_renderer_pid=0),  # 无 pid：不移交
    ]
    try:
        pool.clear()
        assert [p for p, _, _ in registered] == [921, 922], "clear 后池内 pid 必须进入记账候选域"
        assert pool._idle[False] == [], "池对象照常销毁"
        assert all(idx == -1 for _, _, idx in registered), "移交条目 idx=-1（批次视为足够远，T31 可回收）"
    finally:
        window_registry.window_instances.clear()
        pool.clear()

def test_pool_clear_handover_silent_without_alive_window():
    """I2② 保底：无活窗口（退出期）移交静默跳过，clear 不炸不残留。"""
    from types import SimpleNamespace

    from app.widgets.webview_pool import WebViewPool

    pool = WebViewPool.get_instance()
    pool.clear()
    window_registry.window_instances.clear()
    pool._idle[True] = [SimpleNamespace(_renderer_pid=931)]
    try:
        pool.clear()
        assert pool._idle[True] == []
    finally:
        window_registry.window_instances.clear()
        pool.clear()


def test_pool_candidate_blocked_by_other_window(monkeypatch):
    """条 1 保底：池候选仍受跨窗口在用护栏约束"""
    now = time.time()
    monkeypatch.setattr(
        memory_governor, "_idle_pool_pids", lambda: [(now - RENDERER_IDLE_RECLAIM_S - 10, 903)]
    )
    host = _FakeWindow(unloaded_pids=[], batch_cards=[["c"]], distances={0: 50})
    other = _FakeWindow(busy_pids={903})
    window_registry.window_instances.clear()
    window_registry.register_window(other)
    try:
        assert reclaim_idle_renderers(host) == 0, "跨窗口在用 pid 绝不回收"
        assert host.killed == []
    finally:
        window_registry.window_instances.clear()
