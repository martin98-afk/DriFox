# -*- coding: utf-8 -*-
"""主线程阻塞 P0/P1 回归测试（2026-10-31 运行日志驱动）。

背景：all.log 中 MainThreadWatchdog 抓到 47 次主线程阻塞（500~695ms），归因出
三条可修的路径，本文件固定这三条的修复语义：

1. ``chat_worker._compact_process_heap`` 不再裸调 ``gc.collect(2)``，改走
   ``memory_governor`` 的节流版（默认只收年轻代）。原实现每轮对话都在主线程
   付一次全代 stop-the-world（实测最长 695ms）。
2. ``OpenAIChatWorker._close_http_client`` 在 worker 线程关闭复用 client。
   原实现把 client 引用留到主线程 gc 才析构，openai 的
   ``SyncHttpxClientWrapper.__del__`` 会在主线程同步 close 连接池。
3. ``HistoryManager._reload_if_db_changed`` 读路径不再同步 flush：有待保存会话
   时内存领先于 SQLite，重载会把数据换回旧快照，故跳过本轮重载并异步写盘。
4. ``PluginHostService._stop_plugin_watcher(wait=False)`` 应用退出路径不等
   watcher 线程 join（原 join 实测阻塞主线程 535ms）。
"""

import sys
import threading
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── 1. 堆回收走节流版 ──────────────────────────────────────────


def test_compact_process_heap_uses_throttled_governor(monkeypatch):
    """cleanup 路径必须调 memory_governor 的节流版，不得裸调 gc.collect(2)。"""
    import app.core.infra.memory_governor as mg
    from app.core.workers import chat_worker as cw

    calls = []
    monkeypatch.setattr(mg, "_compact_process_heap_after_cleanup", lambda *a, **k: calls.append(1))
    # 绕过 pytest 环境守卫（该守卫为「不拖慢测试」保留，与本用例语义无关）
    monkeypatch.setattr(sys, "argv", ["drifox"])
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    cw._compact_process_heap()

    assert calls == [1], "堆回收必须委托给 memory_governor 的节流实现"


def test_compact_process_heap_skips_under_pytest(monkeypatch):
    """pytest 环境仍跳过（守卫语义不变，避免拖慢测试套件）。"""
    import app.core.infra.memory_governor as mg
    from app.core.workers import chat_worker as cw

    calls = []
    monkeypatch.setattr(mg, "_compact_process_heap_after_cleanup", lambda *a, **k: calls.append(1))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.setattr(sys, "argv", ["drifox"])

    cw._compact_process_heap()

    assert calls == []


# ── 2. HTTP 客户端在 worker 线程内关闭 ──────────────────────────


class _FakeClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _bare_worker():
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker.__new__(OpenAIChatWorker)
    w._stream_lock = threading.RLock()
    w._http_client = None
    return w


def test_close_http_client_closes_and_clears_ref():
    w = _bare_worker()
    client = _FakeClient()
    w._http_client = client

    w._close_http_client()

    assert client.closed is True, "client 必须在当前线程被显式关闭"
    assert w._http_client is None, "关闭后必须清引用，否则主线程 gc 仍会二次析构"


def test_close_http_client_is_idempotent():
    w = _bare_worker()
    w._close_http_client()  # 无 client：no-op

    client = _FakeClient()
    w._http_client = client
    w._close_http_client()
    w._close_http_client()  # 已清引用：不得重复 close

    assert client.closed is True


def test_close_http_client_swallows_close_error():
    """close 抛错不得影响 run() 的其余收尾步骤。"""
    w = _bare_worker()

    class _Broken:
        def close(self):
            raise RuntimeError("boom")

    w._http_client = _Broken()
    w._close_http_client()  # 不抛
    assert w._http_client is None


# ── 3. 读路径不同步 flush ──────────────────────────────────────


class _FakeStore:
    def __init__(self):
        self.calls = 0

    def get_sessions_lightweight(self, limit=500):
        self.calls += 1
        return [{"session_id": "s1", "title": "t", "project": "p", "messages": []}]


def _bare_history_manager():
    from app.utils.history_manager import HistoryManager

    m = HistoryManager.__new__(HistoryManager)
    m._use_sqlite = True
    m._db_signature_stored = (1.0,)
    m._session_store = _FakeStore()
    m._db_signature = lambda: (2.0,)
    m._history_load_lock = threading.Lock()
    m._history_sessions = []
    m._history_loaded = False
    m._history_limit = 500
    m._deduplicate_history_sessions = lambda: None
    m._migrate_if_needed = lambda: None
    m._mark_cache_dirty = lambda: None
    m._pending_save_session_id = None
    return m


def test_reload_with_pending_save_skips_sync_flush_and_reload():
    """有待保存写时：不同步 flush、不重载（内存领先），改走异步写盘。"""
    m = _bare_history_manager()
    counters = {"sync_flush": 0, "async": 0}
    m.flush = lambda *a, **k: counters.__setitem__("sync_flush", counters["sync_flush"] + 1)
    m.flush_async = lambda *a, **k: counters.__setitem__("async", counters["async"] + 1)
    m._pending_save_session_id = "abcd1234-dead-beef"

    m._reload_if_db_changed()

    assert counters["sync_flush"] == 0, "读路径不得同步 flush（实测 500ms+ 主线程阻塞）"
    assert counters["async"] == 1, "待保存写必须转后台线程落盘"
    assert m._session_store.calls == 0, "内存领先时不得从 SQLite 重载（会丢最新写入）"
    assert m._db_signature_stored == (1.0,), "跳过重载时签名不得推进，下次读取仍需对齐"


def test_reload_without_pending_save_still_reloads():
    """无待保存写时保持原语义：重载内存缓存并推进签名。"""
    m = _bare_history_manager()

    m._reload_if_db_changed()

    assert m._session_store.calls == 1, "外部进程写入必须触发重载"
    assert [s["session_id"] for s in m._history_sessions] == ["s1"]
    assert m._db_signature_stored == (2.0,)


# ── 4. 退出路径不等 watcher 线程 ────────────────────────────────


class _SlowThread:
    """模拟 watchfiles 线程：join 要等到 debounce 窗口结束（这里用 sleep 占位）。"""

    def __init__(self, join_cost_s: float):
        self._cost = join_cost_s
        self.joined = False

    def is_alive(self):
        return True

    def join(self, timeout=None):
        import time

        self.joined = True
        time.sleep(min(timeout or 0, self._cost))


def _bare_plugin_host():
    from app.core.services.plugin_host_service import PluginHostService

    svc = PluginHostService.__new__(PluginHostService)
    svc._plugin_watcher_refcount = 1
    svc._plugin_watcher_stop = threading.Event()
    svc._plugin_watcher_thread = None
    svc._plugin_watcher_started = True
    return svc


def test_stop_plugin_watcher_wait_false_does_not_join():
    import time

    svc = _bare_plugin_host()
    slow = _SlowThread(join_cost_s=0.4)
    svc._plugin_watcher_thread = slow

    started = time.perf_counter()
    svc._stop_plugin_watcher(wait=False)
    elapsed = time.perf_counter() - started

    assert slow.joined is False, "退出路径不得在调用线程 join watcher"
    assert elapsed < 0.05, f"退出路径必须立即返回（实测 {elapsed * 1000:.0f}ms）"
    assert svc._plugin_watcher_stop.is_set() is True, "stop_event 仍须置位（线程自行退出）"
    assert svc._plugin_watcher_started is False, "标志必须复位，允许后续重开"


def test_stop_plugin_watcher_default_still_joins():
    """默认 wait=True（窗口关闭但进程存活）：必须等线程退出再放引用。"""
    svc = _bare_plugin_host()
    slow = _SlowThread(join_cost_s=0.05)
    svc._plugin_watcher_thread = slow

    svc._stop_plugin_watcher()

    assert slow.joined is True, "默认路径必须 join（托盘驻留重开场景）"


def test_stop_plugin_watcher_refcount_guard():
    """仍有活跃窗口时不停止 watcher、不置 stop_event。"""
    svc = _bare_plugin_host()
    svc._plugin_watcher_refcount = 2
    slow = _SlowThread(join_cost_s=0.0)
    svc._plugin_watcher_thread = slow

    svc._stop_plugin_watcher(wait=False)

    assert svc._plugin_watcher_refcount == 1
    assert slow.joined is False
    assert svc._plugin_watcher_stop.is_set() is False
    assert svc._plugin_watcher_started is True
