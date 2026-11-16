# -*- coding: utf-8 -*-
"""WebViewPool idle 延迟清池（#26）回归测试。

背景：关 tab 后 viewer 入 WebViewPool 保活，池内 renderer 进程（单个
65~217MB）原本只有关闭最后一个 tab 才清池 → 「关闭对话后内存不回落」。
修复：release 入池挂 60s 单发 QTimer（DRIFOX_WEBVIEW_POOL_IDLE_GC 可关，
import 期固化），acquire 命中取消；到点有新鲜成员（<60s）重排下一窗，
全过期 clear() 清池。与 T31（memory_governor kill pid）正交：清池销毁
viewer 对象，T31 kill 进程，互不引用对方状态。

覆盖（QTimer 不等真实 60s：直接调 _idle_gc 驱动逻辑）：
1. env 开关两态：默认开（release 挂调度）/ 关（完全回现状不挂 timer）
2. release 挂调度：单发、interval 正确、多次 release 合并同一 timer
3. acquire 命中 → cancel
4. _idle_gc 新鲜度重排：有 <60s 成员 → 不清池且重排
5. _idle_gc 全过期 → clear() 清池
"""

import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.widgets.webview_pool as pool_mod  # noqa: E402
from app.widgets.webview_pool import (  # noqa: E402
    _IDLE_GC_DELAY_MS,
    WebViewPool,
)
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app

class _StubViewer(QWidget):
    """只实现池用到的接口（不触碰真实 WebEngine）。"""

    def __init__(self, pid=0):
        super().__init__()
        self._light_skeleton = False
        self._renderer_pid = pid
        self._context_lost = False
        self._page_dead = False

    def page(self):
        return None if self._page_dead else object()

    def reset_for_reuse(self):
        pass

@pytest.fixture
def pool(qapp):
    """每用例重置单例状态 + 停掉在途 timer，防跨用例串味。"""
    p = WebViewPool.get_instance()
    p._cancel_idle_gc()
    p.clear()
    WebViewPool.set_enabled(True)
    p._failures = 0
    p._idle_gc_timer = None
    for key in p._stats:
        p._stats[key] = 0
    yield p
    p._cancel_idle_gc()
    p.clear()

class TestEnvSwitch:
    def test_enabled_by_default_releases_schedule_gc(self, pool):
        """默认开：release 成功入池后挂出单发 timer 且 interval=60s"""
        assert pool.release(_StubViewer()) is True
        timer = pool._idle_gc_timer
        assert timer is not None
        assert timer.isActive()
        assert timer.isSingleShot()
        assert timer.interval() == _IDLE_GC_DELAY_MS

    def test_env_off_disables_scheduling(self, pool, monkeypatch):
        """env 关（_IDLE_GC_ENABLED=False）：完全回现状，不挂 timer，入池行为不变"""
        monkeypatch.setattr(pool_mod, "_IDLE_GC_ENABLED", False)
        assert pool.release(_StubViewer()) is True
        assert pool._idle_gc_timer is None
        assert pool.size() == 1

class TestScheduleAndCancel:
    def test_multiple_releases_merge_into_one_timer(self, pool):
        """多次 release：start() 重启合并为同一 timer 实例"""
        pool.release(_StubViewer())
        t1 = pool._idle_gc_timer
        pool.release(_StubViewer())
        assert pool._idle_gc_timer is t1
        assert t1.isActive()

    def test_acquire_hit_cancels_pending_gc(self, pool):
        """60s 内重开 tab：acquire 命中 → 取消待执行清池"""
        v = _StubViewer()
        pool.release(v)
        assert pool._idle_gc_timer.isActive()
        assert pool.acquire() is v
        assert not pool._idle_gc_timer.isActive()

    def test_partial_acquire_keeps_remaining_viewers_scheduled(self, pool):
        """仅复用一部分页面时，剩下的页面仍须按原窗口回收。"""
        first = _StubViewer()
        remaining = _StubViewer()
        pool.release(first)
        pool.release(remaining)
        assert pool.acquire() is first
        assert pool.size() == 1
        assert pool._idle_gc_timer.isActive(), "部分取用不可取消剩余页面的回收"

    def test_other_bucket_still_has_idle_gc_after_acquire(self, pool):
        """full 桶清空不能取消 light 桶剩余页面的回收。"""
        full = _StubViewer()
        light = _StubViewer()
        pool.release(full)
        pool.release(light, light=True)
        assert pool.acquire() is full
        assert pool.size(light=True) == 1
        assert pool._idle_gc_timer.isActive()

class TestIdleGc:
    def test_fresh_member_reschedules_and_keeps(self, pool):
        """GC 到点有 <60s 新鲜成员 → 不清池、重排下一窗"""
        v = _StubViewer()
        pool.release(v)
        v._pool_idle_since = time.time()  # 强制新鲜
        pool._idle_gc_timer.stop()  # 模拟「到点但成员新鲜」
        pool._idle_gc()
        assert pool.size() == 1, "新鲜成员不得被清"
        assert pool._idle_gc_timer.isActive(), "应重排下一窗"

    def test_all_expired_clears_pool(self, pool):
        """全部成员过期 → clear() 清池"""
        v = _StubViewer(pid=4242)
        pool.release(v)
        v._pool_idle_since = time.time() - (_IDLE_GC_DELAY_MS / 1000.0) - 1
        pool._idle_gc()
        assert pool.size() == 0

    def test_member_without_stamp_treated_as_expired(self, pool):
        """升级前老实例无 _pool_idle_since 打点 → 视为过期可清（销毁对象方向安全）"""
        v = _StubViewer()
        pool.release(v)
        v._pool_idle_since = 0
        pool._idle_gc()
        assert pool.size() == 0
