# -*- coding: utf-8 -*-
"""WebView 复用池（方案 A）回归测试。

覆盖：
* 借还基本语义：空池 miss、还了再借命中同一实例、``reset_for_reuse`` 被调用；
* 配额：超过 ``MAX_IDLE_PER_BUCKET`` 时淘汰最老的；
* 分桶：light（welcome 骨架）与常规骨架互不串用；
* 失效剔除：page 已释放 / 上下文丢失的实例不会被复用；
* 隐藏宿主：``WA_DontShowOnScreen``（防幽灵窗口）；
* 与 B4 强回收协同：``pids()`` 暴露池中 renderer PID；
* 降级：停用开关、连续失败自动停用；
* 信号对偶性：``_connect_viewer_signals`` 与 ``_disconnect_viewer_signals``
  必须覆盖同一组信号（防止将来新增信号只加一边，导致池化复用时残留旧连接）。

运行::

    python -m pytest tests/widgets/test_webview_pool.py -v
"""

import ast
import inspect
import os
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QWidget  # noqa: E402

from app.widgets.message_card import MessageCard  # noqa: E402
from app.widgets.webview_pool import (  # noqa: E402
    MAX_IDLE_PER_BUCKET,
    WebViewPool,
)


class _StubViewer(QWidget):
    """只实现池用到的接口（不触碰真实 WebEngine）。"""

    def __init__(self, light=False, pid=0):
        super().__init__()
        self._light_skeleton = light
        self._renderer_pid = pid
        self._context_lost = False
        self._page_dead = False
        self.reset_calls = 0

    def page(self):
        return None if self._page_dead else object()

    def reset_for_reuse(self):
        self.reset_calls += 1


@pytest.fixture(autouse=True)
def fresh_pool():
    """每个用例前后重置单例，避免互相污染。"""
    pool = WebViewPool.get_instance()
    pool.clear()
    WebViewPool.set_enabled(True)
    pool._failures = 0
    # 单例的统计跨用例累积，必须清零，否则断言会被上一个用例污染
    for key in pool._stats:
        pool._stats[key] = 0
    yield pool
    pool.clear()
    WebViewPool.set_enabled(True)


class TestBorrowReturn:
    def test_acquire_on_empty_pool_is_miss(self, fresh_pool, qapp):
        assert fresh_pool.acquire() is None
        assert fresh_pool.stats["miss"] == 1

    def test_release_then_acquire_hits_same_instance(self, fresh_pool, qapp):
        v = _StubViewer()
        assert fresh_pool.release(v) is True
        assert fresh_pool.acquire() is v
        assert fresh_pool.stats["hit"] == 1

    def test_reset_for_reuse_is_called_on_release(self, fresh_pool, qapp):
        v = _StubViewer()
        fresh_pool.release(v)
        assert v.reset_calls == 1

    def test_host_is_offscreen(self, fresh_pool, qapp):
        """隐藏宿主必须 WA_DontShowOnScreen —— 否则会弹幽灵窗口"""
        v = _StubViewer()
        fresh_pool.release(v)
        host = v.parentWidget()
        assert host is not None
        assert bool(host.windowType() & Qt.Tool)
        assert host.testAttribute(Qt.WA_DontShowOnScreen)


class TestQuota:
    def test_overflow_evicts_oldest(self, fresh_pool, qapp):
        kept = []
        for i in range(MAX_IDLE_PER_BUCKET + 2):
            v = _StubViewer()
            kept.append(v)
            fresh_pool.release(v)
        assert fresh_pool.size() == MAX_IDLE_PER_BUCKET
        # 超配额时淘汰最早入池的：返回的是最后 MAX_IDLE_PER_BUCKET 个
        assert fresh_pool.acquire() is kept[-(MAX_IDLE_PER_BUCKET)]

    def test_size_respects_bucket(self, fresh_pool, qapp):
        for _ in range(MAX_IDLE_PER_BUCKET):
            fresh_pool.release(_StubViewer())
        assert fresh_pool.size() == MAX_IDLE_PER_BUCKET
        assert fresh_pool.size(light=True) == 0


class TestBucketing:
    def test_light_and_full_do_not_mix(self, fresh_pool, qapp):
        light_v = _StubViewer(light=True)
        fresh_pool.release(light_v, light=True)
        assert fresh_pool.acquire(light=False) is None
        assert fresh_pool.acquire(light=True) is light_v

    def test_full_bucket_independent(self, fresh_pool, qapp):
        full_v = _StubViewer(light=False)
        fresh_pool.release(full_v, light=False)
        assert fresh_pool.acquire(light=True) is None
        assert fresh_pool.acquire(light=False) is full_v


class TestInvalidation:
    def test_dead_page_is_not_reused(self, fresh_pool, qapp):
        v = _StubViewer()
        fresh_pool.release(v)
        v._page_dead = True
        assert fresh_pool.acquire() is None
        assert fresh_pool.stats["evicted"] == 1

    def test_context_lost_is_not_reused(self, fresh_pool, qapp):
        v = _StubViewer()
        fresh_pool.release(v)
        v._context_lost = True
        assert fresh_pool.acquire() is None

    def test_release_rejects_dead_viewer(self, fresh_pool, qapp):
        v = _StubViewer()
        v._page_dead = True
        assert fresh_pool.release(v) is False
        assert fresh_pool.stats["rejected"] == 1

    def test_release_none_is_safe(self, fresh_pool, qapp):
        assert fresh_pool.release(None) is False


class TestPidGuard:
    def test_pids_exposes_pool_renderers(self, fresh_pool, qapp):
        """池中 renderer 必须被 B4 强回收视为「在用」，否则会被误杀导致白屏"""
        fresh_pool.release(_StubViewer(pid=4321))
        fresh_pool.release(_StubViewer(light=True, pid=8765))
        assert fresh_pool.pids() == {4321, 8765}

    def test_pids_empty_when_idle(self, fresh_pool, qapp):
        assert fresh_pool.pids() == set()


class TestDegradation:
    def test_disabled_pool_neither_acquires_nor_releases(self, fresh_pool, qapp):
        WebViewPool.set_enabled(False)
        v = _StubViewer()
        assert fresh_pool.release(v) is False
        assert fresh_pool.acquire() is None

    def test_consecutive_failures_disable_pool(self, fresh_pool, qapp):
        """连续失败达到阈值后整池停用，避免在异常环境里反复做无效尝试"""
        for _ in range(3):
            fresh_pool._note_failure()
        assert fresh_pool.enabled is False
        assert fresh_pool.acquire() is None


class TestSignalPairing:
    """连接与断开必须覆盖同一组信号。

    池化复用 viewer 的成败全靠这两处对称：漏掉一个断开，旧卡片销毁后
    残留连接触发即 RuntimeError。用 AST 从源码提取，新增信号时若只改一边
    会立刻在这里失败。
    """

    @staticmethod
    def _viewer_attrs(method_name: str) -> set:
        src = inspect.getsource(getattr(MessageCard, method_name))
        tree = ast.parse(textwrap.dedent(src))
        names = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "v"
                and not node.attr.startswith("_")
            ):
                names.add(node.attr)
        return names

    def test_connect_and_disconnect_cover_same_signals(self):
        connect = self._viewer_attrs("_connect_viewer_signals")
        disconnect = self._viewer_attrs("_disconnect_viewer_signals")
        assert connect, "解析不到任何信号，测试或源码结构已变"
        assert connect == disconnect, f"只在一边出现的信号：{connect ^ disconnect}"
