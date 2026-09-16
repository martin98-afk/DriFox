# -*- coding: utf-8 -*-
"""RenderCrashQueue 错峰自愈队列 + 崩溃计数拆分回归测试（T11）。

背景（2026-09-16 0xC0000409 连锁）
----------------------------------
单 renderer 承载全部卡片时，一次 renderer 崩溃让所有卡片同帧收到
``renderCrashed``。旧实现每卡即刻 ``_try_restore_context``，N 张卡的重载
齐发把刚重启的 Chromium 线程再次压垮 → 进程级死亡。

T11 修复：
1. ``RenderCrashQueue`` 全局单例：崩溃 viewer 入队，每 500ms 只恢复一张，
   可见优先；weakref 持有、去重、池化归还时 discard。
2. 计数按信号源拆分：``_render_crash_count``（renderCrashed，阈值 >2）与
   ``_webgl_ctx_lost_count``（JS webglcontextlost，阈值 >1）独立累加 —— 单次
   renderer 崩溃引发的 webgl 集中上报不再叠加推高崩溃计数。

测试策略：队列用 ``_FakeViewer`` 鸭子类型（不拉起 WebEngine）；崩溃计数
语义用 ``CodeWebViewer.__new__`` + monkeypatch 队列方法（不断言真实恢复链）。
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.widgets.render_crash_queue import _MIN_INTERVAL_MS, RenderCrashQueue


class _FakeViewer:
    """队列所需的 viewer 最小接口：_context_lost / isVisible / _try_restore_context"""

    def __init__(self, visible: bool = True):
        self._context_lost = True
        self._visible = visible
        self.restore_calls = 0

    def isVisible(self) -> bool:
        return self._visible

    def _try_restore_context(self) -> None:
        self.restore_calls += 1
        self._context_lost = False  # 真实实现的语义：恢复成功置 False


@pytest.fixture
def queue():
    """隔离的队列实例（不污染进程级单例）"""
    q = RenderCrashQueue()
    yield q
    q._pending.clear()
    if q._timer is not None:
        q._timer.stop()


# ─── 错峰：每拍只恢复一张，间隔 ≥ _MIN_INTERVAL_MS ──────────────────


def test_drain_one_recovers_single_viewer_per_tick(queue):
    """一次 _drain_one 只恢复一张：N 张入队不齐发"""
    viewers = [_FakeViewer() for _ in range(3)]
    for v in viewers:
        queue.enqueue(v)

    queue._drain_one()
    assert sum(v.restore_calls for v in viewers) == 1  # 恰好一张被恢复
    assert queue.pending_count() == 2


def test_tick_timestamp_gaps_at_least_min_interval(queue, qapp):
    """错峰间隔断言：连续 drain 的时间戳序列间隔 ≥ _MIN_INTERVAL_MS

    通过真实 QTimer 驱动：入队 3 张，用 qapp.processEvents + QTest.qWait 推进，
    记录各 viewer 的恢复时刻。
    """
    import time

    stamps: list = []
    viewers = []
    for _ in range(3):
        v = _FakeViewer()
        orig = v._try_restore_context

        def spy(v=v, orig=orig):
            stamps.append(time.monotonic())
            orig()

        v._try_restore_context = spy
        viewers.append(v)
        queue.enqueue(v)

    # 首拍在 _MIN_INTERVAL_MS 后才触发；等足 3 拍 + 余量
    # （手工 sleep + processEvents：等价 QTest.qWait，避其存根签名误报）
    deadline = time.monotonic() + (_MIN_INTERVAL_MS / 1000.0) * 3 + 2.0
    while len(stamps) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
        qapp.processEvents()

    assert len(stamps) == 3, f"应恢复全部 3 张，实际 {len(stamps)}"
    gaps = [(b - a) * 1000.0 for a, b in zip(stamps, stamps[1:])]
    assert all(g >= _MIN_INTERVAL_MS * 0.8 for g in gaps), f"拍间隔过密: {gaps}"  # 计时抖动留 20% 余量


# ─── 可见优先 ────────────────────────────────────────────────────────


def test_visible_viewer_prioritized(queue):
    """可见者优先恢复（用户眼前内容先回来）"""
    hidden = _FakeViewer(visible=False)
    visible = _FakeViewer(visible=True)
    queue.enqueue(hidden)
    queue.enqueue(visible)  # 后入队但在可见区

    queue._drain_one()
    assert visible.restore_calls == 1
    assert hidden.restore_calls == 0

    queue._drain_one()
    assert hidden.restore_calls == 1


# ─── 去重与剔除 ──────────────────────────────────────────────────────


def test_enqueue_dedup(queue):
    """同一 viewer 重复入队只登记一次"""
    v = _FakeViewer()
    assert queue.enqueue(v) is True
    assert queue.enqueue(v) is False
    assert queue.pending_count() == 1

    queue._drain_one()
    assert v.restore_calls == 1
    assert queue.pending_count() == 0


def test_dead_viewer_pruned(queue):
    """weakref 失效（viewer 已销毁）的条目在下一拍被剔除，不影响其他条目"""
    dead = _FakeViewer()
    alive = _FakeViewer(visible=False)
    queue.enqueue(dead)
    queue.enqueue(alive)
    del dead  # 强引用消失 → weakref 失效

    queue._drain_one()  # 剔除 dead，恢复 alive
    assert alive.restore_calls == 1
    assert queue.pending_count() == 0


def test_discard_removes_pending(queue):
    """discard（池化归还路径）后条目不再被恢复"""
    v = _FakeViewer()
    queue.enqueue(v)
    queue.discard(v)
    assert queue.pending_count() == 0

    queue._drain_one()
    assert v.restore_calls == 0


def test_recovered_by_other_path_pruned(queue):
    """他路已恢复（_context_lost=False）→ 队列剔除，不重复恢复

    对应 JS webgl 路径先恢复的场景：``_try_restore_context`` 置 False 后，
    排队器下一拍自动剔除（T11 变更指令 ③ 的语义验证）。
    """
    v = _FakeViewer()
    queue.enqueue(v)
    v._context_lost = False  # 模拟 JS webgl 路径已恢复

    queue._drain_one()
    assert v.restore_calls == 0


# ─── 计数拆分与阈值升级链 ────────────────────────────────────────────


def _make_viewer() -> Any:
    """绕过 __init__ 的 CodeWebViewer 桩（只测 _on_render_crashed 逻辑）

    返回 Any：桩对象由 ``__new__`` 构造，pyright 无法推断其动态注入的属性
    （pyqtSignal / MagicMock 存根均会误报）。
    """
    from app.widgets.message_card import CodeWebViewer

    v = CodeWebViewer.__new__(CodeWebViewer)
    v._context_lost = False
    v._render_crash_count = 0
    v._webgl_ctx_lost_count = 0
    v.needRecreate = MagicMock()
    v.contextLost = MagicMock()
    v.contextRestored = MagicMock()
    return v


def test_render_crash_enqueues_below_threshold(monkeypatch):
    """崩溃（未到阈值）→ 置 _context_lost + 入队，不直接恢复、不重建"""
    v = _make_viewer()
    enq = MagicMock(return_value=True)
    monkeypatch.setattr(RenderCrashQueue, "get_instance", classmethod(lambda cls: MagicMock(enqueue=enq)))

    v._on_render_crashed()

    assert v._render_crash_count == 1
    assert v._context_lost is True
    enq.assert_called_once_with(v)
    v.needRecreate.emit.assert_not_called()


def test_render_crash_upgrades_to_recreate_at_third(monkeypatch):
    """第 3 次崩溃（>2）→ needRecreate 升级，不再入队"""
    v = _make_viewer()
    enq = MagicMock(return_value=True)
    monkeypatch.setattr(RenderCrashQueue, "get_instance", classmethod(lambda cls: MagicMock(enqueue=enq)))

    v._on_render_crashed()
    v._on_render_crashed()
    v._on_render_crashed()  # 第 3 次

    assert v._render_crash_count == 3
    assert v.needRecreate.emit.call_count == 1
    assert enq.call_count == 2  # 前两次入队，第三次直接重建


def test_webgl_count_does_not_promote_render_crash(monkeypatch):
    """webgl 计数叠加不再推高崩溃计数（T11 变更指令 ④）

    单 renderer 崩溃引发的 JS webgl 集中上报只累加 _webgl_ctx_lost_count，
    第一次 renderCrashed 不应因共享计数被误推过阈值。
    """
    v = _make_viewer()
    enq = MagicMock(return_value=True)
    monkeypatch.setattr(RenderCrashQueue, "get_instance", classmethod(lambda cls: MagicMock(enqueue=enq)))

    # JS webgl 路径先报到 2 次（第二次触发 needRecreate，属 webgl 自身阈值 >1）
    # 真实链路：lost → 置 True → 500ms 后恢复置 False → 再次 lost 才算第二次
    v._handle_context_lost()
    v._context_lost = False  # 模拟 _try_restore_context 恢复成功
    v._handle_context_lost()
    assert v._webgl_ctx_lost_count == 2
    assert v.needRecreate.emit.call_count == 1  # webgl 阈值命中

    # 随后 renderer 崩溃：崩溃计数独立从 0 起算，仍走排队而非重建
    v._context_lost = False
    v._on_render_crashed()
    assert v._render_crash_count == 1
    assert enq.call_count == 1
    assert v.needRecreate.emit.call_count == 1  # 崩溃路径未额外触发重建


def test_counters_reset_independently_on_reuse(monkeypatch):
    """两路径计数在 reset_for_reuse 中独立清零（T11 变更指令 ④）"""
    from app.widgets.message_card import CodeWebViewer

    v = _make_viewer()
    discard = MagicMock()
    monkeypatch.setattr(RenderCrashQueue, "get_instance", classmethod(lambda cls: MagicMock(discard=discard)))
    # reset_for_reuse 会清大量字段与页面状态，用最小替身屏蔽页面调用
    v.page = MagicMock(return_value=None)
    v._is_js_ready = False
    v.setMinimumHeight = MagicMock()
    v.setFixedHeight = MagicMock()
    v._streaming = False
    v._is_history = False
    v._stable_html = ""
    v._stable_md_len = 0
    v._needs_full_render = True
    v._tail_html_hash = 0
    v._lazy_markdown_cb = None
    v._restore_finished_ids = None
    v._resize_locked = False
    v._height_report_pending = False
    v._document_height = 0
    v._body_client_height = 0
    v._body_scroll_top = 0
    v._body_geom_valid = False
    v._markdown_text = ""
    v._last_rendered_markdown = ""
    v._cached_streaming_html = None
    v._processed_md_hash = 0
    v._cached_raw_md_hash = 0
    v._last_rendered_html = None
    v._render_deferred = False
    v._pending_todos = None
    v._tool_md_cache = {}  # hasattr 对裸 __new__ 实例访问未定义属性抛 RuntimeError，须预置
    v._render_crash_count = 3
    v._webgl_ctx_lost_count = 2
    v._context_lost = True

    CodeWebViewer.reset_for_reuse(v)

    assert v._render_crash_count == 0
    assert v._webgl_ctx_lost_count == 0
    assert v._context_lost is False
    discard.assert_called_once_with(v)
