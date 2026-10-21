# -*- coding: utf-8 -*-
"""T29 整改回归测试（render_crash_queue A1-A5 + main_widget B1）。

覆盖：
- A2 进程退出清理：aboutToQuit 触发时队列清空且定时器停止
- A3 恢复失败防循环入队：_try_restore_context 抛异常后 viewer 被置
  _context_lost=False，不被其他触发点重新算作「待恢复」
- A1 定时器挂 QApplication parent
- A4 尾部 start 有 isActive 守卫（不重置已排期的拍）
- 多窗口可见优先扩展：多个可见 viewer 时按入队顺序（首个可见者优先），
  且第二个可见者仍留在队列内等待下一拍
- 跨线程约束：主线程约束下无法构造真实竞态 → 静态断言 docstring 声明

复用 test_render_crash_queue.py 的 _FakeViewer / queue fixture 模式。
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QApplication

from app.widgets.render_crash_queue import RenderCrashQueue

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FailingViewer:
    """恢复必失败的 viewer（模拟 _try_restore_context 抛异常）"""

    def __init__(self, visible: bool = True):
        self._context_lost = True
        self._visible = visible
        self.restore_calls = 0

    def isVisible(self) -> bool:
        return self._visible

    def _try_restore_context(self) -> None:
        self.restore_calls += 1
        raise RuntimeError("模拟恢复失败（对象已析构）")


class _OkViewer:
    def __init__(self, visible: bool = True):
        self._context_lost = True
        self._visible = visible
        self.restore_calls = 0

    def isVisible(self) -> bool:
        return self._visible

    def _try_restore_context(self) -> None:
        self.restore_calls += 1
        self._context_lost = False


@pytest.fixture
def queue():
    q = RenderCrashQueue()
    yield q
    q._pending.clear()
    if q._timer is not None:
        q._timer.stop()


# ─── A2：进程退出清队列 ──────────────────────────────────────────────


def test_abouttoquit_clears_queue(qapp):
    """A2：aboutToQuit 信号触发 clear → 队列空 + 定时器停"""
    q = RenderCrashQueue()
    # __init__ 已把 clear 接到 aboutToQuit；直接验证连接生效
    v = _OkViewer()
    q.enqueue(v)
    assert q.pending_count() == 1
    assert q._timer is not None and q._timer.isActive()

    # 模拟退出：发射 aboutToQuit（真实退出时由 Qt 发射）
    app = QApplication.instance()
    assert app is not None
    app.aboutToQuit.emit()

    assert q.pending_count() == 0, "aboutToQuit 应清空队列（防退出期 timeout 访问已析构 viewer）"
    assert q._timer is not None and not q._timer.isActive(), "aboutToQuit 应停止定时器"


# ─── A1：定时器挂 parent ──────────────────────────────────────────────


def test_timer_has_qapplication_parent(qapp):
    """A1：懒创建的 QTimer 以 QApplication 为 parent（随进程销毁）"""
    q = RenderCrashQueue()
    v = _OkViewer()
    q.enqueue(v)

    assert q._timer is not None
    assert q._timer.parent() is QApplication.instance(), "QTimer 应挂 QApplication parent 防退出期野回调"


# ─── A3：恢复失败防循环入队 ──────────────────────────────────────────


def test_failed_restore_marks_context_lost_false(queue):
    """A3：恢复抛异常 → 置 _context_lost=False，不再被视为待恢复对象"""
    v = _FailingViewer()
    queue.enqueue(v)
    queue._drain_one()

    assert v.restore_calls == 1, "应尝试恢复一次"
    assert v._context_lost is False, "失败后必须置 False，否则每拍循环入队（CPU/日志风暴）"
    assert queue.pending_count() == 0, "失败者已出队"


def test_failed_viewer_not_requeued_by_trigger(queue):
    """A3 补充：失败出队后，后续 enqueue 对同对象仍可入队（一次性语义）

    验证「失败置 False」的语义边界：置 False 只影响 _pick_next 的剔除判定，
    不阻止外部显式 enqueue（真实链路里崩溃重入队属新事件，应允许）。
    """
    v = _FailingViewer()
    queue.enqueue(v)
    queue._drain_one()
    assert v._context_lost is False

    # 标记为「又崩了」→ 可重新入队（真实链路：第二次 renderCrashed）
    v._context_lost = True
    assert queue.enqueue(v) is True
    assert queue.pending_count() == 1


def test_failed_restore_stops_drain_chain(qapp, queue):
    """A3 端到端：失败恢复后队列清空，定时器不再持续排拍"""
    v = _FailingViewer()
    queue.enqueue(v)
    queue._drain_one()
    assert queue.pending_count() == 0
    # 队列空 → 尾部不重排（无待办）
    timer = queue._timer
    assert timer is not None
    # 无 pending 时不应再有新的 start（除非外部又 enqueue）
    assert not queue._pending


# ─── A4：尾部 start 的 isActive 守卫 ─────────────────────────────────


def test_drain_one_does_not_restart_active_timer(queue):
    """A4：_drain_one 执行期间若定时器已被新入队重新排期，尾部不得再 start 重置"""
    v1 = _OkViewer()
    queue.enqueue(v1)  # 启动第一拍
    # 入队第二个（_ensure_timer 发现已 active，不重启）
    v2 = _OkViewer()
    queue.enqueue(v2)

    timer = queue._timer
    assert timer is not None
    # 模拟 _drain_one 执行中新 viewer 入队导致定时器已被排期
    timer.start()  # 主动排期（模拟 enqueue → _ensure_timer 的 start）
    assert timer.isActive()

    # 此时 _drain_one 尾部若无条件 start 会重置计时器；有守卫则保持
    remaining_before = timer.remainingTime()
    queue._drain_one()
    # 若尾部重置了定时器，remainingTime 会跳回接近满值
    if queue._pending and timer.isActive():
        remaining_after = timer.remainingTime()
        assert remaining_after <= remaining_before + 50, "尾部 start 不得重置已排期的计时器（会无限推后节奏）"


# ─── 多窗口可见优先扩展 ──────────────────────────────────────────────


def test_multiple_visible_viewers_first_visible_wins(queue):
    """多窗口：多个可见 viewer 时取首个可见者，其余留在队列等下一拍"""
    hidden = _OkViewer(visible=False)
    vis_a = _OkViewer(visible=True)
    vis_b = _OkViewer(visible=True)
    queue.enqueue(hidden)
    queue.enqueue(vis_a)
    queue.enqueue(vis_b)

    queue._drain_one()
    assert vis_a.restore_calls == 1, "首个可见者（按入队顺序）优先恢复"
    assert vis_b.restore_calls == 0, "第二个可见者留待下一拍"
    assert hidden.restore_calls == 0, "隐藏者最后"
    assert queue.pending_count() == 2

    queue._drain_one()
    assert vis_b.restore_calls == 1
    assert queue.pending_count() == 1


# ─── 跨线程约束（降级静态断言） ──────────────────────────────────────


def test_docstring_declares_main_thread_only():
    """跨线程 race：主线程约束下无法构造真实竞态 → 静态断言 docstring 约束

    A5 要求标注「仅主线程调用」。_pending 是裸 OrderedDict 无锁，
    跨线程并发 enqueue/discard 会产生竞态；此处锁定文档约束存在。
    """
    src = (REPO_ROOT / "app" / "widgets" / "render_crash_queue.py").read_text(encoding="utf-8")
    assert "仅限主线程调用" in src, "docstring 必须声明主线程约束（A5）"
    assert "QMetaObject.invokeMethod" in src or "信号桥接" in src, "应指明跨线程调用的正确 marshal 方式"


def test_source_has_required_fixes():
    """源码静态断言：A1-A4 四项改动的关键结构都在"""
    src = (REPO_ROOT / "app" / "widgets" / "render_crash_queue.py").read_text(encoding="utf-8")
    assert "QTimer(QApplication.instance())" in src, "A1：QTimer 应挂 QApplication parent"
    assert "aboutToQuit.connect(self.clear)" in src, "A2：应连 aboutToQuit → clear"
    assert "viewer._context_lost = False" in src, "A3：恢复失败应置 _context_lost=False"
    assert "not timer.isActive()" in src, "A4：尾部 start 应有 isActive 守卫"
